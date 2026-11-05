"""株価まわりの計算が正しいかを、作り物のデータで確かめるテスト。
（ネットにはつながない）"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from kabu.config import JST
from kabu.data.prices import (
    check_prices,
    drop_unfinished_today,
    extract_yfinance,
    merge_with_cache,
    parse_stooq_csv,
)
from kabu.quality import QualityLog

CONF = {"max_stale_days": 5, "min_history_rows": 3, "max_daily_move": 0.3, "suspicious_check_rows": 30,
        "market_close_jst": "16:00"}


def make_prices(dates, closes, volume=1000):
    idx = pd.DatetimeIndex(pd.to_datetime(dates), name="date")
    closes = np.array(closes, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "adj_close": closes, "volume": volume, "source": "test"},
        index=idx,
    )


def test_extract_yfinance_multiindex():
    idx = pd.to_datetime(["2026-10-28", "2026-10-29"])
    columns = pd.MultiIndex.from_product(
        [["7203.T", "6758.T"], ["Open", "High", "Low", "Close", "Adj Close", "Volume"]]
    )
    frame = pd.DataFrame(np.arange(24, dtype=float).reshape(2, 12) + 1, index=idx, columns=columns)
    frame.loc[:, ("6758.T", slice(None))] = np.nan  # 6758 は取れなかった想定

    toyota = extract_yfinance(frame, "7203.T")
    assert list(toyota["close"]) == [4.0, 16.0]
    assert toyota["source"].iloc[0] == "yfinance"
    assert extract_yfinance(frame, "6758.T") is None
    assert extract_yfinance(frame, "9999.T") is None


def test_parse_stooq_csv():
    text = "Date,Open,High,Low,Close,Volume\n2026-10-29,100,110,90,105,5000\n"
    df = parse_stooq_csv(text)
    assert df["close"].iloc[0] == 105
    assert np.isnan(df["adj_close"].iloc[0])
    assert parse_stooq_csv("No data") is None


def test_merge_with_cache_appends_new_days():
    cached = make_prices(["2026-10-27", "2026-10-28"], [100, 101])
    fresh = make_prices(["2026-10-28", "2026-10-29"], [101, 102])
    merged, consistent = merge_with_cache(cached, fresh)
    assert consistent
    assert list(merged["close"]) == [100, 101, 102]


def test_merge_with_cache_detects_split():
    cached = make_prices(["2026-10-27", "2026-10-28"], [1000, 1010])
    fresh = make_prices(["2026-10-28", "2026-10-29"], [505, 510])  # 1:2 の株式分割で過去分が半分に
    _, consistent = merge_with_cache(cached, fresh)
    assert not consistent


def test_drop_unfinished_today():
    df = make_prices(["2026-10-29", "2026-10-30"], [100, 101])
    during = datetime(2026, 10, 30, 11, 0, tzinfo=JST)
    after = datetime(2026, 10, 30, 17, 0, tzinfo=JST)
    kept, dropped = drop_unfinished_today(df, during, "16:00")
    assert len(kept) == 1 and dropped
    kept, dropped = drop_unfinished_today(df, after, "16:00")
    assert len(kept) == 2 and not dropped


def test_check_prices_flags_lagging_and_short():
    now = datetime(2026, 10, 30, 17, 0, tzinfo=JST)
    prices = {
        "1111": make_prices(["2026-10-28", "2026-10-29", "2026-10-30"], [100, 101, 102]),
        "2222": make_prices(["2026-10-27", "2026-10-28", "2026-10-29"], [100, 101, 102]),  # 1日遅れ
        "3333": make_prices(["2026-10-29", "2026-10-30"], [100, 101], volume=0),         # 短い・出来高0
    }
    quality = QualityLog()
    reference = check_prices(prices, CONF, now, quality)
    assert reference == pd.Timestamp("2026-10-30")
    flagged = {(i.level, i.code) for i in quality.issues}
    assert ("WARN", "2222") in flagged
    assert ("WARN", "3333") in flagged
    assert ("INFO", "3333") in flagged
    assert not any(i.code == "1111" for i in quality.issues)


def test_check_prices_flags_all_stale():
    now = datetime(2026, 10, 30, 17, 0, tzinfo=JST)
    prices = {"1111": make_prices(["2026-10-20", "2026-10-21", "2026-10-22"], [1, 2, 3])}
    quality = QualityLog()
    check_prices(prices, CONF, now, quality)
    assert any(i.code == "" and "止まり" in i.message for i in quality.issues)


def test_find_suspicious_moves():
    from kabu.data.prices import find_suspicious_moves
    df = make_prices(["2026-10-27", "2026-10-28", "2026-10-29", "2026-10-30"], [100, 101, 1010, 102])
    moves = find_suspicious_moves(df, 0.3)
    assert set(moves["date"].astype(str)) == {"2026-10-29", "2026-10-30"}   # 10倍になった日と戻った日
    assert find_suspicious_moves(make_prices(["2026-10-29", "2026-10-30"], [100, 120]), 0.3).empty


def test_check_prices_warns_on_suspicious_move():
    now = datetime(2026, 10, 30, 17, 0, tzinfo=JST)
    prices = {"1111": make_prices(["2026-10-28", "2026-10-29", "2026-10-30"], [100, 1000, 100])}
    quality = QualityLog()
    check_prices(prices, CONF, now, quality)
    assert any("異常に大きな値動き" in i.message for i in quality.issues)


def test_repair_scale_glitch_two_days():
    """実際に起きた例: TOPIX ETF が2日間だけ 1/10 の値になっていた。"""
    from kabu.data.prices import repair_scale_glitches
    df = make_prices(["2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31", "2026-04-01"],
                     [380.0, 382.7, 37.64, 37.14, 389.2])
    df.loc["2026-03-30", "open"] = 37.0
    fixed, repaired = repair_scale_glitches(df)
    assert repaired == [(pd.Timestamp("2026-03-30").date(), pd.Timestamp("2026-03-31").date(), 10.0)]
    assert list(fixed["close"].round(1)) == [380.0, 382.7, 376.4, 371.4, 389.2]
    assert fixed.loc["2026-03-30", "open"] == pytest.approx(370.0)
    assert "桁ずれ補正" in fixed.loc["2026-03-30", "source"]
    assert fixed.loc["2026-03-27", "source"] == "test"


def test_repair_leaves_real_split_alone():
    """水準が変わったまま戻らないもの（本当の株式分割の可能性）は直さない。"""
    from kabu.data.prices import repair_scale_glitches
    df = make_prices(["2026-03-26", "2026-03-27", "2026-03-30", "2026-03-31", "2026-04-01"],
                     [1000.0, 1010.0, 101.0, 102.0, 103.0])
    fixed, repaired = repair_scale_glitches(df)
    assert repaired == [] and list(fixed["close"]) == [1000.0, 1010.0, 101.0, 102.0, 103.0]


def test_repair_ignores_normal_moves():
    from kabu.data.prices import repair_scale_glitches
    df = make_prices(["2026-03-26", "2026-03-27", "2026-03-30"], [1000.0, 1200.0, 1000.0])
    assert repair_scale_glitches(df)[1] == []


def _split_frame(after_volume):
    dates = pd.bdate_range("2026-01-05", periods=60, name="date")
    closes = np.r_[np.full(30, 11040.0), np.full(30, 3713.0)]
    volumes = np.r_[np.full(30, 100_000.0), np.full(30, after_volume)]
    return pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes,
                         "adj_close": closes, "volume": volumes, "source": "yfinance"}, index=dates)


def test_repair_unadjusted_split_with_volume_evidence():
    """実際に起きた例: しまむら（8227）の 1:3 分割が反映されていなかった。出来高も約3倍になる。"""
    from kabu.data.prices import clean_prices
    df = _split_frame(after_volume=300_000.0)
    fixed, notes = clean_prices(df)
    assert len(notes) == 1 and "1:3の株式分割" in notes[0]
    assert fixed["close"].iloc[0] == pytest.approx(11040 / 3)
    assert fixed["volume"].iloc[0] == pytest.approx(300_000)
    assert fixed["close"].iloc[-1] == 3713.0
    assert "1:3分割を調整" in fixed["source"].iloc[0]


def test_no_split_repair_without_volume_change():
    """出来高が変わらないなら分割とは言えないので、直さない。"""
    from kabu.data.prices import clean_prices
    fixed, notes = clean_prices(_split_frame(after_volume=100_000.0))
    assert notes == [] and fixed["close"].iloc[0] == 11040.0


def test_drop_invalid_rows():
    """実際に起きた例: 再上場した SBI新生銀行（8303）の上場前の日に 5.5×10^10 円が入っていた。"""
    from kabu.data.prices import clean_prices
    df = make_prices(["2025-12-17", "2025-12-18", "2025-12-19"], [5.532e10, 1800.0, 1810.0])
    fixed, notes = clean_prices(df)
    assert list(fixed["close"]) == [1800.0, 1810.0]
    assert "ありえない値" in notes[0]


def test_expected_latest_weekday():
    from kabu.data.prices import expected_latest_weekday
    # 実際に起きた例: 11/6（金）0:15 の実行なら、11/5（木）の株価まであるはず
    assert expected_latest_weekday(datetime(2026, 11, 6, 0, 15, tzinfo=JST), "16:00") == pd.Timestamp("2026-11-05")
    assert expected_latest_weekday(datetime(2026, 11, 6, 17, 0, tzinfo=JST), "16:00") == pd.Timestamp("2026-11-06")
    assert expected_latest_weekday(datetime(2026, 11, 9, 10, 0, tzinfo=JST), "16:00") == pd.Timestamp("2026-11-06")  # 月曜の朝 → 金曜
    assert expected_latest_weekday(datetime(2026, 11, 8, 20, 0, tzinfo=JST), "16:00") == pd.Timestamp("2026-11-06")  # 日曜 → 金曜


def test_check_prices_warns_when_latest_weekday_missing():
    now = datetime(2026, 11, 6, 0, 15, tzinfo=JST)
    prices = {"1111": make_prices(["2026-11-02", "2026-11-04"], [100, 101])}
    quality = QualityLog()
    check_prices(prices, CONF, now, quality)
    assert any("2026-11-05 の株価が、どの銘柄にもありません" in i.message for i in quality.issues)
