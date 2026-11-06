"""XBRL から、売上・利益・予想・配当などの主要な数字だけを取り出す。

項目名は会計基準で違う（実物で確認したもの）:
  日本基準: NetSales（会社によって OperatingRevenues）、OperatingIncome、OrdinaryIncome、
            ProfitAttributableToOwnersOfParent、NetIncomePerShare
  IFRS:     SalesIFRS、OperatingIncomeIFRS、ProfitBeforeTaxIFRS、
            ProfitAttributableToOwnersOfParentIFRS、BasicEarningsPerShareIFRS
  米国基準・銀行は実物で未確認。見つからない項目は取り出さない（黙って別の項目で代用しない）。

contextRef（どの期間の、どんな数字か）の読み方:
  例 CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember
     → 今期・通期 / 連結 / 修正前 / 予想
  期間: CurrentYearDuration（今期通期）、NextYearDuration（来期通期）、CurrentAccumulatedQ2Duration（今期の
        第2四半期累計）、Prior〜（前期・前年同期）、〜Instant（期末時点）
  修正前後: PreviousMember（修正前）、CurrentMember（修正後）。業績予想の修正の書類だけに付く
  種類: ResultMember（実績）、ForecastMember（予想）、UpperMember / LowerMember（予想の上限・下限）
"""

import pandas as pd

# 取り出す項目 → 会計基準ごとの名前（tse-ed-t: の後ろ）
ITEMS = {
    "revenue": ["NetSales", "OperatingRevenues", "SalesIFRS", "RevenueIFRS", "NetSalesUS", "OperatingRevenuesUS"],
    "operating_income": ["OperatingIncome", "OperatingIncomeIFRS", "OperatingIncomeUS"],
    "ordinary_income": ["OrdinaryIncome"],
    "pretax_income": ["ProfitBeforeTaxIFRS", "IncomeBeforeIncomeTaxesUS"],
    "net_income": ["ProfitAttributableToOwnersOfParent", "ProfitAttributableToOwnersOfParentIFRS",
                   "NetIncomeAttributableToOwnersOfParentUS"],
    "eps": ["NetIncomePerShare", "BasicEarningsPerShareIFRS", "BasicNetIncomePerShareUS"],
    "dividend_per_share": ["DividendPerShare"],
}
NAME_TO_ITEM = {name: item for item, names in ITEMS.items() for name in names}
ITEM_LABELS = {"revenue": "売上", "operating_income": "営業利益", "ordinary_income": "経常利益",
               "pretax_income": "税引前利益", "net_income": "純利益", "eps": "1株利益", "dividend_per_share": "1株配当"}

KINDS = {"ResultMember": "result", "ForecastMember": "forecast", "UpperMember": "upper", "LowerMember": "lower"}
STAGES = {"PreviousMember": "previous", "CurrentMember": "current"}
TIMINGS = ("FirstQuarterMember", "SecondQuarterMember", "ThirdQuarterMember", "YearEndMember", "AnnualMember")
FIGURE_COLUMNS = ["item", "period", "consolidated", "stage", "kind", "timing", "value", "name", "doc_type"]


def parse_context(context: str) -> dict:
    """contextRef を、期間・連結かどうか・修正前後・実績か予想か・配当の時期に分ける。"""
    tokens = context.split("_")
    consolidated = ("連結" if "ConsolidatedMember" in tokens
                    else "個別" if "NonConsolidatedMember" in tokens else "")
    stage = next((STAGES[t] for t in tokens if t in STAGES), "")
    kind = next((KINDS[t] for t in tokens if t in KINDS), "")
    timing = next((t.removesuffix("Member") for t in tokens if t in TIMINGS), "")
    return {"period": tokens[0] if tokens else "", "consolidated": consolidated, "stage": stage,
            "kind": kind, "timing": timing}


def is_summary_or_revision(doc_type: str) -> bool:
    """決算短信のサマリー（例 scedjpsy・acedifsm）か、業績予想の修正（rvfc）か。
    財務諸表の本体（〜fr）は項目が多すぎるので、今は使わない。"""
    return doc_type == "rvfc" or (len(doc_type) == 8 and doc_type[1:4] == "ced" and doc_type[6] == "s")


def extract_key_figures(facts: pd.DataFrame) -> pd.DataFrame:
    """read_xbrl_zip の結果から、主要な数字だけを取り出す（値のないものは捨てる）。"""
    if facts.empty:
        return pd.DataFrame(columns=FIGURE_COLUMNS)
    target = facts[(facts["kind"] == "number") & (facts["prefix"] == "tse-ed-t")
                   & facts["value"].notna() & facts["doc_type"].map(is_summary_or_revision)]
    rows = []
    for f in target.itertuples(index=False):
        item = NAME_TO_ITEM.get(f.name)
        if item is None:
            continue
        rows.append({"item": item, **parse_context(f.context), "value": f.value, "name": f.name,
                     "doc_type": f.doc_type})
    return pd.DataFrame(rows, columns=FIGURE_COLUMNS)


def revision_summary(figures: pd.DataFrame) -> str:
    """業績予想の修正の中身を1行の文章にする（例: 通期営業利益 1,150→1,205百万円（+4.8%））。
    修正前・修正後の両方がそろう項目だけを使う。そろわなければ空文字。"""
    rv = figures[(figures["doc_type"] == "rvfc") & (figures["kind"] == "forecast")]
    if rv.empty:
        return ""
    # 通期を優先し、なければ第2四半期累計など。連結を優先し、なければ個別
    periods = sorted(rv["period"].unique(), key=lambda p: (p != "CurrentYearDuration", p))
    for period in periods:
        for consolidated in ("連結", "個別", ""):
            g = rv[(rv["period"] == period) & (rv["consolidated"] == consolidated)]
            parts = []
            for item in ("operating_income", "ordinary_income", "pretax_income", "net_income", "revenue"):
                before = g[(g["item"] == item) & (g["stage"] == "previous")]["value"]
                after = g[(g["item"] == item) & (g["stage"] == "current")]["value"]
                if before.empty or after.empty:
                    continue
                b, a = before.iloc[0], after.iloc[0]
                change = f"（{(a / b - 1) * 100:+.1f}%）" if b > 0 else "（前回予想が0以下のため率は出さない）"
                parts.append(f"{ITEM_LABELS[item]} {b / 1e6:,.0f}→{a / 1e6:,.0f}百万円{change}")
                if len(parts) == 2:
                    break
            if parts:
                label = "通期" if period == "CurrentYearDuration" else "中間期" if "Q2" in period else period
                return f"{label}{consolidated}予想: " + " / ".join(parts)
    return ""


def revision_change(figures: pd.DataFrame) -> float | None:
    """業績予想の修正で、通期の利益予想が何%変わったか。

    営業利益 → 経常利益 → 税引前利益 → 純利益 の順に見て、最初に変化があった利益の変化率を返す
    （営業利益は据え置きで純利益だけ修正、といった場合も修正として扱うため）。
    どの利益も変わっていなければ 0.0（据え置き）。前回予想が0以下の利益は率に意味がないので飛ばし、
    比べられる利益が1つもなければ None。"""
    rv = figures[(figures["doc_type"] == "rvfc") & (figures["kind"] == "forecast")
                 & (figures["period"] == "CurrentYearDuration")]
    for consolidated in ("連結", "個別", ""):
        g = rv[rv["consolidated"] == consolidated]
        changes = []
        for item in ("operating_income", "ordinary_income", "pretax_income", "net_income"):
            before = g[(g["item"] == item) & (g["stage"] == "previous")]["value"]
            after = g[(g["item"] == item) & (g["stage"] == "current")]["value"]
            if not before.empty and not after.empty and before.iloc[0] > 0:
                changes.append(after.iloc[0] / before.iloc[0] - 1)
        if changes:
            return next((c for c in changes if abs(c) > 1e-9), 0.0)
    return None
