"""毎日のレポートの「材料の整理」の部分を作る。

ここで出すのは判断の材料で、売買の推奨ではない。これまでの検証で、機械的な点数に予測する力は
確認できていないため、ランキングはレポートの最後に「参考」として置く。

欄:
  自分の注目銘柄       config/local/my_stocks.yaml に書いた銘柄の、株価・トレンド・直近の開示
  新しい開示           前の営業日の15時以降に出た開示を、種類ごとに整理（決算・業績修正は XBRL の中身つき）
  値動きで目立った銘柄 52週高値の更新、出来高の急増
"""

import re

import pandas as pd
import yaml

from kabu.analysis.earnings_brief import earnings_brief
from kabu.config import resolve_path

MY_STOCKS_FILE = "config/local/my_stocks.yaml"
RECENT_DAYS = 14
MAX_ROWS = 30

# 新しい開示を並べる欄（分類名 → 欄）。どれにも当たらない分類は出さない
SECTIONS = [
    ("業績予想の修正", {"upward_revision", "downward_revision", "revision_unknown"}),
    ("決算発表", {"earnings"}),
    ("配当・自社株買い・株式分割", {"dividend_up", "buyback", "split"}),
    ("注意が必要な開示", {"dividend_down", "dilution", "misconduct", "impairment"}),
]


def load_my_stocks() -> dict[str, str]:
    """自分の注目銘柄（銘柄コード → メモ）。ファイルがなければ空。"""
    path = resolve_path(MY_STOCKS_FILE)
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(code).strip().upper(): ("" if memo is None else str(memo)) for code, memo in data.items()}


def _trend_text(row) -> str:
    alignment = {1: "上昇配列", -1: "下降配列"}.get(row.get("ma_alignment"), "まちまち")
    parts = [alignment]
    if pd.notna(row.get("deviation25")):
        parts.append(f"25日線から{row['deviation25']:+.1f}%")
    if pd.notna(row.get("high52_distance")):
        parts.append(f"52週高値から{row['high52_distance']:+.1f}%")
    return "、".join(parts)


def _fmt_change(value) -> str:
    return "—" if pd.isna(value) else f"{value:+.2f}%"


def my_stocks_section(my_stocks: dict[str, str], indicators: pd.DataFrame, names: dict[str, str],
                      classified: pd.DataFrame, figures: pd.DataFrame, reference: pd.Timestamp) -> list[str]:
    if not my_stocks:
        return ["## 自分の注目銘柄", "",
                f"まだ登録されていません。`{MY_STOCKS_FILE}` に銘柄コードを書くと、ここに表示されます。", ""]
    lines = ["## 自分の注目銘柄", "", "| コード | 銘柄 | メモ | 終値 | 前日比 | トレンド |", "|---|---|---|---:|---:|---|"]
    since = reference - pd.Timedelta(days=RECENT_DAYS)
    news = []
    for code, memo in my_stocks.items():
        name = names.get(code, "")
        if code in indicators.index:
            r = indicators.loc[code]
            lines.append(f"| {code} | {name} | {memo} | {r['close']:,.0f} | {_fmt_change(r['day_change_pct'])} | "
                         f"{_trend_text(r)} |")
        else:
            lines.append(f"| {code} | {name} | {memo} | — | — | 株価を取れませんでした |")
        recent = classified[(classified["code"] == code) & (classified["pubdate"] >= since)]
        for d in recent.drop_duplicates("id").itertuples(index=False):
            brief, _ = earnings_brief(figures[figures["code"] == code], d.id) if not figures.empty else ("", None)
            news.append(f"- {code} {name} {d.pubdate:%m/%d %H:%M} {d.title}" + (f" — {brief}" if brief else ""))
    lines += ["", f"直近{RECENT_DAYS}日の開示:", ""] + (news or ["- ありません"]) + [""]
    return lines


def new_disclosures_section(classified: pd.DataFrame, figures: pd.DataFrame, names: dict[str, str],
                            since: pd.Timestamp, codes: set[str]) -> list[str]:
    recent = classified[(classified["pubdate"] >= since) & classified["code"].isin(codes)]
    lines = [f"## 新しい開示（{since:%m/%d %H:%M} 以降、対象銘柄のみ）", ""]
    if recent.empty:
        return lines + ["ありません。", ""]
    for title, categories in SECTIONS:
        rows = recent[recent["category"].isin(categories)].drop_duplicates("id")
        if rows.empty:
            continue
        entries = []
        for d in rows.itertuples(index=False):
            brief, change = (earnings_brief(figures[figures["code"] == d.code], d.id)
                             if not figures.empty and title in ("業績予想の修正", "決算発表") else ("", None))
            labels = "・".join(recent.loc[recent["id"] == d.id, "label"].unique())
            if brief:
                labels = re.sub(r"（[^）]*）", "", labels)   # 中身を表示できたら「（中身はまだ評価していない）」などは省く
            text = brief or (d.title if len(d.title) <= 50 else d.title[:49] + "…")
            entries.append((change, f"| {d.code} | {names.get(d.code, d.company_name)} | {d.pubdate:%m/%d %H:%M} | "
                                    f"{labels} | {text} |"))
        # 業績予想の修正と決算は、予想の変化が大きい順（上方修正が上）。分からないものは最後
        entries.sort(key=lambda e: (e[0] is None, -(e[0] or 0)))
        lines += [f"### {title}（{len(entries)}件）", "", "| コード | 銘柄 | 時刻 | 種類 | 中身 |", "|---|---|---|---|---|"]
        lines += [e[1] for e in entries[:MAX_ROWS]]
        if len(entries) > MAX_ROWS:
            lines.append(f"\n……ほか {len(entries) - MAX_ROWS} 件")
        lines.append("")
    if figures.empty:
        lines += ["XBRL（決算の数字）がまだ保存されていないため、決算・業績修正の中身は表示していません。", ""]
    return lines


def movers_section(indicators: pd.DataFrame, names: dict[str, str]) -> list[str]:
    lines = ["## 値動きで目立った銘柄", ""]
    highs = indicators[indicators["high52_distance"] >= 0].sort_values("turnover20_oku", ascending=False)
    lines += [f"### 52週高値を更新（{len(highs)}銘柄、売買代金の多い順）", ""]
    lines += [f"- {c} {names.get(c, '')} 終値 {r['close']:,.0f}（{_fmt_change(r['day_change_pct'])}）"
              for c, r in highs.head(15).iterrows()] or ["- ありません"]
    if len(highs) > 15:
        lines.append(f"- ……ほか {len(highs) - 15} 銘柄")
    for title, cond, order in (("出来高を伴って上昇（出来高が直前20日平均の3倍以上）", indicators["volume_ratio_signed"] >= 3, False),
                               ("出来高を伴って下落（出来高が直前20日平均の3倍以上）", indicators["volume_ratio_signed"] <= -3, True)):
        rows = indicators[cond].sort_values("day_change_pct", ascending=order)
        lines += ["", f"### {title}（{len(rows)}銘柄）", ""]
        lines += [f"- {c} {names.get(c, '')} {_fmt_change(r['day_change_pct'])}、出来高 {abs(r['volume_ratio_signed']):.1f}倍"
                  for c, r in rows.head(15).iterrows()] or ["- ありません"]
    return lines + [""]
