"""適時開示の種類ごとに、開示のあと株価がどう動いたかを集計する（イベントスタディ）。

使い方（プロジェクトのフォルダで）:
    python scripts/event_study.py                        # 直近1年分
    python scripts/event_study.py --start 2025-11-01     # 期間を指定

開示は手元にない日の分だけ取りに行く（backfill.py で取った分はそのまま使う）。
株価は手元に保存済みのものを使う。対象は settings.yaml の対象銘柄（今は TOPIX500 相当）。
結果は output/evaluation/ にレポート（.md）と1件ずつの明細（.csv）で保存する。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.disclosures import prepare_disclosure_events  # noqa: E402
from kabu.data.prices import load_cached_prices  # noqa: E402
from kabu.data.universe import build_universe, universe_label  # noqa: E402
from kabu.evaluation import codes_without_dividends  # noqa: E402
from kabu.event_study import build_event_report, event_returns, summarize_events, summary_lines  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402
from kabu.scoring import load_weights  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="適時開示の種類ごとの値動き")
    parser.add_argument("--start", help="対象の開示の開始日（省略すると、手元の株価の最新日の1年前）")
    parser.add_argument("--end", help="対象の開示の終了日（省略すると、手元の株価の最新日）")
    args = parser.parse_args()

    now = now_jst()
    settings = load_settings()
    weights = load_weights()
    horizons = settings["evaluation"]["horizons"]
    quality = QualityLog()
    out_dir = resolve_path(settings["paths"]["evaluation"])
    out_dir.mkdir(parents=True, exist_ok=True)

    universe = build_universe(settings, quality)
    benchmark = settings["benchmark"]["code"]
    prices = load_cached_prices(list(universe["code"]) + [benchmark], settings, quality)
    if benchmark not in prices:
        print("TOPIX ETF（1306）の株価が手元にありません。先に run_daily.py を実行してください")
        return 1
    latest = prices[benchmark].index[-1]
    end = pd.Timestamp(args.end) if args.end else latest
    start = pd.Timestamp(args.start) if args.start else end - pd.DateOffset(years=1)

    _, failed, classified = prepare_disclosure_events(
        start.date(), end.date(), settings, weights["disclosure"], quality, now.date())
    events = classified[classified["code"].isin(set(universe["code"]))]
    events = events[events["code"].isin(prices.keys())]
    if events.empty:
        print("対象の期間・銘柄に、分類できる開示がありませんでした")
        return 1
    print(f"{start.date()} 〜 {end.date()} の開示のうち、対象銘柄で分類できたもの: {len(events)} 件。値動きを計算しています…")

    no_dividends = codes_without_dividends(prices)
    if no_dividends:
        quality.warn("evaluate", "", f"調整後終値がない {len(no_dividends)} 銘柄は、配当を含めずに株価だけで計算しています"
                                     f"（{', '.join(no_dividends[:10])}{' など' if len(no_dividends) > 10 else ''}）")
    results = event_returns(events.reset_index(drop=True), prices, prices[benchmark], horizons)
    summary = summarize_events(results, horizons)

    notes = [f"手元の株価は {latest.date()} まで。それより後の値動きが必要な分は集計から外しています。"]
    if failed:
        notes.append(f"開示を取得できなかった日が {len(failed)} 日あります（{failed[0]} など）。その日の開示は入っていません。")
    report = build_event_report(summary, horizons, (start.date(), end.date()), universe_label(settings), notes)
    stamp = now.strftime("%Y-%m-%d")
    report_path = out_dir / f"{stamp}_events_report.md"
    detail_path = out_dir / f"{stamp}_events_detail.csv"
    report_path.write_text(report, encoding="utf-8-sig")
    results.to_csv(detail_path, index=False, encoding="utf-8-sig")

    print("\n".join(summary_lines(summary, horizons, "開示の種類ごとの値動き")))
    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)
    print(f"レポート: {report_path}")
    print(f"明細:     {detail_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
