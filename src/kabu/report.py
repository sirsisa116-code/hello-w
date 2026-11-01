"""ランキングを CSV とレポート（Markdown）に書き出し、履歴に蓄積する。"""

from datetime import datetime
from pathlib import Path

import pandas as pd

from kabu.quality import QualityLog
from kabu.scoring import CATEGORY_LABELS, CategoryScore

WEEKDAYS = "月火水木金土日"


def save_ranking_csv(ranking: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ranking.to_csv(path, index=False, encoding="utf-8-sig")


def append_history(ranking: pd.DataFrame, path: Path, data_date: pd.Timestamp, run_at: datetime, weights_version) -> None:
    """全日付のランキングを1つのファイルに蓄積する。
    同じ基準日で実行し直した場合は、その日の分を入れ替える（重複させない）。"""
    new = ranking.copy()
    new.insert(0, "data_date", data_date.strftime("%Y-%m-%d"))
    new.insert(1, "run_at", run_at.strftime("%Y-%m-%d %H:%M"))
    new.insert(2, "weights_version", weights_version)

    if path.exists():
        old = pd.read_csv(path, dtype={"code": str}, encoding="utf-8-sig")
        old = old[old["data_date"] != new["data_date"].iloc[0]]
        new = pd.concat([old, new], ignore_index=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    new.to_csv(path, index=False, encoding="utf-8-sig")


def _fmt_date(ts: pd.Timestamp) -> str:
    return f"{ts:%Y-%m-%d}（{WEEKDAYS[ts.weekday()]}）"


def build_report(
    ranking: pd.DataFrame,
    details: dict[str, dict[str, CategoryScore]],
    excluded: pd.DataFrame,
    quality: QualityLog,
    data_date: pd.Timestamp,
    run_at: datetime,
    weights: dict,
    top_n: int,
) -> str:
    used = [CATEGORY_LABELS[k] + f"（重み {w}）" for k, w in weights["category_weights"].items() if w and w > 0]
    unused = [CATEGORY_LABELS[k] for k, w in weights["category_weights"].items() if not w or w <= 0]

    lines = [
        f"# 買い候補ランキング {_fmt_date(data_date)}",
        "",
        f"- データ基準日: {_fmt_date(data_date)} の終値まで",
        f"- 実行日時: {run_at:%Y-%m-%d %H:%M}",
        f"- 使った分野: {'、'.join(used)}",
    ]
    if unused:
        lines.append(f"- 使っていない分野: {'、'.join(unused)}（重み0）")
    lines += [
        f"- 点数ルール: weights.yaml version {weights['version']}",
        f"- 対象 {len(ranking) + len(excluded)} 銘柄 / ランキング入り {len(ranking)} / 除外 {len(excluded)}",
        "",
        "## データの問題",
        "",
    ]
    if quality.issues:
        lines += [f"    {line.strip()}" for line in quality.summary_lines()]
    else:
        lines.append("問題はありません。")

    lines += ["", f"## ランキング（上位 {min(top_n, len(ranking))}）", ""]
    if ranking.empty:
        lines.append("ランキングに入った銘柄はありません。")
    else:
        lines += [
            "| 順位 | コード | 銘柄 | 総合 | 終値 | 前日比 | 主なプラス要因 |",
            "|---:|---|---|---:|---:|---:|---|",
        ]
        for r in ranking.head(top_n).itertuples(index=False):
            lines.append(
                f"| {r.rank} | {r.code} | {r.name} | {r.total_score:.1f} | {r.close:,.0f} | "
                f"{r.day_change_pct:+.2f}% | {r.plus_reasons or '—'} |"
            )

        lines += ["", "## 銘柄ごとの根拠", ""]
        for r in ranking.head(top_n).itertuples(index=False):
            sector = f"（{r.sector33}）" if isinstance(r.sector33, str) else ""
            lines += [f"### {r.rank}位 {r.code} {r.name}{sector}  総合 {r.total_score:.1f}点", ""]
            for category in details[r.code].values():
                label = CATEGORY_LABELS[category.name]
                score = "欠損" if category.score is None else f"{category.score:.1f}点"
                lines += [f"**{label}: {score}**", "", "| 指標 | 値 | 点 | 判定 |", "|---|---:|---:|---|"]
                for c in sorted(category.contributions, key=lambda c: c.points, reverse=True):
                    lines.append(f"| {c.label} | {c.value_text} | {c.points:+.0f} | {c.note} |")
                for m in category.missing:
                    lines.append(f"| {m} | 計算不可 | 0 | データ不足のため点数なし |")
                lines.append("")
            if r.missing:
                lines += [f"欠損: {r.missing}", ""]

    if not excluded.empty:
        lines += ["## ランキングから外した銘柄", ""]
        for e in excluded.itertuples(index=False):
            lines.append(f"- {e.code} {e.name}: {e.reason}")
        lines.append("")

    lines += [
        "---",
        "このランキングは機械的なルールで付けた点数で、売買を勧めるものではありません。",
        "点数ルールの効果は未検証です。",
    ]
    return "\n".join(lines) + "\n"


def save_report(text: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Markdown はメモ帳でも読めるよう BOM 付き UTF-8 にする
    path.write_text(text, encoding="utf-8-sig")
