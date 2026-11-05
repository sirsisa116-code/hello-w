"""テクニカル指標・点数・レポートのテスト（作り物のデータで、ネットにはつながない）。"""

from datetime import datetime

import numpy as np
import pandas as pd
import pytest

from kabu.analysis.technical import compute_indicators, rsi
from kabu.config import JST
from kabu.quality import QualityLog
from kabu.report import append_history, build_report
from kabu.scoring import CategoryScore, combine, load_weights, match_band, rank_stocks, score_technical


def make_prices(closes, volumes=None, end="2026-10-30"):
    closes = np.asarray(closes, dtype=float)
    idx = pd.bdate_range(end=end, periods=len(closes), name="date")
    volumes = np.full(len(closes), 1_000_000.0) if volumes is None else np.asarray(volumes, dtype=float)
    return pd.DataFrame(
        {"open": closes, "high": closes, "low": closes, "close": closes,
         "adj_close": closes, "volume": volumes, "source": "test"},
        index=idx,
    )


# ---------- 指標 ----------

def test_rsi_extremes():
    assert rsi(pd.Series(np.arange(1, 40, dtype=float))).iloc[-1] == 100
    assert rsi(pd.Series(np.full(40, 5.0))).iloc[-1] == 50
    assert rsi(pd.Series(np.arange(40, 1, -1, dtype=float))).iloc[-1] == pytest.approx(0)


def test_indicators_uptrend():
    df = make_prices(np.linspace(1000, 2000, 300))
    ind = compute_indicators(df)
    assert ind["ma_alignment"] == 1
    assert ind["ma25_slope"] > 0
    assert ind["deviation25"] > 0
    assert ind["high52_distance"] == pytest.approx(0)
    assert ind["turnover20_oku"] == pytest.approx(2000 * 1_000_000 / 1e8, rel=0.02)


def test_indicators_volume_sign():
    closes = np.full(60, 1000.0)
    volumes = np.full(60, 100.0)
    volumes[-1] = 300.0
    closes[-1] = 990.0  # 出来高3倍で下落
    ind = compute_indicators(make_prices(closes, volumes))
    assert ind["volume_ratio"] == pytest.approx(3.0)
    assert ind["volume_ratio_signed"] == pytest.approx(-3.0)


def test_indicators_short_history_gives_nan():
    ind = compute_indicators(make_prices(np.linspace(100, 120, 30)))
    assert np.isnan(ind["ma_alignment"])        # 75日分ない
    assert np.isnan(ind["high52_distance"])     # 250日分ない
    assert not np.isnan(ind["rsi14"])


def test_indicators_as_of_ignores_future():
    df = make_prices(np.linspace(1000, 2000, 300))
    as_of = df.index[-11]
    assert compute_indicators(df, as_of=as_of)["close"] == df["close"].iloc[-11]


# ---------- 点数 ----------

BANDS = [
    {"min": 15, "points": -10, "note": "過熱"},
    {"min": 0, "points": 5, "note": "上"},
    {"points": -5, "note": "下"},
]


def test_match_band_boundaries():
    assert match_band(15, BANDS) == (-10, "過熱")    # min は「以上」
    assert match_band(14.9, BANDS) == (5, "上")
    assert match_band(0, BANDS) == (5, "上")
    assert match_band(-0.1, BANDS) == (-5, "下")
    assert match_band(5, [{"max": 3, "points": 1, "note": "x"}])[0] == 0  # どれにも当たらない


def test_weights_file_is_valid():
    weights = load_weights()
    assert weights["category_weights"]["technical"] > 0


def test_score_technical_missing_indicators():
    conf = {"min_available_ratio": 0.5, "rules": {"rsi14": [{"points": 4, "note": "a"}],
                                                   "deviation25": [{"points": 6, "note": "b"}]}}
    full = score_technical({"rsi14": 40, "deviation25": 3}, conf)
    assert full.score == 60 and not full.missing
    half = score_technical({"rsi14": 40, "deviation25": np.nan}, conf)
    assert half.score == 54 and half.missing == ["25日線乖離率(%)"]
    none = score_technical({"rsi14": np.nan, "deviation25": np.nan}, {**conf, "min_available_ratio": 0.6})
    assert none.score is None


def test_combine_reweights_when_missing():
    cats = {"technical": CategoryScore("technical", 70.0), "news": CategoryScore("news", None)}
    total, used, missing = combine(cats, {"technical": 1.0, "fundamental": 0, "news": 1.0})
    assert total == 70.0 and used == ["technical"] and missing == ["news"]
    total, _, _ = combine({"technical": CategoryScore("technical", 70.0),
                           "fundamental": CategoryScore("fundamental", 40.0)},
                          {"technical": 2.0, "fundamental": 1.0})
    assert total == pytest.approx(60.0)


# ---------- ランキングとレポート ----------

def test_rank_stocks_orders_and_excludes(tmp_path):
    weights = load_weights()
    universe = pd.DataFrame({"code": ["1111", "2222", "3333", "4444"],
                             "name": ["上昇", "下落", "古い", "薄商い"],
                             "sector33": ["a", "b", "c", "d"]})
    prices = {
        "1111": make_prices(np.linspace(1000, 1500, 300)),
        "2222": make_prices(np.linspace(1500, 1000, 300)),
        "3333": make_prices(np.linspace(1000, 1500, 300), end="2026-10-29"),
        "4444": make_prices(np.linspace(1000, 1500, 300), volumes=np.full(300, 100.0)),
    }
    quality = QualityLog()
    reference = pd.Timestamp("2026-10-30")
    ranking, details, excluded = rank_stocks(universe, prices, reference, weights, quality)

    assert list(ranking["code"]) == ["1111", "2222"]
    assert ranking["total_score"].iloc[0] > ranking["total_score"].iloc[1]
    assert set(excluded["code"]) == {"3333", "4444"}
    assert any("上昇配列" in c.note for c in details["1111"]["technical"].contributions)

    run_at = datetime(2026, 10, 30, 18, 0, tzinfo=JST)
    text = build_report(ranking, details, excluded, quality, reference, run_at, weights, top_n=10)
    assert "2026-10-30（金）" in text
    assert "1位 1111 上昇" in text
    assert "3333 古い: 株価が古い" in text

    history = tmp_path / "history.csv"
    append_history(ranking, history, reference, run_at, 1, "watchlist")
    append_history(ranking, history, reference, run_at, 1, "watchlist")   # 同じ日をやり直しても重複しない
    append_history(ranking, history, pd.Timestamp("2026-11-02"), run_at, 1, "watchlist")
    saved = pd.read_csv(history, dtype={"code": str}, encoding="utf-8-sig")
    assert len(saved) == 4
    assert saved["code"].iloc[0] == "1111"


def test_indicator_series_uses_only_past_data():
    """全期間まとめて計算した値と、その日までで切って計算した値が一致する（未来を見ていない）。"""
    from kabu.analysis.technical import compute_indicator_series, indicators_at
    rng = np.random.default_rng(3)
    df = make_prices(1000 * np.exp(np.cumsum(rng.normal(0, 0.02, 320))),
                     volumes=rng.integers(1, 3_000_000, 320))
    series = compute_indicator_series(df)
    for pos in (10, 30, 80, 260, 319):
        as_of = df.index[pos]
        expected = compute_indicators(df[df.index <= as_of])
        actual = indicators_at(series, as_of)
        for key, value in expected.items():
            assert (np.isnan(value) and np.isnan(actual[key])) or actual[key] == pytest.approx(value)


def test_universe_label():
    from kabu.data.universe import universe_label
    assert universe_label({"universe": {"mode": "watchlist"}}) == "watchlist"
    conf = {"universe": {"mode": "prime", "prime_scales": ["TOPIX Core30", "TOPIX Large70", "TOPIX Mid400"]}}
    assert universe_label(conf) == "prime:Core30+Large70+Mid400"


def test_tie_notes():
    from kabu.report import tie_notes
    ranking = pd.DataFrame({"total_score": [66.5, 66.5, 66.5, 60.0, 55.0]})
    notes = tie_notes(ranking, top_n=2)
    assert len(notes) == 2 and "66.5 点は 3 銘柄が同点" in notes[1] and "そのうち 2 銘柄" in notes[1]
    assert tie_notes(pd.DataFrame({"total_score": [70.0, 60.0]}), top_n=2) == []
