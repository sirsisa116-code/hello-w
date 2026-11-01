"""日足の株価からテクニカル指標を計算する。

計算はすべてこのファイルの中で完結し、ネットにはつながない。
as_of（基準日）を渡すと、その日までのデータだけで計算する。
バックテストで「その日に分かっていたことだけ」を使うための仕組み。

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


def compute_indicators(prices: pd.DataFrame, as_of: pd.Timestamp | None = None) -> dict:
    """1銘柄の株価表から、最新日（または as_of の日）の指標を辞書で返す。"""
    if as_of is not None:
        prices = prices[prices.index <= as_of]
    keys = list(INDICATORS) + EXTRA_VALUES
    result = {k: np.nan for k in keys}
    if len(prices) < 2:
        return result

    close = prices["close"]
    volume = prices["volume"]
    n = len(prices)
    last_close = close.iloc[-1]

    ma5 = close.rolling(5).mean()
    ma25 = close.rolling(25).mean()
    ma75 = close.rolling(75).mean()

    result["close"] = last_close
    result["ma5"] = ma5.iloc[-1]
    result["ma25"] = ma25.iloc[-1]
    result["ma75"] = ma75.iloc[-1]
    result["day_change_pct"] = (last_close / close.iloc[-2] - 1) * 100

    if n >= 75:
        m5, m25, m75 = ma5.iloc[-1], ma25.iloc[-1], ma75.iloc[-1]
        result["ma_alignment"] = 1.0 if m5 > m25 > m75 else -1.0 if m5 < m25 < m75 else 0.0
    if n >= 30:
        result["ma25_slope"] = (ma25.iloc[-1] / ma25.iloc[-6] - 1) * 100
    if n >= 25:
        result["deviation25"] = (last_close / ma25.iloc[-1] - 1) * 100
    if n > RSI_PERIOD:
        result["rsi14"] = rsi(close).iloc[-1]

    if n > VOLUME_AVG_DAYS:
        # 当日を含めない直前20日の平均と比べる
        avg = volume.iloc[-VOLUME_AVG_DAYS - 1:-1].mean()
        if avg > 0:
            ratio = volume.iloc[-1] / avg
            result["volume_ratio"] = ratio
            # 出来高を伴う上昇は良い、出来高を伴う下落は悪い、と区別するため符号を付ける
            result["volume_ratio_signed"] = ratio if result["day_change_pct"] >= 0 else -ratio
    if n >= VOLUME_AVG_DAYS:
        turnover = (close * volume).iloc[-VOLUME_AVG_DAYS:].mean()
        result["turnover20_oku"] = turnover / 1e8
    if n >= WEEKS52_DAYS:
        high52 = prices["high"].iloc[-WEEKS52_DAYS:].max()
        result["high52_distance"] = (last_close / high52 - 1) * 100

    return result
