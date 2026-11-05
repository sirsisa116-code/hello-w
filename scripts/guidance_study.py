"""会社予想の変化（業績予想の修正・本決算の上振れ下振れ・来期予想の伸び）のあと、株価がどう動いたかを集計する。

使い方（プロジェクトのフォルダで）:
    python scripts/guidance_study.py              # 未取得の銘柄の決算データを J-Quants から取ってから集計
    python scripts/guidance_study.py --no-fetch   # 手元に保存済みの決算データだけで集計

初回は約500銘柄の決算データを取るので、1時間半以上かかる（無料プランは1分5回まで）。
途中で止めても、もう一度実行すれば続きから取る。
J-Quants の無料プランは約2年分・12週間遅れなので、集計できるのは過去の分だけ（毎日のランキングには使わない）。
結果は output/evaluation/ にレポート（.md）と明細（.csv）で保存する。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.analysis.guidance import guidance_events  # noqa: E402
from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.jquants import JQuantsError, get_api_key  # noqa: E402
from kabu.data.jquants_statements import fetch_statements, load_statements  # noqa: E402
from kabu.data.prices import load_cached_prices  # noqa: E402
from kabu.data.universe import build_universe, universe_label  # noqa: E402
from kabu.event_study import build_event_report, event_returns, summarize_events, summary_lines  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402

KIND_ORDER = {"revision": 0, "fy_surprise": 1, "new_guidance": 2}


def main() -> int:
    parser = argparse.ArgumentParser(description="会社予想の変化のあとの値動き")
    parser.add_argument("--no-fetch", action="store_true", help="J-Quants に問い合わせず、保存済みの分だけで集計する")
    args = parser.parse_args()

    now = now_jst()
    settings = load_settings()
    horizons = settings["evaluation"]["horizons"]
    quality = QualityLog()
    out_dir = resolve_path(settings["paths"]["evaluation"])
    out_dir.mkdir(parents=True, exist_ok=True)

    universe = build_universe(settings, quality)
    codes = list(universe["code"])
    if not args.no_fetch:
        try:
            api_key = get_api_key()
        except JQuantsError as e:
            print(f"失敗: {e}")
            return 1
        fetch_statements(codes, settings, api_key, quality)

    statements = load_statements(codes, settings)
    if statements.empty:
        print("決算データがありません。--no-fetch を付けずに実行してください")
        return 1
    print(f"決算データ: {statements['code'].nunique()} 銘柄・{len(statements)} 件"
          f"（開示日 {statements['disclosed_at'].min():%Y-%m-%d} 〜 {statements['disclosed_at'].max():%Y-%m-%d}）")
    next_cols = [c for c in statements.columns if c.startswith("NxF")]
    print(f"来期予想の項目: {', '.join(next_cols) if next_cols else 'なし（本決算の F〜 の項目を来期予想とみなす）'}")

    benchmark = settings["benchmark"]["code"]
    prices = load_cached_prices(codes + [benchmark], settings, quality)
    if benchmark not in prices:
        print("TOPIX ETF（1306）の株価が手元にありません。先に run_daily.py を実行してください")
        return 1

    events = guidance_events(statements)
    events = events[events["code"].isin(prices.keys()) & events["disclosed_at"].notna()]
    if events.empty:
        print("集計できる出来事がありませんでした")
        return 1
    print(f"出来事: {len(events)} 件（{', '.join(f'{k} {n}件' for k, n in events['kind'].value_counts().items())}）")

    names = dict(zip(universe["code"], universe["name"]))
    study_input = pd.DataFrame({
        "pubdate": events["disclosed_at"].to_numpy(), "code": events["code"].to_numpy(),
        "company_name": events["code"].map(names).to_numpy(), "title": events["doc_type"].to_numpy(),
        "category": events["category"].to_numpy(), "label": events["label"].to_numpy(),
        "points": (events["change"] * 100).to_numpy(),
    })
    results = event_returns(study_input, prices, prices[benchmark], horizons)
    results["kind"] = events["kind"].to_numpy()
    results["metric"] = events["metric"].to_numpy()
    results["change_pct"] = (events["change"] * 100).round(2).to_numpy()
    summary = summarize_events(results, horizons)
    summary["_order"] = summary["category"].map(lambda c: (KIND_ORDER.get(c.rsplit("_", 1)[0], 9), c))
    summary = summary.sort_values("_order").drop(columns="_order").reset_index(drop=True)

    latest = prices[benchmark].index[-1]
    notes = [
        f"手元の株価は {latest.date()} まで。それより後の値動きが必要な分は集計から外しています。",
        "決算データは J-Quants の無料プラン（約2年分・12週間遅れ）。利益は営業利益→経常利益→純利益の順で、"
        "比べる2つがそろう最初のものを使い、元の数字が0以下のものは除いています。",
        "銘柄は「今の」対象銘柄なので、上場廃止になった銘柄などが入っていません。",
    ]
    period = (events["disclosed_at"].min().date(), events["disclosed_at"].max().date())
    report = build_event_report(summary, horizons, period, universe_label(settings), notes,
                                title="会社予想の変化のあとの値動き（イベントスタディ）")
    stamp = now.strftime("%Y-%m-%d")
    report_path = out_dir / f"{stamp}_guidance_report.md"
    detail_path = out_dir / f"{stamp}_guidance_detail.csv"
    report_path.write_text(report, encoding="utf-8-sig")
    results.to_csv(detail_path, index=False, encoding="utf-8-sig")

    print("\n".join(summary_lines(summary, horizons, "会社予想の変化のあとの値動き")))
    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)
    print(f"レポート: {report_path}")
    print(f"明細:     {detail_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
