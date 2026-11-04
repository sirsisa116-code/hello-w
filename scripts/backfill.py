"""過去の日付のランキングを後から再現して、output/history_backfill.csv に保存する。

使い方（プロジェクトのフォルダで）:
    初回（株価の履歴を長く取り直してから再現する。数分かかる）:
        python scripts/backfill.py --fetch-days 900
    2回目以降（手元の株価だけで再現し直す。点数ルールを変えたときなど）:
        python scripts/backfill.py
    期間を指定する:
        python scripts/backfill.py --start 2026-01-05 --end 2026-06-30

実行するたびに history_backfill.csv は丸ごと作り直す。
毎日の実行で作る history.csv には触らない。
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.backfill import backfill, default_start  # noqa: E402
from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.prices import load_cached_prices, update_prices  # noqa: E402
from kabu.data.universe import build_universe, universe_label  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402
from kabu.scoring import load_weights  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="過去の日付のランキングを再現する")
    parser.add_argument("--fetch-days", type=int, help="この日数分（カレンダー日）の株価を取り直してから再現する")
    parser.add_argument("--start", help="再現を始める日（例: 2026-01-05）。省略すると、計算に必要な履歴がそろう最初の日")
    parser.add_argument("--end", help="再現を終える日。省略すると手元の最新日")
    args = parser.parse_args()

    now = now_jst()
    settings = load_settings()
    weights = load_weights()
    quality = QualityLog()
    out_dir = resolve_path(settings["paths"]["evaluation"])
    out_dir.mkdir(parents=True, exist_ok=True)

    universe = build_universe(settings, quality)
    benchmark = settings["benchmark"]["code"]
    codes = list(universe["code"]) + [benchmark]

    if args.fetch_days:
        print(f"株価を {args.fetch_days} 日分取り直しています…（銘柄数が多いと数分かかります）")
        prices, _ = update_prices(codes, settings, quality, now, full_days=args.fetch_days)
    else:
        prices = load_cached_prices(codes, settings, quality)
        for code in codes:
            if code not in prices:
                quality.error("backfill", code, "手元に株価がありません。先に run_daily.py か --fetch-days 付きで実行してください")

    if benchmark not in prices:
        quality.error("backfill", benchmark, "TOPIX ETF の株価がないため、営業日を決められません")
        _finish(quality, out_dir)
        return 1

    calendar = prices[benchmark].index
    try:
        start = pd.Timestamp(args.start) if args.start else default_start(calendar)
    except ValueError as e:
        quality.error("backfill", "", f"{e}。--fetch-days 900 を付けて実行してください")
        _finish(quality, out_dir)
        return 1
    end = pd.Timestamp(args.end) if args.end else calendar[-1]
    dates = calendar[(calendar >= start) & (calendar <= end)]
    if len(dates) == 0:
        quality.error("backfill", "", f"{start.date()} 〜 {end.date()} に営業日がありません")
        _finish(quality, out_dir)
        return 1

    print(f"{dates[0]:%Y-%m-%d} 〜 {dates[-1]:%Y-%m-%d} の {len(dates)} 営業日分を再現します")
    stock_prices = {c: df for c, df in prices.items() if c != benchmark}
    history, log = backfill(universe, stock_prices, dates, weights, now, universe_label(settings))

    path = resolve_path(settings["paths"]["history_backfill"])
    history.to_csv(path, index=False, encoding="utf-8-sig")
    log.to_csv(out_dir / "backfill_log.csv", index=False, encoding="utf-8-sig")

    warned_days = int((log["missing_indicator_warnings"] > 0).sum())
    if warned_days:
        quality.warn("backfill", "", f"{warned_days} 日分で、計算できない指標がある銘柄がありました"
                                     "（点数0で扱っています。日ごとの件数は backfill_log.csv）")
    empty_days = int((log["ranked"] == 0).sum())
    if empty_days:
        quality.warn("backfill", "", f"{empty_days} 日分は、ランキングに入る銘柄が0でした")
    quality.warn("backfill", "", "銘柄は今の対象銘柄を使っています。上場廃止になった銘柄や、"
                                 "この1〜2年で値上がりして対象に入ってきた銘柄の偏りがあるため、"
                                 "成績は実際より良く見えやすい点に注意してください")

    print(f"保存しました: {path}（{len(history)} 行）")
    _finish(quality, out_dir)
    return 0


def _finish(quality: QualityLog, out_dir: Path) -> None:
    quality.save(out_dir / "backfill_quality.csv")
    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)


if __name__ == "__main__":
    sys.exit(main())
