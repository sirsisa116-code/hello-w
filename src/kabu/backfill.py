"""過去の日付のランキングを、後から再現する（後付けの再現）。

毎日の実行（run_daily.py）と同じ点数計算を、過去の各営業日を基準日にして繰り返す。
指標は「基準日までの株価」だけで計算するので、未来の株価は混ざらない。

ただし、次の偏りは避けられない（レポートにも書く）:
  - 銘柄は「今の」対象銘柄を使う。途中で上場廃止になった銘柄や、
    今は条件から外れた銘柄が入らないので、成績が良く見えやすい
    （TOPIX500 なら「今500位以内に入っている＝これまで株価が上がってきた」銘柄に偏る）
  - ニュースは過去にさかのぼれないので、テクニカル（と将来のファンダメンタル）だけ
"""

from datetime import datetime

import pandas as pd

from kabu.analysis.technical import WEEKS52_DAYS, compute_indicator_series
from kabu.quality import QualityLog
from kabu.scoring import rank_stocks


def default_start(calendar: pd.DatetimeIndex) -> pd.Timestamp:
    """52週高値の計算に必要な履歴がそろう最初の営業日。"""
    if len(calendar) < WEEKS52_DAYS:
        raise ValueError(f"TOPIX ETF の履歴が {len(calendar)} 日分しかなく、再現に必要な {WEEKS52_DAYS} 日に足りません")
    return calendar[WEEKS52_DAYS - 1]


def backfill(
    universe: pd.DataFrame,
    prices: dict[str, pd.DataFrame],
    dates: pd.DatetimeIndex,
    weights: dict,
    run_at: datetime,
    universe_label: str,
    progress=print,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """各日付のランキングを作り、history.csv と同じ形の表にして返す。
    戻り値は (ランキングの履歴, 日ごとの記録: 何銘柄入ったか・指標の欠損が何件あったか)。"""
    frames, log = [], []
    # 指標は銘柄ごとに一度だけ全期間を計算しておき、日付ごとに行を取り出す
    series = {code: compute_indicator_series(df) for code, df in prices.items()}
    for i, date in enumerate(dates, 1):
        quality = QualityLog()  # 日ごとの警告は件数だけ数える（全部出すと多すぎるため）
        ranking, _, excluded = rank_stocks(universe, prices, date, weights, quality, series)
        log.append({"data_date": date.date(), "ranked": len(ranking), "excluded": len(excluded),
                    "missing_indicator_warnings": quality.count("WARN")})
        if not ranking.empty:
            ranking.insert(0, "data_date", date.strftime("%Y-%m-%d"))
            ranking.insert(1, "run_at", run_at.strftime("%Y-%m-%d %H:%M"))
            ranking.insert(2, "weights_version", weights["version"])
            ranking.insert(3, "universe", universe_label)
            frames.append(ranking)
        if i % 20 == 0 or i == len(dates):
            progress(f"  {i} / {len(dates)} 日 処理済み（{date:%Y-%m-%d}）")

    history = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return history, pd.DataFrame(log)
