"""過去のランキングと、その後の実際の値動きを比べる（事後検証）。

使い方（プロジェクトのフォルダで）:
    毎日実際に作ったランキングを検証する:
        python scripts/evaluate.py
    後付けで再現したランキングを検証する:
        python scripts/evaluate.py --source backfill

株価は手元に保存済みのものを使う（ネットにはつながない）。
最新の値動きまで入れたいときは、先に run_daily.py を実行しておく。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.prices import load_cached_prices  # noqa: E402
from kabu.evaluation import (  # noqa: E402
    MISSING, OUTLIER_PCT, build_evaluation_report, find_outliers, fmt_value, forward_returns, summarize,
)
from kabu.quality import QualityLog  # noqa: E402

SOURCES = {
    "live": ("history", "毎日のランキング"),
    "backfill": ("history_backfill", "後付けで再現したランキング"),
}


def main() -> int:
    parser = argparse.ArgumentParser(description="過去のランキングの事後検証")
    parser.add_argument("--source", choices=list(SOURCES), default="live",
                        help="live: 毎日のランキング（既定） / backfill: 後付けで再現したランキング")
    parser.add_argument("--top-n", type=int, help="「上位」とみなす銘柄数（既定は settings.yaml の値）")
    parser.add_argument("--all-versions", action="store_true",
                        help="点数ルールの version が違うランキングも混ぜて集計する")
    args = parser.parse_args()

    now = now_jst()
    settings = load_settings()
    conf = settings["evaluation"]
    top_n = args.top_n or conf["top_n"]
    horizons = conf["horizons"]
    quality = QualityLog()
    path_key, source_label = SOURCES[args.source]
    history_path = resolve_path(settings["paths"][path_key])
    out_dir = resolve_path(settings["paths"]["evaluation"])
    out_dir.mkdir(parents=True, exist_ok=True)

    if not history_path.exists():
        hint = "run_daily.py" if args.source == "live" else "backfill.py"
        print(f"{history_path} がありません。先に scripts/{hint} を実行してください")
        return 1
    history = pd.read_csv(history_path, dtype={"code": str}, encoding="utf-8-sig")

    # 点数ルールが違うランキングを混ぜると、何を検証しているのか分からなくなるので、既定では最新だけ
    versions = sorted(history["weights_version"].unique())
    excluded_versions = []
    if not args.all_versions and len(versions) > 1:
        excluded_versions = versions[:-1]
        history = history[history["weights_version"] == versions[-1]]
        versions = versions[-1:]

    # 対象銘柄の範囲（20銘柄・TOPIX500 など）が違うランキングも混ぜない。一番新しい日の範囲だけを使う
    if "universe" not in history.columns:
        history["universe"] = "watchlist"
    latest_universe = history.loc[pd.to_datetime(history["data_date"]).idxmax(), "universe"]
    excluded_universes = sorted(set(history["universe"]) - {latest_universe})
    history = history[history["universe"] == latest_universe]

    benchmark = settings["benchmark"]["code"]
    codes = sorted(history["code"].unique())
    prices = load_cached_prices(codes + [benchmark], settings, quality)
    if benchmark not in prices:
        print("TOPIX ETF（1306）の株価が手元にありません。先に run_daily.py を実行してください")
        return 1
    for code in codes:
        if code not in prices:
            quality.error("evaluate", code, "手元に株価がないため、この銘柄の成績は計算できません")

    results = forward_returns(history, prices, prices[benchmark], horizons)
    summary = summarize(results, horizons, top_n)
    outliers = find_outliers(results, horizons, prices, prices[benchmark], settings["prices"]["max_daily_move"])
    suspects = int(outliers["data_suspect"].sum())
    if suspects:
        quality.error("evaluate", "", f"超過リターンが ±{OUTLIER_PCT:.0f}% を超え、期間中の株価にも異常な値動きがある回が"
                                      f" {suspects} 件あります。株価データの誤りの可能性が高く、平均は信用できません。"
                                      "scripts/check_prices.py で株価を点検してください")
    if len(outliers) > suspects:
        names = ", ".join(sorted(set(outliers.loc[~outliers["data_suspect"], "code"])))
        quality.warn("evaluate", "", f"超過リターンが ±{OUTLIER_PCT:.0f}% を超える回が {len(outliers) - suspects} 件あります"
                                     f"（銘柄: {names}）。株価に異常はなく、実際の大きな値動きと考えられます。"
                                     "平均が引っ張られやすいので中央値もあわせて見てください")

    missing = results[[f"status_{n}" for n in horizons]].eq(MISSING).any(axis=1)
    for r in results[missing].drop_duplicates("code").itertuples(index=False):
        if r.code in prices:
            quality.warn("evaluate", r.code, "買値・売値の日の株価がない回があります（売買停止・データ欠損の可能性）。その回は集計から外しました")

    latest = prices[benchmark].index[-1].date()
    notes = [f"対象銘柄の範囲: {latest_universe}"
             + (f"（範囲の違う {', '.join(excluded_universes)} の分は混ぜずに外しています）" if excluded_universes else ""),
             f"手元の株価は {latest} まで。これより後の値動きが必要な分は「未確定」として外しています。"]
    if args.source == "backfill":
        notes.append("後付けの再現は「今の」対象銘柄で計算しています。上場廃止になった銘柄などが入らないため、"
                     "成績は実際より良く見えやすいです。")
    if quality.issues:
        notes.append(f"データの問題が {len(quality.issues)} 件あります（evaluate_quality.csv を見てください）。")

    dates = pd.to_datetime(history["data_date"])
    report = build_evaluation_report(
        summary, source_label, versions, excluded_versions,
        (dates.min().date(), dates.max().date()), top_n, notes, outliers,
    )
    stamp = now.strftime("%Y-%m-%d")
    report_path = out_dir / f"{stamp}_{args.source}_report.md"
    detail_path = out_dir / f"{stamp}_{args.source}_detail.csv"
    report_path.write_text(report, encoding="utf-8-sig")
    results.to_csv(detail_path, index=False, encoding="utf-8-sig")
    quality.save(out_dir / "evaluate_quality.csv")

    print(f"=== 事後検証（{source_label}） ===")
    for n, s in summary.items():
        if s["dates"] == 0:
            print(f"  {n:>2}営業日後: 確定した結果なし（未確定 {s['pending']} 件）")
            continue
        top = s["table"].iloc[0]
        rest = s["table"].iloc[1]
        print(f"  {n:>2}営業日後: {s['dates']}日分 / 上位{top_n} 平均超過 {fmt_value(top['平均超過リターン(%)'], sign=True)}%"
              f"（中央値 {fmt_value(top['超過リターンの中央値(%)'], sign=True)}%）"
              f" / それ以外 {fmt_value(rest['平均超過リターン(%)'], sign=True)}%"
              f"（中央値 {fmt_value(rest['超過リターンの中央値(%)'], sign=True)}%）"
              f" / 順位相関 {fmt_value(s['ic_mean'], 3, sign=True)}")
    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)
    print(f"レポート: {report_path}")
    print(f"明細:     {detail_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
