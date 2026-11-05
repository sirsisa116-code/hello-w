"""過去のランキングと、その後の実際の値動きを突き合わせる（事後検証）。

考え方:
  - ランキングは引け後に作るので、実際に買えるのは翌営業日の始値（＝買値）
  - N営業日後の終値で売ったとして騰落率を出す（N = 1, 5, 10, 20 など）
  - 同じ期間の TOPIX ETF（1306）の騰落率を引いて「超過リターン」を出す
    → 市場全体が上がっただけなのか、選んだ銘柄が良かったのかを区別するため
  - 営業日は TOPIX ETF の取引日で数える

配当は計算に入れる（配当を受け取って持ち続けた場合の成績）。株価だけで計算すると、
3月末・9月末に集中する権利落ち（配当の分だけ株価が下がる日）をまたいだ回が、実際より悪く見えるため。
配当込みの値は、yfinance の調整後終値（adj_close）と終値の比から作る（total_return_prices）。
ネットにはつながない。株価は手元に保存済みのものを使う。
"""

import numpy as np
import pandas as pd

from kabu.data.prices import find_suspicious_moves

CONFIRMED = "確定"
PENDING = "未確定"
MISSING = "株価欠損"


def total_return_prices(df: pd.DataFrame) -> tuple[pd.Series, pd.Series, bool]:
    """配当込みの始値・終値を作る。戻り値は (始値, 終値, 配当込みにできたか)。

    調整後終値 ÷ 終値 を「その日の調整の倍率」とし、始値・終値の両方に掛ける。
    調整後終値がない日（予備の Stooq で取った日など）は、前後の日の倍率で埋める。
    調整後終値がまったくない銘柄は、株価だけで計算する（False を返すので、呼び出し側で警告する）。
    """
    if "adj_close" not in df.columns:
        return df["open"], df["close"], False
    factor = (df["adj_close"] / df["close"]).where(df["close"] > 0)
    if factor.notna().sum() == 0:
        return df["open"], df["close"], False
    factor = factor.ffill().bfill()
    return df["open"] * factor, df["close"] * factor, True


def codes_without_dividends(prices: dict[str, pd.DataFrame]) -> list[str]:
    """配当込みの値を作れない（調整後終値がない）銘柄。"""
    return sorted(code for code, df in prices.items() if not total_return_prices(df)[2])


def forward_returns(
    history: pd.DataFrame, prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame, horizons: list[int]
) -> pd.DataFrame:
    """history（ランキングの履歴）の各行に、N営業日後の騰落率（%、配当込み）を付けて返す。

    付ける列（N ごと）:
      ret_N:    銘柄の騰落率
      bench_N:  TOPIX ETF の騰落率
      excess_N: 超過リターン（ret_N − bench_N）
      status_N: 確定 / 未確定（まだ N 営業日たっていない） / 株価欠損（売買停止など）
    """
    calendar = benchmark.index
    b_open, b_close, _ = total_return_prices(benchmark)
    bench_open = b_open.to_numpy()
    bench_close = b_close.to_numpy()
    last_pos = len(calendar) - 1
    adjusted = {code: total_return_prices(df) for code, df in prices.items()}

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
            # 銘柄の株価（配当込み）を TOPIX ETF の取引日にそろえる（売買停止の日は NaN になる）
            s_open = adjusted[code][0].reindex(calendar).to_numpy()
            s_close = adjusted[code][1].reindex(calendar).to_numpy()
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


def t_value(values: pd.Series, overlap: int = 1) -> tuple[float, int]:
    """平均が0から離れているかの目安（t値）と、実質的な件数。

    N営業日後の成績は、隣り合う日でほとんど同じ期間を見ている（重なっている）ので、
    日数をそのまま件数として数えると、実際より確かに見えてしまう。
    そこで実質的な件数を「日数 ÷ N」として計算する（overlap = N）。
    t値の絶対値が2を超えていなければ、偶然でも普通に出る程度の差と考える。
    """
    values = values.dropna()
    effective = len(values) // max(overlap, 1)
    if effective < 3 or values.std() == 0:
        return np.nan, effective
    return values.mean() / (values.std() / np.sqrt(effective)), effective


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

        ic_t, ic_k = t_value(by_date["ic"], overlap=n)
        spread_t, _ = t_value(by_date["spread"], overlap=n)

        summary[n] = {
            "table": table,
            "ic_t": ic_t,
            "spread_t": spread_t,
            "independent_dates": ic_k,
            "dates": len(by_date),
            "spread_mean": by_date["spread"].mean(),
            "spread_win": (by_date["spread"].dropna() > 0).mean() * 100 if by_date["spread"].notna().any() else np.nan,
            "ic_mean": by_date["ic"].mean(),
            "pending": int((status == PENDING).sum()),
            "missing": int((status == MISSING).sum()),
        }
    return summary


OUTLIER_PCT = 50.0   # 超過リターンがこれ（±50%）を超えたら、データ誤りを疑う


def find_outliers(
    results: pd.DataFrame, horizons: list[int], prices: dict[str, pd.DataFrame], benchmark: pd.DataFrame,
    max_move: float, threshold: float = OUTLIER_PCT,
) -> pd.DataFrame:
    """超過リターンが極端な回を一覧にする。

    極端な値は、株価データの誤りでも、実際の大きな値動きでも起きる。見分けるために、
    その回の期間（買った日の前日〜売った日）に、銘柄か TOPIX ETF の株価に異常な値動き
    （find_suspicious_moves）があるかを調べ、data_suspect 列に入れる。
    """
    calendar = benchmark.index
    rows = []
    for n in horizons:
        hit = results[results[f"excess_{n}"].abs() >= threshold]
        for r in hit.itertuples(index=False):
            entry = calendar.searchsorted(pd.Timestamp(r.entry_date))
            window = calendar[max(entry - 1, 0):entry + n]
            suspect = any(
                not find_suspicious_moves(df.loc[(df.index >= window[0]) & (df.index <= window[-1])], max_move).empty
                for df in (prices.get(r.code), benchmark) if df is not None
            )
            rows.append({"N": n, "data_date": r.data_date.date(), "code": r.code, "name": r.name,
                         "entry_date": r.entry_date, "ret": getattr(r, f"ret_{n}"),
                         "bench": getattr(r, f"bench_{n}"), "excess": getattr(r, f"excess_{n}"),
                         "data_suspect": suspect})
    columns = ["N", "data_date", "code", "name", "entry_date", "ret", "bench", "excess", "data_suspect"]
    return pd.DataFrame(rows, columns=columns)


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
        "- 超過リターン = 銘柄の騰落率 − 同じ期間の TOPIX ETF の騰落率（どちらも配当込み）",
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
            f"- 偶然かどうかの目安（t値。期間の重なりを考えて、実質 {s['independent_dates']} 日分として計算）: "
            f"順位相関 {fmt_value(s['ic_t'], 2, sign=True)}、上位−それ以外 {fmt_value(s['spread_t'], 2, sign=True)}"
            "（絶対値が2未満なら、偶然でも普通に出る程度）",
            "",
        ]

    if outliers is not None and not outliers.empty:
        suspects = int(outliers["data_suspect"].sum())
        lines += [f"## 極端な値（超過リターン ±{OUTLIER_PCT:.0f}% 以上）: {len(outliers)} 件", ""]
        if suspects:
            lines += [
                f"うち {suspects} 件は、期間中の株価に異常な値動きがあり、データの誤りが疑われます。"
                "原因が分かるまで上の平均は信用しないでください（中央値のほうがまだ当てになります）。",
                "",
            ]
        else:
            lines += [
                "期間中の株価に異常な値動きは見つからなかったので、実際の大きな値動きと考えられます。"
                "平均はこうした少数の回に大きく引っ張られるので、中央値とあわせて見てください。",
                "",
            ]
        lines += [
            "| N | 基準日 | コード | 銘柄 | 買った日 | 騰落率 | TOPIX ETF | 超過 | データ異常 |",
            "|---:|---|---|---|---|---:|---:|---:|---|",
        ]
        for r in outliers.head(30).itertuples(index=False):
            lines.append(f"| {r.N} | {r.data_date} | {r.code} | {r.name} | {r.entry_date} | "
                         f"{r.ret:+.1f}% | {r.bench:+.1f}% | {r.excess:+.1f}% | {'あり' if r.data_suspect else 'なし'} |")
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
