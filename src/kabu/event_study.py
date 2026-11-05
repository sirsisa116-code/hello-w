"""適時開示の種類ごとに、開示のあと株価がどう動いたかを集計する（イベントスタディ）。

1件の開示ごとに、次の2つを分けて測る:
  反応（買う前）: 開示前の最後の終値 → 買える最初の始値 の騰落率（TOPIX ETF との差）
                  ここは翌朝の寄り付きで買う前提だと取れない部分
  その後:         買える最初の始値 → N営業日後の終値 の騰落率（TOPIX ETF との差）
                  ここが、開示を見てから買った場合に実際に取れる部分

「買える最初の始値」は、開示が 9:00 より前なら当日の始値（取引日なら）、9:00 以降なら翌営業日の始値。
騰落率はどれも配当込み（evaluation.total_return_prices）。
ネットにはつながない（株価も開示も手元に保存済みのものを使う）。
"""

import numpy as np
import pandas as pd

from kabu.evaluation import CONFIRMED, forward_returns, t_value, total_return_prices

MIN_DATES_FOR_T = 10   # t値を出すのに必要な「出来事のあった日」の数


def base_date(pubdate: pd.Timestamp) -> pd.Timestamp:
    """開示の「基準日」= この日の終値までは開示前、という日。
    9:00 より前の開示なら前日（当日の寄り付きで買える）、それ以外は当日（翌営業日の寄り付きで買える）。"""
    day = pubdate.normalize()
    return day - pd.Timedelta(days=1) if pubdate.hour < 9 else day


def event_returns(
    events: pd.DataFrame, prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame, horizons: list[int]
) -> pd.DataFrame:
    """分類済みの開示（1行1件）に、反応（買う前）と N営業日後の超過リターンを付けて返す。"""
    history = pd.DataFrame({
        "data_date": events["pubdate"].map(base_date),
        "code": events["code"],
        "name": events["company_name"],
        "rank": 0,
        "total_score": events["points"],
        "weights_version": 0,
    }).reset_index(drop=True)
    out = forward_returns(history, prices, benchmark, horizons)
    for col in ("pubdate", "category", "label", "title"):
        out[col] = events[col].to_numpy()

    # 反応（買う前）: 基準日の終値 → 買える最初の始値
    calendar = benchmark.index
    base_pos = calendar.searchsorted(out["data_date"].to_numpy(), side="right") - 1
    entry_pos = base_pos + 1
    gap = np.full(len(out), np.nan)
    b_open, b_close, _ = total_return_prices(benchmark)
    for code, idx in out.groupby("code").indices.items():
        stock = prices.get(code)
        if stock is None:
            continue
        adj_open, adj_close, _ = total_return_prices(stock)
        s_open = adj_open.reindex(calendar).to_numpy()
        s_close = adj_close.reindex(calendar).to_numpy()
        for i in idx:
            b, e = base_pos[i], entry_pos[i]
            if b < 0 or e >= len(calendar):
                continue
            stock_gap = s_open[e] / s_close[b] - 1
            bench_gap = b_open.iloc[e] / b_close.iloc[b] - 1
            gap[i] = (stock_gap - bench_gap) * 100
    out["gap_excess"] = gap.round(3)
    return out


def summarize_events(results: pd.DataFrame, horizons: list[int]) -> pd.DataFrame:
    """開示の種類ごとに集計する。

    t値は、同じ日に出た開示どうしは同じ相場の影響を受けるので、日ごとに平均してから計算する
    （件数をそのまま使うと、実際より確かに見える）。
    """
    rows = []
    for (category, label), g in results.groupby(["category", "label"], sort=False):
        row = {"category": category, "label": label, "件数": len(g), "開示の日数": g["data_date"].nunique(),
               "反応（買う前）中央値(%)": g["gap_excess"].median()}
        for n in horizons:
            done = g[g[f"status_{n}"] == CONFIRMED]
            excess = done[f"excess_{n}"]
            by_date = done.groupby("data_date")[f"excess_{n}"].mean()
            # 出来事のあった日が少ないと、日ごとの平均から出す t値は偶然で大きく振れるので出さない
            t = t_value(by_date)[0] if len(by_date) >= MIN_DATES_FOR_T else np.nan
            row[f"{n}日後 件数"] = len(done)
            row[f"{n}日後 平均(%)"] = excess.mean()
            row[f"{n}日後 中央値(%)"] = excess.median()
            row[f"{n}日後 勝率(%)"] = (excess > 0).mean() * 100 if len(done) else np.nan
            row[f"{n}日後 t値"] = t
        rows.append(row)
    return pd.DataFrame(rows).sort_values("件数", ascending=False).reset_index(drop=True)


def summary_lines(summary: pd.DataFrame, horizons: list[int], heading: str) -> list[str]:
    """画面に出す集計表（中央値と t値）。"""
    short, long = horizons[1] if len(horizons) > 1 else horizons[0], horizons[-1]

    def fmt(v, d=2):
        return "—" if pd.isna(v) else f"{v:+.{d}f}"

    lines = [f"=== {heading}（TOPIX ETF との差、中央値） ===",
             f"{'種類':<30}{'件数':>6}{'反応(買う前)':>12}{f'{short}日後':>9}{'t値':>7}{f'{long}日後':>9}{'t値':>7}"]
    for r in summary.to_dict("records"):
        lines.append(f"{r['label'][:28]:<30}{r['件数']:>6}{fmt(r['反応（買う前）中央値(%)']):>11}%"
                     f"{fmt(r[f'{short}日後 中央値(%)']):>8}%{fmt(r[f'{short}日後 t値']):>7}"
                     f"{fmt(r[f'{long}日後 中央値(%)']):>8}%{fmt(r[f'{long}日後 t値']):>7}")
    lines.append("  ※ t値の絶対値が2未満なら、偶然でも普通に出る程度の差です")
    return lines


def build_event_report(summary: pd.DataFrame, horizons: list[int], period: tuple, universe_label: str,
                       notes: list[str], title: str = "適時開示の種類ごとの値動き（イベントスタディ）") -> str:
    def f(v, digits=2, sign=True):
        return "—" if pd.isna(v) else (f"{v:+.{digits}f}" if sign else f"{v:.{digits}f}")

    lines = [
        f"# {title}",
        "",
        f"- 対象の開示: {period[0]} 〜 {period[1]}（対象銘柄の範囲: {universe_label}）",
        "- 反応（買う前）: 開示前の最後の終値 → 買える最初の始値。翌朝に買う前提では取れない部分",
        "- N日後: 買える最初の始値 → N営業日後の終値。開示を見てから買った場合に取れる部分",
        "- どちらも TOPIX ETF（1306）の同じ期間の騰落率を引いた超過リターン。配当込み、手数料は含まない",
        "",
    ]
    lines += [f"> {n}" for n in notes]
    if notes:
        lines.append("")
    for n in horizons:
        lines += [
            f"## {n}営業日後",
            "",
            "| 種類 | 件数 | 反応（買う前）中央値 | 平均 | 中央値 | 市場に勝った割合 | t値 |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
        for r in summary.to_dict("records"):
            lines.append(
                f"| {r['label']} | {r[f'{n}日後 件数']} | {f(r['反応（買う前）中央値(%)'])}% | "
                f"{f(r[f'{n}日後 平均(%)'])}% | {f(r[f'{n}日後 中央値(%)'])}% | "
                f"{f(r[f'{n}日後 勝率(%)'], 0, False)}% | {f(r[f'{n}日後 t値'])} |"
            )
        lines.append("")
    lines += [
        "---",
        "読み方の注意:",
        "",
        "- t値は、同じ日の出来事を平均してから計算しています。絶対値が2未満なら、偶然でも普通に出る程度の差です。",
        f"- 出来事のあった日が {MIN_DATES_FOR_T} 日分未満の種類は、t値を出していません（— と表示）。",
        "- 決算の時期に出来事が集中するため、N日後の期間が重なる出来事どうしは独立ではありません。t値は実際より大きめに出ます。",
        "- 件数が数十件以下の種類は、結果が大きくぶれます。",
        "- 銘柄は「今の」対象銘柄なので、上場廃止になった銘柄などが入っていません。",
        "- タイトルだけで分類しているので、中身（修正の大きさなど）は区別していません。",
    ]
    return "\n".join(lines) + "\n"
