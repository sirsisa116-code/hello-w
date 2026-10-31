"""株価まわりの計算が正しいかを、作り物のデータで確かめるテスト。
（ネットにはつながない）"""

from datetime import datetime

import numpy as np
import pandas as pd

from kabu.config import JST
from kabu.data.prices import (
    check_prices,
    drop_unfinished_today,
    extract_yfinance,
    merge_with_cache,
    parse_stooq_csv,
)
from kabu.quality import QualityLog

CONF = {"max_stale_days": 5, "min_history_rows": 3}


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
        "1111": make_prices(["2026-10-28", "2026-10-29", "2026-10-30"], [1, 2, 3]),
        "2222": make_prices(["2026-10-27", "2026-10-28", "2026-10-29"], [1, 2, 3]),  # 1日遅れ
        "3333": make_prices(["2026-10-29", "2026-10-30"], [1, 2], volume=0),         # 短い・出来高0
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
