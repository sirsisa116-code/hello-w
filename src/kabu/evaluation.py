"""過去のランキングと、その後の実際の値動きを突き合わせる（事後検証）。

考え方:
  - ランキングは引け後に作るので、実際に買えるのは翌営業日の始値（＝買値）
  - N営業日後の終値で売ったとして騰落率を出す（N = 1, 5, 10, 20 など）
  - 同じ期間の TOPIX ETF（1306）の騰落率を引いて「超過リターン」を出す
    → 市場全体が上がっただけなのか、選んだ銘柄が良かったのかを区別するため
  - 営業日は TOPIX ETF の取引日で数える

配当は計算に入れていない（株価のみ）。配当落ち日をまたぐと、実際より少し悪く出る。
ネットにはつながない。株価は手元に保存済みのものを使う。
"""

import numpy as np
import pandas as pd

CONFIRMED = "確定"
PENDING = "未確定"
MISSING = "株価欠損"


def forward_returns(
    history: pd.DataFrame, prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame, horizons: list[int]
) -> pd.DataFrame:
    """history（ランキングの履歴）の各行に、N営業日後の騰落率（%）を付けて返す。

    付ける列（N ごと）:
      ret_N:    銘柄の騰落率
      bench_N:  TOPIX ETF の騰落率
      excess_N: 超過リターン（ret_N − bench_N）
      status_N: 確定 / 未確定（まだ N 営業日たっていない） / 株価欠損（売買停止など）
    """
    calendar = benchmark.index
    bench_open = benchmark["open"].to_numpy()
    bench_close = benchmark["close"].to_numpy()
    last_pos = len(calendar) - 1

    out = history[["data_date", "code", "name", "rank", "total_score", "weights_version"]].copy()
    out["data_date"] = pd.to_datetime(out["data_date"])
    # 基準日が TOPIX ETF の何番目の取引日か（基準日以前で一番近い取引日）
    base_pos = calendar.searchsorted(out["data_date"].to_numpy(), side="right") - 1
    entry_pos = base_pos + 1
    out["entry_date"] = [calendar[p].date() if p <= last_pos else None for p in entry_pos]

    for n in horizons:
        ret = np.full(len(out), np.nan)
        bench = np.full(len(out), np.nan)
        status = np.full(len(out), PENDING, dtype=object)
        exit_pos = base_pos + n

        for code, idx in out.groupby("code").indices.items():
            stock = prices.get(code)
            if stock is None:
                status[idx] = MISSING
                continue
            # 銘柄の株価を TOPIX ETF の取引日にそろえる（売買停止の日は NaN になる）
            s_open = stock["open"].reindex(calendar).to_numpy()
            s_close = stock["close"].reindex(calendar).to_numpy()
            for i in idx:
                e, x = entry_pos[i], exit_pos[i]
                if base_pos[i] < 0:
                    status[i] = MISSING  # 基準日が TOPIX ETF の手元データより前
                    continue
                if x > last_pos:
                    continue  # まだ N 営業日たっていない（未確定のまま）
                buy, sell = s_open[e], s_close[x]
                if np.isnan(buy) or np.isnan(sell) or buy <= 0:
                    status[i] = MISSING
                    continue
                ret[i] = (sell / buy - 1) * 100
                bench[i] = (bench_close[x] / bench_open[e] - 1) * 100
                status[i] = CONFIRMED

        out[f"ret_{n}"] = ret.round(3)
        out[f"bench_{n}"] = bench.round(3)
        out[f"excess_{n}"] = (ret - bench).round(3)
        out[f"status_{n}"] = status
    return out


def _spearman(a: pd.Series, b: pd.Series) -> float:
    """順位相関（−1〜1）。点数が高い銘柄ほど実際に上がっていれば +1 に近づく。
    scipy を入れずに済むよう、順位に直してから普通の相関を取る。"""
    if len(a) < 3 or a.nunique() < 2 or b.nunique() < 2:
        return np.nan
    return a.rank().corr(b.rank())


def summarize(results: pd.DataFrame, horizons: list[int], top_n: int) -> dict[int, dict]:
    """N ごとに成績を集計する。"""
    summary = {}
    for n in horizons:
        status = results[f"status_{n}"]
        done = results[status == CONFIRMED]
        excess = f"excess_{n}"
        groups = {
            f"上位{top_n}銘柄": done[done["rank"] <= top_n],
            "それ以外": done[done["rank"] > top_n],
            "全体": done,
        }
        table = pd.DataFrame(
            [
                {
                    "グループ": label,
                    "件数": len(g),
                    "平均騰落率(%)": g[f"ret_{n}"].mean(),
                    "平均超過リターン(%)": g[excess].mean(),
                    "超過リターンの中央値(%)": g[excess].median(),
                    "市場に勝った割合(%)": (g[excess] > 0).mean() * 100 if len(g) else np.nan,
                }
                for label, g in groups.items()
            ]
        )

        # 日ごとに「上位の平均 − それ以外の平均」を出す。同じ日の銘柄は同じ相場の影響を受けるので、
        # 件数より「何日分あるか」のほうが、結果の信頼度の目安として正直。
        by_date = []
        for date, g in done.groupby("data_date"):
            top = g.loc[g["rank"] <= top_n, excess]
            rest = g.loc[g["rank"] > top_n, excess]
            by_date.append({
                "data_date": date,
                "spread": top.mean() - rest.mean() if len(top) and len(rest) else np.nan,
                "ic": _spearman(g["total_score"], g[excess]),
            })
        by_date = pd.DataFrame(by_date, columns=["data_date", "spread", "ic"])

        summary[n] = {
            "table": table,
            "dates": len(by_date),
            "spread_mean": by_date["spread"].mean(),
            "spread_win": (by_date["spread"].dropna() > 0).mean() * 100 if by_date["spread"].notna().any() else np.nan,
            "ic_mean": by_date["ic"].mean(),
            "pending": int((status == PENDING).sum()),
            "missing": int((status == MISSING).sum()),
        }
    return summary


OUTLIER_PCT = 50.0   # 超過リターンがこれ（±50%）を超えたら、データ誤りを疑う


def find_outliers(results: pd.DataFrame, horizons: list[int], threshold: float = OUTLIER_PCT) -> pd.DataFrame:
    """超過リターンが極端な回を一覧にする。株価データの誤りがあると、ここに集中して現れる。"""
    rows = []
    for n in horizons:
        hit = results[results[f"excess_{n}"].abs() >= threshold]
        for r in hit.itertuples(index=False):
            rows.append({"N": n, "data_date": r.data_date.date(), "code": r.code, "name": r.name,
                         "entry_date": r.entry_date, "ret": getattr(r, f"ret_{n}"),
                         "bench": getattr(r, f"bench_{n}"), "excess": getattr(r, f"excess_{n}")})
    return pd.DataFrame(rows, columns=["N", "data_date", "code", "name", "entry_date", "ret", "bench", "excess"])


def fmt_value(value, digits: int = 2, sign: bool = False) -> str:
    if value is None or pd.isna(value):
        return "—"
    return f"{value:+.{digits}f}" if sign else f"{value:.{digits}f}"


def build_evaluation_report(
    summary: dict[int, dict], source_label: str, versions: list, excluded_versions: list,
    date_range: tuple, top_n: int, notes: list[str], outliers: pd.DataFrame | None = None,
) -> str:
    lines = [
        f"# 事後検証レポート（{source_label}）",
        "",
        f"- 対象のランキング: {date_range[0]} 〜 {date_range[1]}",
        f"- 点数ルール: weights.yaml version {', '.join(map(str, versions))}",
    ]
    if excluded_versions:
        lines.append(f"- 集計から外した古い version: {', '.join(map(str, excluded_versions))}（ルールが違うため混ぜない）")
    lines += [
        "- 買値: ランキング翌営業日の始値 / 売値: N営業日後の終値 / 比較対象: TOPIX ETF（1306）",
        "- 超過リターン = 銘柄の騰落率 − 同じ期間の TOPIX ETF の騰落率（配当は含まない）",
        "",
    ]
    for note in notes:
        lines.append(f"> {note}")
    if notes:
        lines.append("")

    for n, s in summary.items():
        lines += [f"## {n}営業日後", ""]
        if s["dates"] == 0:
            lines += [f"確定した結果がまだありません（未確定 {s['pending']} 件）。", ""]
            continue
        lines += [
            "| グループ | 件数 | 平均騰落率 | 平均超過リターン | 超過の中央値 | 市場に勝った割合 |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for r in s["table"].itertuples(index=False):
            lines.append(
                f"| {r[0]} | {r[1]} | {fmt_value(r[2], sign=True)}% | {fmt_value(r[3], sign=True)}% | "
                f"{fmt_value(r[4], sign=True)}% | {fmt_value(r[5], 0)}% |"
            )
        lines += [
            "",
            f"- 集計できた日数: {s['dates']} 日（未確定 {s['pending']} 件、株価欠損 {s['missing']} 件は除外）",
            f"- 上位{top_n}銘柄 − それ以外 の差（日ごとの平均）: {fmt_value(s['spread_mean'], sign=True)}%"
            f"、上位が勝った日の割合: {fmt_value(s['spread_win'], 0)}%",
            f"- 点数と超過リターンの順位相関（日ごとの平均）: {fmt_value(s['ic_mean'], 3, sign=True)}"
            "（0 なら点数と値動きは無関係、+ なら点数が高いほど上がった）",
            "",
        ]

    if outliers is not None and not outliers.empty:
        lines += [
            f"## 極端な値（超過リターン ±{OUTLIER_PCT:.0f}% 以上）: {len(outliers)} 件",
            "",
            "株価データの誤り（株式分割の反映漏れなど）の可能性が高いです。これがあると平均が大きくゆがむので、"
            "原因が分かるまで上の平均は信用しないでください（中央値のほうがまだ当てになります）。",
            "",
            "| N | 基準日 | コード | 銘柄 | 買った日 | 騰落率 | TOPIX ETF | 超過 |",
            "|---:|---|---|---|---|---:|---:|---:|",
        ]
        for r in outliers.head(30).itertuples(index=False):
            lines.append(f"| {r.N} | {r.data_date} | {r.code} | {r.name} | {r.entry_date} | "
                         f"{r.ret:+.1f}% | {r.bench:+.1f}% | {r.excess:+.1f}% |")
        if len(outliers) > 30:
            lines.append(f"\n……ほか {len(outliers) - 30} 件（明細 CSV を見てください）")
        lines.append("")

    lines += [
        "---",
        "読み方の注意:",
        "",
        "- 日数が少ないうちは、良い結果も悪い結果も偶然の可能性が高いです。目安として、少なくとも数十日分はほしいところです。",
        "- 連続した日のランキングは中身がよく似ていて、保有期間も重なります。件数の見た目ほど独立した証拠ではありません。",
        "- 売買手数料・スプレッドは引いていません。",
    ]
    return "\n".join(lines) + "\n"
