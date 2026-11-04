"""事後検証と後付けの再現のテスト（作り物のデータで、ネットにはつながない）。"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from kabu.analysis.technical import WEEKS52_DAYS
from kabu.backfill import backfill, default_start
from kabu.config import JST
from kabu.evaluation import CONFIRMED, MISSING, PENDING, build_evaluation_report, forward_returns, summarize
from kabu.scoring import load_weights

DATES = pd.bdate_range("2026-10-01", periods=10, name="date")


def frame(opens, closes, dates=DATES):
    return pd.DataFrame({"open": opens, "high": closes, "low": closes, "close": closes,
                         "adj_close": closes, "volume": 1e6, "source": "test"}, index=dates)


def history_rows(rows):
    return pd.DataFrame(rows, columns=["data_date", "code", "name", "rank", "total_score", "weights_version"])


def test_forward_returns_entry_next_open_and_excess():
    bench = frame(np.full(10, 100.0), np.full(10, 101.0))           # 毎日 始値100→終値101（+1%）
    stock = frame(np.full(10, 200.0), np.linspace(200, 290, 10))    # 終値が毎日10円ずつ上がる
    history = history_rows([[DATES[0].strftime("%Y-%m-%d"), "1111", "A", 1, 70, 1]])
    out = forward_returns(history, {"1111": stock}, bench, [1, 5])

    r = out.iloc[0]
    assert r["entry_date"] == DATES[1].date()
    # 1営業日後: 翌日の始値200で買い、同じ日の終値210で売る → +5%
    assert r["ret_1"] == pytest.approx(5.0)
    assert r["bench_1"] == pytest.approx(1.0)
    assert r["excess_1"] == pytest.approx(4.0)
    # 5営業日後: 終値250 → +25%
    assert r["ret_5"] == pytest.approx(25.0)
    assert r["status_5"] == CONFIRMED


def test_forward_returns_pending_and_missing():
    bench = frame(np.full(10, 100.0), np.full(10, 100.0))
    stock = frame(np.full(10, 100.0), np.full(10, 100.0)).drop(DATES[2])   # 3日目は売買停止
    history = history_rows([
        [DATES[7].strftime("%Y-%m-%d"), "1111", "A", 1, 70, 1],   # 5営業日後はまだ来ていない
        [DATES[0].strftime("%Y-%m-%d"), "1111", "A", 1, 70, 1],   # 2営業日後＝売買停止の日
        [DATES[0].strftime("%Y-%m-%d"), "9999", "Z", 2, 60, 1],   # 株価が手元にない銘柄
    ])
    out = forward_returns(history, {"1111": stock}, bench, [1, 2, 5])
    assert out.loc[0, "status_1"] == CONFIRMED
    assert out.loc[0, "status_5"] == PENDING
    assert out.loc[1, "status_2"] == MISSING
    assert out.loc[2, "status_1"] == MISSING


def test_summarize_and_report():
    bench = frame(np.full(10, 100.0), np.full(10, 100.0))
    up = frame(np.full(10, 100.0), np.full(10, 110.0))     # 毎回 +10%
    flat = frame(np.full(10, 100.0), np.full(10, 100.0))   # 毎回 0%
    down = frame(np.full(10, 100.0), np.full(10, 95.0))    # 毎回 −5%
    rows = []
    for d in DATES[:5]:
        day = d.strftime("%Y-%m-%d")
        rows += [[day, "1111", "A", 1, 80, 1], [day, "2222", "B", 2, 60, 1], [day, "3333", "C", 3, 40, 1]]
    out = forward_returns(history_rows(rows), {"1111": up, "2222": flat, "3333": down}, bench, [1])
    s = summarize(out, [1], top_n=1)[1]

    assert s["dates"] == 5
    table = s["table"].set_index("グループ")
    assert table.loc["上位1銘柄", "平均超過リターン(%)"] == pytest.approx(10.0)
    assert table.loc["それ以外", "平均超過リターン(%)"] == pytest.approx(-2.5)
    assert s["spread_mean"] == pytest.approx(12.5)
    assert s["ic_mean"] == pytest.approx(1.0)   # 点数の順と値動きの順が完全に一致

    text = build_evaluation_report({1: s}, "テスト", [1], [], ("2026-10-01", "2026-10-07"), 1, ["注意書き"])
    assert "## 1営業日後" in text and "+12.50%" in text and "> 注意書き" in text


def test_backfill_uses_only_past_data():
    weights = load_weights()
    n = WEEKS52_DAYS + 30
    dates = pd.bdate_range(end="2026-10-30", periods=n, name="date")
    closes = np.concatenate([np.linspace(1000, 1500, n - 10), np.linspace(1500, 800, 10)])  # 最後の10日で急落
    stock = frame(closes, closes, dates)
    universe = pd.DataFrame({"code": ["1111"], "name": ["A"], "sector33": ["x"]})
    run_at = datetime(2026, 10, 30, 18, 0, tzinfo=JST)

    start = default_start(dates)
    assert start == dates[WEEKS52_DAYS - 1]
    target = dates[n - 11:n - 10]   # 急落の前日
    history, log = backfill(universe, {"1111": stock}, target, weights, run_at, "watchlist", progress=lambda _: None)

    assert len(history) == 1 and log["ranked"].iloc[0] == 1
    # 急落前日の時点では、急落後の安値を知らないので高値圏・上昇配列のまま
    assert history["close"].iloc[0] == pytest.approx(1500)
    assert history["ma_alignment"].iloc[0] == 1


def test_benchmark_glitch_shows_as_outliers():
    """TOPIX ETF の株価が1日だけ10倍になっていると、1営業日後は無傷で、5営業日後の平均だけが大きくゆがむ。"""
    from kabu.evaluation import find_outliers
    dates = pd.bdate_range("2026-09-01", periods=40, name="date")
    bench = frame(np.full(40, 100.0), np.full(40, 100.0), dates)
    bench.iloc[20, [0, 3]] = 1000.0   # 21日目だけ始値・終値とも10倍
    stock = frame(np.full(40, 100.0), np.full(40, 100.0), dates)
    rows = [[d.strftime("%Y-%m-%d"), "1111", "A", 1, 70, 1] for d in dates[:30]]
    out = forward_returns(history_rows(rows), {"1111": stock}, bench, [1, 5])
    s = summarize(out, [1, 5], top_n=1)
    assert s[1]["table"]["平均超過リターン(%)"].iloc[0] == pytest.approx(0)
    assert s[5]["table"]["平均超過リターン(%)"].iloc[0] < -20
    assert s[5]["table"]["超過リターンの中央値(%)"].iloc[0] == pytest.approx(0)   # 中央値はゆがまない
    outliers = find_outliers(out, [1, 5], {"1111": stock}, bench, 0.3)
    assert set(outliers["N"]) == {5} and len(outliers) == 2
    assert outliers["data_suspect"].all()   # TOPIX ETF の株価に異常あり → データの誤りを疑う


def test_real_big_move_is_not_data_suspect():
    """毎日 +4% ずつ上がって 20 日で 2 倍超、のような実際にありうる動きはデータ異常にしない。"""
    from kabu.evaluation import find_outliers
    dates = pd.bdate_range("2026-09-01", periods=40, name="date")
    bench = frame(np.full(40, 100.0), np.full(40, 100.0), dates)
    closes = 100 * 1.04 ** np.arange(40)
    stock = frame(closes, closes, dates)
    rows = [[dates[0].strftime("%Y-%m-%d"), "1111", "A", 1, 70, 1]]
    out = forward_returns(history_rows(rows), {"1111": stock}, bench, [20])
    outliers = find_outliers(out, [20], {"1111": stock}, bench, 0.3)
    assert len(outliers) == 1 and not outliers["data_suspect"].iloc[0]
