"""決算データ（J-Quants）を日付順に追い、会社予想の変化を「出来事」として取り出す。

出来事は3種類:
  revision     同じ決算期の利益予想が、前の書類から何%変わったか（業績予想の修正の書類だけでなく、
               四半期の決算短信の中で予想を変えた場合も含む。変わらなかった回も「据え置き」として残す）
  fy_surprise  本決算の利益の実績が、直前の会社予想より何%上か下か
  new_guidance 本決算と一緒に出る来期予想が、今期の実績より何%伸びる見込みか

利益は 営業利益（OP）→ 経常利益（OdP）→ 純利益（NP）の順で、比べる2つの数字がそろう最初のものを使う。
比べる元の数字が0以下（赤字など）のときは、変化率に意味がないので出来事にしない。
その日に分かっていた情報だけで計算する（書類を開示の時刻順に1件ずつ読み進める）。
"""

import numpy as np
import pandas as pd

METRICS = [("OP", "FOP", "NxFOP", "営業利益"), ("OdP", "FOdP", "NxFOdP", "経常利益"), ("NP", "FNP", "NxFNP", "純利益")]
EVENT_COLUMNS = ["code", "disclosed_at", "doc_type", "fy_end", "kind", "metric", "before", "after", "change",
                 "category", "label"]

# 変化率の区切り（下限以上・上限未満）と名前
REVISION_BUCKETS = [(0.10, np.inf, "大幅上方修正（+10%以上）"), (0.01, 0.10, "上方修正（+1〜10%）"),
                    (-0.01, 0.01, "据え置き（±1%未満）"), (-0.10, -0.01, "下方修正（−1〜10%）"),
                    (-np.inf, -0.10, "大幅下方修正（−10%以下）")]
SURPRISE_BUCKETS = [(0.10, np.inf, "本決算が予想を大きく上回る（+10%以上）"), (0.02, 0.10, "本決算が予想を上回る（+2〜10%）"),
                    (-0.02, 0.02, "本決算がほぼ予想どおり（±2%未満）"), (-0.10, -0.02, "本決算が予想を下回る（−2〜10%）"),
                    (-np.inf, -0.10, "本決算が予想を大きく下回る（−10%以下）")]
GUIDANCE_BUCKETS = [(0.20, np.inf, "来期予想が大幅増益（+20%以上）"), (0.05, 0.20, "来期予想が増益（+5〜20%）"),
                    (-0.05, 0.05, "来期予想が横ばい（±5%未満）"), (-0.20, -0.05, "来期予想が減益（−5〜20%）"),
                    (-np.inf, -0.20, "来期予想が大幅減益（−20%以下）")]


def bucket(change: float, buckets: list) -> tuple[str, str]:
    for i, (low, high, label) in enumerate(buckets):
        if low <= change < high:
            return str(i), label
    return "", ""


def _compare(before: dict, after: dict) -> tuple[str, float, float] | None:
    """2つの数字の組から、そろう最初の利益で (名前, 前, 後) を返す。前が0以下ならその利益は飛ばす。"""
    for name in ("営業利益", "経常利益", "純利益"):
        b, a = before.get(name), after.get(name)
        if b is not None and a is not None and not (np.isnan(b) or np.isnan(a)) and b > 0:
            return name, b, a
    return None


def _text(value) -> str | None:
    """空欄（NaN や ""）なら None。"""
    return value if isinstance(value, str) and value else None


def _find(known: dict, fy_end: str | None) -> str | None:
    """決算期の終わりの日が同じ（閏年の2月末などで数日ずれても同じとみなす）予想の鍵を探す。"""
    if not fy_end:
        return None
    target = pd.Timestamp(fy_end)
    for key in known:
        if abs((pd.Timestamp(key) - target).days) <= 3:
            return key
    return None


def _values(row, columns: list[str]) -> dict:
    return {label: row.get(col, np.nan) for col, label in zip(columns, [m[3] for m in METRICS])}


def guidance_events(statements: pd.DataFrame) -> pd.DataFrame:
    """決算データから出来事を取り出す（1行1件）。"""
    has_next_year = any(m[2] in statements.columns for m in METRICS)
    events = []
    for code, g in statements.groupby("code", sort=False):
        known: dict[str, dict] = {}   # 決算期の終わりの日 → その時点で一番新しい会社予想
        for row in g.to_dict("records"):
            fy_end = _text(row.get("CurFYEn"))
            forecast = _values(row, [m[1] for m in METRICS])
            has_forecast = any(not np.isnan(v) for v in forecast.values())
            base = {"code": code, "disclosed_at": row["disclosed_at"], "doc_type": row.get("DocType", ""), "fy_end": fy_end}

            if row.get("CurPerType") == "FY" and "FinancialStatements" in str(row.get("DocType", "")):
                actual = _values(row, [m[0] for m in METRICS])
                key = _find(known, fy_end)
                if key and (cmp := _compare(known[key], actual)):
                    name, b, a = cmp
                    category, label = bucket(a / b - 1, SURPRISE_BUCKETS)
                    events.append({**base, "kind": "fy_surprise", "metric": name, "before": b, "after": a,
                                   "change": a / b - 1, "category": f"fy_surprise_{category}", "label": label})
                # 来期予想: NxF〜 の項目があればそれ、なければ F〜 の項目
                # （本決算の時点で今期は終わっているので、F〜 に入っているのは来期の予想とみなす）
                next_forecast = _values(row, [m[2] for m in METRICS]) if has_next_year else forecast
                next_end = _text(row.get("NxFYEn")) or (
                    str(pd.Timestamp(fy_end) + pd.DateOffset(years=1))[:10] if fy_end else None)
                if next_end:
                    if any(not np.isnan(v) for v in next_forecast.values()):
                        if cmp := _compare(actual, next_forecast):
                            name, b, a = cmp
                            category, label = bucket(a / b - 1, GUIDANCE_BUCKETS)
                            events.append({**base, "fy_end": next_end, "kind": "new_guidance", "metric": name,
                                           "before": b, "after": a, "change": a / b - 1,
                                           "category": f"new_guidance_{category}", "label": label})
                        known[next_end] = next_forecast
                continue

            if has_forecast and fy_end:
                key = _find(known, fy_end) or fy_end
                if key in known and (cmp := _compare(known[key], forecast)):
                    name, b, a = cmp
                    category, label = bucket(a / b - 1, REVISION_BUCKETS)
                    events.append({**base, "kind": "revision", "metric": name, "before": b, "after": a,
                                   "change": a / b - 1, "category": f"revision_{category}", "label": label})
                known[key] = {k: (v if not np.isnan(v) else known.get(key, {}).get(k, np.nan))
                              for k, v in forecast.items()}
    return pd.DataFrame(events, columns=EVENT_COLUMNS)
