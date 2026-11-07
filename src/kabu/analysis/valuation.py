"""予想PER・PBR・予想配当利回りを計算する。

  予想PER        = 株価 ÷ 会社予想の1株利益（今期）。予想が0以下（赤字予想）なら出さない
  PBR            = 株価 ÷ 1株あたり純資産。直接の値がなければ 自己資本 ÷（発行済株式数 − 自己株式数）
  予想配当利回り = 会社予想の年間配当 ÷ 株価

数字は2か所から取り、銘柄ごと・項目ごとに、開示日が新しいほうを使う:
  TDnet の XBRL（data/xbrl/key_figures.csv）  新しいが、保存を始めてからの分しかない
  J-Quants（data/raw/jquants/）               数が多いが、無料プランは12週間遅れ

本決算の開示に載っている予想は「来期」の予想（その時点で今期は終わっている）なので、そちらを今期の予想として使う。
注意: 開示のあとに株式分割があると、1株あたりの数字が古いままになり、PER・PBR が分割の比率だけずれる。
"""

import numpy as np
import pandas as pd

from kabu.analysis.earnings_brief import is_annual

INPUT_COLUMNS = ["code", "metric", "value", "date", "source"]


def _value(g: pd.DataFrame, item: str, periods, kind: str, stage: str = "", timing: str | None = None) -> float:
    """条件に合う数字を1つ返す（連結を優先し、なければ個別）。periods は期間名か、期間名を受け取って真偽を返す関数。"""
    match = periods if callable(periods) else (lambda p: p == periods)
    sub = g[(g["item"] == item) & g["period"].map(match) & (g["kind"] == kind) & (g["stage"] == stage)]
    if timing is not None:
        sub = sub[sub["timing"] == timing]
    for consolidated in ("連結", "個別", ""):
        s = sub[sub["consolidated"] == consolidated]["value"].dropna()
        if not s.empty:
            return float(s.iloc[0])
    return np.nan


def _current_instant(period: str) -> bool:
    return period.startswith("Current") and period.endswith("Instant")


def xbrl_inputs(figures: pd.DataFrame) -> pd.DataFrame:
    """XBRL の保存分から、開示ごとの 予想1株利益（eps）・1株純資産（bps）・予想年間配当（dps）を取り出す。"""
    rows = []
    for (code, doc_id), g in figures.groupby(["code", "id"]):
        doc_type, date = g["doc_type"].iloc[0], g["pubdate"].iloc[0]
        if doc_type == "rvfc":
            eps = _value(g, "eps", "CurrentYearDuration", "forecast", stage="current")
            dps, bps = np.nan, np.nan
        else:
            period = "NextYearDuration" if is_annual(doc_type) else "CurrentYearDuration"
            eps = _value(g, "eps", period, "forecast")
            dps = _value(g, "dividend_per_share", period, "forecast", timing="Annual")
            bps = _value(g, "bps", _current_instant, "result")
            if np.isnan(bps):
                equity = _value(g, "equity", _current_instant, "result")
                issued = _value(g, "shares_issued", _current_instant, "result")
                treasury = _value(g, "treasury_shares", _current_instant, "result")
                shares = issued - (0 if np.isnan(treasury) else treasury)
                bps = equity / shares if shares > 0 else np.nan
        for metric, value in (("eps", eps), ("bps", bps), ("dps", dps)):
            if not np.isnan(value):
                rows.append({"code": code, "metric": metric, "value": value, "date": date, "source": "XBRL"})
    return pd.DataFrame(rows, columns=INPUT_COLUMNS)


def _num(row: dict, *names: str) -> float:
    for name in names:
        value = row.get(name)
        if value is not None and not (isinstance(value, float) and np.isnan(value)):
            return float(value)
    return np.nan


def jquants_inputs(statements: pd.DataFrame) -> pd.DataFrame:
    """J-Quants の決算データから、書類ごとの eps・bps・dps を取り出す（項目名は実物で確認したもの）。"""
    rows = []
    for row in statements.to_dict("records"):
        doc_type = str(row.get("DocType", ""))
        annual = row.get("CurPerType") == "FY" and "FinancialStatements" in doc_type
        if annual:
            eps = _num(row, "NxFEPS", "NxFNCEPS")
            dps = _num(row, "NxFDivAnn")
        else:
            eps = _num(row, "FEPS", "FNCEPS")
            dps = _num(row, "FDivAnn")
        bps = np.nan
        if "FinancialStatements" in doc_type:
            bps = _num(row, "BPS", "NCBPS")
            if np.isnan(bps):
                equity = _num(row, "ShEq")
                shares = _num(row, "ShOutFY") - (_num(row, "TrShFY") if not np.isnan(_num(row, "TrShFY")) else 0)
                bps = equity / shares if shares > 0 else np.nan
        for metric, value in (("eps", eps), ("bps", bps), ("dps", dps)):
            if not np.isnan(value):
                rows.append({"code": row["code"], "metric": metric, "value": value, "date": row["disclosed_at"],
                             "source": "J-Quants"})
    return pd.DataFrame(rows, columns=INPUT_COLUMNS)


def latest_inputs(*sources: pd.DataFrame) -> pd.DataFrame:
    """銘柄ごと・項目ごとに、開示日が一番新しい数字を残す。"""
    frames = [s for s in sources if s is not None and not s.empty]
    if not frames:
        return pd.DataFrame(columns=INPUT_COLUMNS)
    df = pd.concat(frames, ignore_index=True).dropna(subset=["date"])
    return df.sort_values("date").drop_duplicates(["code", "metric"], keep="last").reset_index(drop=True)


def compute_valuation(inputs: pd.DataFrame, closes: pd.Series) -> pd.DataFrame:
    """銘柄ごとの 予想PER・PBR・予想配当利回り（%）と、元になった開示の日付・取り先を返す（索引は銘柄コード）。"""
    table = {m: inputs[inputs["metric"] == m].set_index("code") for m in ("eps", "bps", "dps")}
    rows = {}
    for code, close in closes.dropna().items():
        out = {}
        eps = table["eps"]["value"].get(code, np.nan)
        out["per_forecast"] = close / eps if eps > 0 else np.nan
        out["per_note"] = "赤字予想" if eps <= 0 else ""
        bps = table["bps"]["value"].get(code, np.nan)
        out["pbr"] = close / bps if bps > 0 else np.nan
        dps = table["dps"]["value"].get(code, np.nan)
        out["dividend_yield"] = dps / close * 100 if dps >= 0 else np.nan
        for m in ("eps", "bps"):
            if code in table[m].index:
                r = table[m].loc[code]
                out[f"{m}_date"] = f"{pd.Timestamp(r['date']):%Y-%m-%d}（{r['source']}）"
            else:
                out[f"{m}_date"] = ""
        rows[code] = out
    return pd.DataFrame.from_dict(rows, orient="index")


def format_valuation(row) -> tuple[str, str, str]:
    """表に出す文字（予想PER, PBR, 予想配当利回り）。"""
    per = f"{row['per_forecast']:.1f}倍" if pd.notna(row.get("per_forecast")) else (row.get("per_note") or "—")
    pbr = f"{row['pbr']:.2f}倍" if pd.notna(row.get("pbr")) else "—"
    dy = f"{row['dividend_yield']:.2f}%" if pd.notna(row.get("dividend_yield")) else "—"
    return per, pbr, dy
