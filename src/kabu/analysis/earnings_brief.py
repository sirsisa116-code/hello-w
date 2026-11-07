"""保存済みの XBRL の数字（data/xbrl/key_figures.csv）から、決算発表の中身を1行にまとめる。

決算短信（四半期・中間期）:  営業利益の実績と前年同期比、通期予想、通期予想に対する進捗率、
                            前回の予想からの変化（手元に前回の数字があれば）
決算短信（本決算）:          営業利益の実績と前期比、直前の会社予想との差（上振れ・下振れ）、来期予想と伸び率
業績予想の修正:              修正前 → 修正後（xbrl_figures.revision_summary）

利益は 営業利益 → 経常利益 → 税引前利益 → 純利益 の順で、数字がある最初のものを使う。
「前回の予想」は、この開示より前に保存した同じ会社の数字から探す。保存を始める前の分は分からないので、
その場合は表示しない（黙って推測しない）。
"""

import numpy as np
import pandas as pd

from kabu.data.xbrl_figures import ITEM_LABELS, revision_summary

PROFIT_ORDER = ["operating_income", "ordinary_income", "pretax_income", "net_income"]
LOOKBACK_DAYS = 400   # 前回の予想を探す範囲


def _pick(g: pd.DataFrame, period: str, kind: str, stage: str = "") -> dict:
    """期間・種類（実績/予想）・修正前後が合う数字を {項目: 値} で返す。連結を優先し、なければ個別。"""
    sub = g[(g["period"] == period) & (g["kind"] == kind) & (g["stage"] == stage)]
    for consolidated in ("連結", "個別", ""):
        s = sub[sub["consolidated"] == consolidated]
        if not s.empty:
            return dict(zip(s["item"], s["value"]))
    return {}


def is_annual(doc_type: str) -> bool:
    """本決算の決算短信か（書類の種類が a で始まる。例 acedjpsm・acedifsm）。"""
    return doc_type.startswith("a") and doc_type != "rvfc"


def _forecast_of(g: pd.DataFrame, doc_type: str, for_next_year: bool) -> dict:
    if doc_type == "rvfc":
        return _pick(g, "CurrentYearDuration", "forecast", "current")
    if for_next_year:
        return _pick(g, "NextYearDuration", "forecast")
    return _pick(g, "CurrentYearDuration", "forecast")


def previous_forecast(history: pd.DataFrame, before: pd.Timestamp, metric: str) -> tuple[float, str] | None:
    """この開示より前の、同じ決算期についての一番新しい通期予想（値, どの開示からか）。

    さかのぼって最初に見つかった書類が本決算なら、そこで出た「来期予想」が今期の最初の予想になる。
    """
    earlier = history[(history["pubdate"] < before) & (history["pubdate"] >= before - pd.Timedelta(days=LOOKBACK_DAYS))]
    for doc_id, g in sorted(earlier.groupby("id"), key=lambda x: x[1]["pubdate"].iloc[0], reverse=True):
        doc_type = g["doc_type"].iloc[0]
        forecast = _forecast_of(g, doc_type, for_next_year=is_annual(doc_type))
        if metric in forecast and not np.isnan(forecast[metric]):
            label = "本決算時の来期予想" if is_annual(doc_type) else "業績予想の修正" if doc_type == "rvfc" else "前回の決算短信"
            return float(forecast[metric]), f"{g['pubdate'].iloc[0]:%Y-%m-%d}の{label}"
        if is_annual(doc_type):
            return None   # 本決算より前は別の決算期なので、それ以上さかのぼらない
    return None


def _pct(a: float, b: float) -> str:
    return f"{(a / b - 1) * 100:+.1f}%" if b and b > 0 else "—"


def _oku(value: float) -> str:
    return f"{value / 1e8:,.0f}億円" if abs(value) >= 1e10 else f"{value / 1e6:,.0f}百万円"


def earnings_brief(figures: pd.DataFrame, disclosure_id: str) -> tuple[str, float | None]:
    """1件の開示の中身を1行にまとめる。戻り値は (文章, 予想の変化率 or 上振れ率)。
    figures は同じ会社の保存済みの数字すべて（この開示より前のものも含む）。"""
    this = figures[figures["id"] == disclosure_id]
    if this.empty:
        return "", None
    doc_type = this["doc_type"].iloc[0]
    if doc_type == "rvfc":
        from kabu.data.xbrl_figures import revision_change
        return revision_summary(this), revision_change(this)

    annual = is_annual(doc_type)
    periods = [p for p in this.loc[this["kind"] == "result", "period"].unique()
               if p.startswith("Current") and p.endswith("Duration")]
    if not periods:
        return "", None
    period = "CurrentYearDuration" if annual and "CurrentYearDuration" in periods else periods[0]
    actual = _pick(this, period, "result")
    prior = _pick(this, period.replace("Current", "Prior", 1), "result")
    metric = next((m for m in PROFIT_ORDER if m in actual and not np.isnan(actual[m])), None)
    if metric is None:
        return "", None

    label = ITEM_LABELS[metric]
    value = actual[metric]
    parts = [f"{label} {_oku(value)}（{'前期比' if annual else '前年同期比'} {_pct(value, prior.get(metric, np.nan))}）"]
    pubdate = this["pubdate"].iloc[0]
    previous = previous_forecast(figures, pubdate, metric)
    change = None

    if annual:
        if previous:
            change = value / previous[0] - 1 if previous[0] > 0 else None
            parts.append(f"直前の会社予想 {_oku(previous[0])} に対し {_pct(value, previous[0])}")
        next_year = _forecast_of(this, doc_type, for_next_year=True)
        if metric in next_year:
            parts.append(f"来期予想 {_oku(next_year[metric])}（{_pct(next_year[metric], value)}）")
    else:
        full_year = _forecast_of(this, doc_type, for_next_year=False)
        if metric in full_year and full_year[metric] > 0:
            parts.append(f"通期予想 {_oku(full_year[metric])}、進捗率 {value / full_year[metric] * 100:.0f}%")
            if previous:
                change = full_year[metric] / previous[0] - 1 if previous[0] > 0 else None
                if change is not None and abs(change) > 1e-9:
                    parts.append(f"通期予想を {previous[1]}から {_pct(full_year[metric], previous[0])} 修正")
                elif change is not None:
                    parts.append("通期予想は据え置き")
    return " / ".join(parts), change
