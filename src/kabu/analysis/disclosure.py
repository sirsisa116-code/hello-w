"""適時開示のタイトルを決まった言葉で分類し、点数にする。

分類の決まりは config/weights.yaml の disclosure に書いてある。ここでは当てはめるだけ。
タイトルしか見ないので、「業績予想の修正」のように上か下か書いていないものは方向が分からない。
そうしたものは点数0で表示だけする（中身の判断は、後で上位候補だけ Claude に任せる予定）。

未来の情報を混ぜないため、基準日 D の点数には「D の 23:59 までに出た開示」だけを使う。
ランキングは引け後に作り、買うのは翌営業日の寄り付きなので、D の夜の開示までは買う前に分かっている。
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from kabu.scoring_types import CategoryScore, Contribution


def classify_title(title: str, conf: dict) -> list[dict]:
    """1件のタイトルに当てはまる決まりを全部返す（例: 上方修正と増配の両方、もありうる）。
    include のどれかを含み、exclude のどれも含まないときに当てはまる（正規表現ではなく、ただの文字列）。"""
    if any(word in title for word in conf.get("ignore", [])):
        return []
    matched = []
    for rule in conf["rules"]:
        if any(w in title for w in rule["include"]) and not any(w in title for w in rule.get("exclude", [])):
            matched.append(rule)
    return matched


def classify_disclosures(disclosures: pd.DataFrame, conf: dict) -> pd.DataFrame:
    """開示の表に分類を付ける。当てはまる決まりがないものは捨て、複数当てはまるものは行を分ける。
    付ける列: category（分類名）、label（表示名）、points（満点の点数）"""
    rows = []
    for item in disclosures.itertuples(index=False):
        for rule in classify_title(item.title, conf):
            rows.append({**item._asdict(), "category": rule["category"], "label": rule["label"],
                         "points": float(rule["points"])})
    columns = list(disclosures.columns) + ["category", "label", "points"]
    return pd.DataFrame(rows, columns=columns)


def group_by_code(classified: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """銘柄ごとに分け、開示の時刻順に並べる（日付ごとに何度も取り出すので先に分けておく）。"""
    return {code: g.sort_values("pubdate").reset_index(drop=True) for code, g in classified.groupby("code")}


def missing_in_window(reference: pd.Timestamp, decay_days: int, failed_days: list[date]) -> list[date]:
    """点数に使う期間のうち、開示を取れなかった日。"""
    first = (reference - timedelta(days=decay_days - 1)).date()
    return [d for d in failed_days if first <= d <= reference.date()]


def score_disclosures(
    events: pd.DataFrame | None, reference: pd.Timestamp, conf: dict, failed_days: list[date]
) -> CategoryScore:
    """1銘柄の適時開示の点数。50 + Σ（決まりの点数 × 日がたつほど小さくなる重み）を 0〜100 に収める。

    重み = 1 − 開示からの日数 ÷ decay_days（開示当日が1、decay_days 日で0）
    点数に使う期間に開示を取れなかった日があれば「欠損」にする（開示がなかったのか、取れなかったのか区別できないため）。
    """
    decay_days = conf["decay_days"]
    result = CategoryScore("disclosure", None)
    missing = missing_in_window(reference, decay_days, failed_days)
    if missing:
        result.missing.append(f"適時開示（{missing[0]} ほか {len(missing)} 日分を取得できず）")
        return result

    end = reference + pd.Timedelta(hours=23, minutes=59, seconds=59)
    total = 50.0
    if events is not None and not events.empty:
        times = events["pubdate"].to_numpy()
        lo = np.searchsorted(times, np.datetime64(end - pd.Timedelta(days=decay_days)), side="right")
        hi = np.searchsorted(times, np.datetime64(end), side="right")
        for e in events.iloc[lo:hi].itertuples(index=False):
            age = (reference.normalize() - e.pubdate.normalize()).days
            weight = max(0.0, 1 - age / decay_days)
            points = e.points * weight
            total += points
            title = e.title if len(e.title) <= 40 else e.title[:39] + "…"
            detail = getattr(e, "detail", None)
            if isinstance(detail, str) and detail:
                title = detail   # XBRL から中身（修正前後の予想）が分かっていれば、タイトルの代わりに表示する
            result.contributions.append(Contribution(
                e.category, f"{e.pubdate:%m/%d} {e.label}", float(age), title, points,
                f"開示から{age}日（重み{weight:.0%}）",
            ))
    result.score = float(min(100.0, max(0.0, total)))
    return result
