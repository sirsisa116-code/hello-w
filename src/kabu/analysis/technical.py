"""日足の株価からテクニカル指標を計算する。

計算はすべてこのファイルの中で完結し、ネットにはつながない。

compute_indicator_series は、全営業日について指標をまとめて計算する。
各日の値はその日までの株価だけから計算される（移動平均などはすべて過去方向にしか見ない）ので、
ある日の行を取り出せば「その日に分かっていたことだけ」で計算した値になる。
後付けの再現では、銘柄ごとに一度だけ計算して、日付ごとに行を取り出して使う。

データが足りずに計算できない指標は NaN（値なし）にする。
NaN をどう扱うか（点数0にして「欠損」と表示する）は scoring.py が決める。
"""

import numpy as np
import pandas as pd

# 指標ごとの表示名と、値の書き方。レポートで使う。
INDICATORS = {
    "ma_alignment":        ("移動平均の並び", "{:+.0f}"),       # +1 上昇配列 / -1 下降配列 / 0 どちらでもない
    "ma25_slope":          ("25日線の傾き(5日間,%)", "{:+.2f}"),
    "deviation25":         ("25日線乖離率(%)", "{:+.1f}"),
    "rsi14":               ("RSI(14)", "{:.0f}"),
    "volume_ratio_signed": ("出来高倍率(下落日はマイナス)", "{:+.2f}"),
    "high52_distance":     ("52週高値からの距離(%)", "{:+.1f}"),
}

# ルールには使わないが、レポートや絞り込みで使う値
EXTRA_VALUES = ["close", "ma5", "ma25", "ma75", "volume_ratio", "day_change_pct", "turnover20_oku"]

RSI_PERIOD = 14
VOLUME_AVG_DAYS = 20
WEEKS52_DAYS = 250   # 52週 ≒ 250営業日


def rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """RSI（0〜100）。直近の上げ幅と下げ幅の比率から、買われすぎ・売られすぎを見る指標。
    平均の取り方は一般的なワイルダー方式（指数平滑）。"""
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    value = 100 - 100 / (1 + gain / loss)
    # 下げが一度もない → 100、値動きが全くない → 50（中立）
    value = value.mask((loss == 0) & (gain > 0), 100.0)
    value = value.mask((loss == 0) & (gain == 0), 50.0)
    return value


def compute_indicator_series(prices: pd.DataFrame) -> pd.DataFrame:
    """1銘柄の株価表から、全営業日の指標を表にして返す（行＝日付、列＝指標）。
    履歴が足りない日の値は NaN になる（例: 75日線は75日分たまるまで NaN）。"""
    close = prices["close"]
    volume = prices["volume"]
    ma5 = close.rolling(5).mean()
    ma25 = close.rolling(25).mean()
    ma75 = close.rolling(75).mean()

    day_change = (close / close.shift(1) - 1) * 100
    alignment = np.select([(ma5 > ma25) & (ma25 > ma75), (ma5 < ma25) & (ma25 < ma75)], [1.0, -1.0], 0.0)
    # 当日を含めない直前20日の平均と比べる
    previous_avg = volume.shift(1).rolling(VOLUME_AVG_DAYS).mean()
    ratio = (volume / previous_avg).where(previous_avg > 0)

    out = pd.DataFrame({
        "close": close,
        "ma5": ma5,
        "ma25": ma25,
        "ma75": ma75,
        "day_change_pct": day_change,
        "ma_alignment": pd.Series(alignment, index=prices.index).where(ma75.notna()),
        "ma25_slope": (ma25 / ma25.shift(5) - 1) * 100,
        "deviation25": (close / ma25 - 1) * 100,
        "rsi14": rsi(close),
        "volume_ratio": ratio,
        # 出来高を伴う上昇は良い、出来高を伴う下落は悪い、と区別するため符号を付ける
        "volume_ratio_signed": ratio.where(day_change >= 0, -ratio),
        "turnover20_oku": (close * volume).rolling(VOLUME_AVG_DAYS).mean() / 1e8,
        "high52_distance": (close / prices["high"].rolling(WEEKS52_DAYS).max() - 1) * 100,
    })
    return out


def indicators_at(series: pd.DataFrame, as_of: pd.Timestamp | None = None) -> dict:
    """compute_indicator_series の結果から、as_of の日（なければそれ以前で一番近い日）の値を取り出す。"""
    keys = list(INDICATORS) + EXTRA_VALUES
    pos = len(series) if as_of is None else series.index.searchsorted(as_of, side="right")
    if pos == 0:
        return {k: np.nan for k in keys}
    row = series.iloc[pos - 1]
    return {k: row[k] for k in keys}


def compute_indicators(prices: pd.DataFrame, as_of: pd.Timestamp | None = None) -> dict:
    """1銘柄の株価表から、最新日（または as_of の日）の指標を辞書で返す。"""
    if as_of is not None:
        prices = prices[prices.index <= as_of]
    return indicators_at(compute_indicator_series(prices))
