"""決算データ（J-Quants）を日付順に追い、会社予想の変化を「出来事」として取り出す。

出来事は3種類:
  revision     同じ決算期の利益予想が、前の書類から何%変わったか（業績予想の修正の書類だけでなく、
               四半期の決算短信の中で予想を変えた場合も含む。変わらなかった回も「据え置き」として残す）
  fy_surprise  本決算の利益の実績が、直前の会社予想より何%上か下か
  new_guidance 本決算と一緒に出る来期予想が、今期の実績より何%伸びる見込みか

利益は 営業利益（OP）→ 経常利益（OdP）→ 純利益（NP）の順に比べ、最初に変化があったものを使う
（営業利益は据え置きで純利益だけ修正、といった場合も修正として扱う）。どれも変わらなければ据え置き。
比べる元の数字が0以下（赤字など）のときは、変化率に意味がないので出来事にしない。
その日に分かっていた情報だけで計算する（書類を開示の時刻順に1件ずつ読み進める）。
"""

import numpy as np
import pandas as pd

# (実績, 今期予想, 来期予想, 表示名)。連結の項目名。非連結の会社は NC を付けた名前（NCOP・FNCOP・NxFNCOP など）。
# J-Quants は表記がそろっていない（今期の純利益予想は FNP、来期は NxFNp）ので、大文字小文字を区別せずに探す。
METRICS = [("OP", "FOP", "NxFOP", "営業利益"), ("OdP", "FOdP", "NxFOdP", "経常利益"), ("NP", "FNP", "NxFNP", "純利益")]
ROLES = {"actual": 0, "forecast": 1, "next": 2}


def _non_consolidated(name: str) -> str:
    """連結の項目名から、非連結の項目名を作る（OP → NCOP、FOP → FNCOP、NxFOP → NxFNCOP）。"""
    for prefix in ("NxF", "F"):
        if name.startswith(prefix):
            return prefix + "NC" + name[len(prefix):]
    return "NC" + name


def normalize_columns(statements: pd.DataFrame) -> pd.DataFrame:
    """項目名の大文字小文字のゆれをそろえる（例: NxFNp → NxFNP）。"""
    canonical = {}
    for metric in METRICS:
        for name in metric[:3]:
            canonical[name.lower()] = name
            canonical[_non_consolidated(name).lower()] = _non_consolidated(name)
    return statements.rename(columns={c: canonical[c.lower()] for c in statements.columns if c.lower() in canonical})
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
    """2つの数字の組を、営業利益 → 経常利益 → 純利益 の順に比べ、最初に変化があった利益で (名前, 前, 後) を返す。
    どの利益も変わっていなければ、比べられた最初の利益を返す（据え置き）。前が0以下の利益は飛ばす。"""
    first = None
    for name in ("営業利益", "経常利益", "純利益"):
        b, a = before.get(name), after.get(name)
        if b is None or a is None or np.isnan(b) or np.isnan(a) or b <= 0:
            continue
        if abs(a / b - 1) > 1e-9:
            return name, b, a
        first = first or (name, b, a)
    return first


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


def _values(row, role: str) -> dict:
    """1つの書類から、実績・今期予想・来期予想のどれかを {表示名: 値} で取り出す。
    連結の数字が1つでもあれば連結を、なければ非連結を使う（連結と非連結を混ぜて比べないため）。"""
    i = ROLES[role]
    consolidated = {m[3]: row.get(m[i], np.nan) for m in METRICS}
    if any(not np.isnan(v) for v in consolidated.values()):
        return consolidated
    return {m[3]: row.get(_non_consolidated(m[i]), np.nan) for m in METRICS}


def guidance_events(statements: pd.DataFrame) -> pd.DataFrame:
    """決算データから出来事を取り出す（1行1件）。"""
    statements = normalize_columns(statements)
    has_next_year = any(name in statements.columns for m in METRICS for name in (m[2], _non_consolidated(m[2])))
    events = []
    for code, g in statements.groupby("code", sort=False):
        known: dict[str, dict] = {}   # 決算期の終わりの日 → その時点で一番新しい会社予想
        for row in g.to_dict("records"):
            fy_end = _text(row.get("CurFYEn"))
            forecast = _values(row, "forecast")
            has_forecast = any(not np.isnan(v) for v in forecast.values())
            base = {"code": code, "disclosed_at": row["disclosed_at"], "doc_type": row.get("DocType", ""), "fy_end": fy_end}

            if row.get("CurPerType") == "FY" and "FinancialStatements" in str(row.get("DocType", "")):
                actual = _values(row, "actual")
                key = _find(known, fy_end)
                if key and (cmp := _compare(known[key], actual)):
                    name, b, a = cmp
                    category, label = bucket(a / b - 1, SURPRISE_BUCKETS)
                    events.append({**base, "kind": "fy_surprise", "metric": name, "before": b, "after": a,
                                   "change": a / b - 1, "category": f"fy_surprise_{category}", "label": label})
                # 来期予想: NxF〜 の項目があればそれ、なければ F〜 の項目
                # （本決算の時点で今期は終わっているので、F〜 に入っているのは来期の予想とみなす）
                next_forecast = _values(row, "next") if has_next_year else forecast
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
