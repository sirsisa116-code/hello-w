"""毎日1回実行する入口。

使い方（プロジェクトのフォルダで）:
    python scripts/run_daily.py

流れ:
  1. 対象銘柄を決める
  2. 株価を最新にする
  3. テクニカル指標を計算して点数を付け、ランキングにする
  4. ランキング CSV・レポート・品質ログを保存し、履歴に追加する

ランキングとレポートのファイル名は「データ基準日」（何日の終値で計算したか）。
週末に実行すれば金曜日の日付になる。取得状況と品質ログは実行した日の日付。
"""

import sys
from datetime import timedelta
from pathlib import Path

# src フォルダの中の kabu を import できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.disclosures import prepare_disclosure_events  # noqa: E402
from kabu.data.prices import update_prices  # noqa: E402
from kabu.data.xbrl_store import collect_xbrl, revision_details  # noqa: E402
from kabu.data.universe import build_universe, universe_label  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402
from kabu.report import append_history, build_report, save_ranking_csv, save_report  # noqa: E402
from kabu.scoring import load_weights, rank_stocks  # noqa: E402


def main() -> int:
    now = now_jst()
    settings = load_settings()
    weights = load_weights()
    quality = QualityLog()
    out_dir = resolve_path(settings["paths"]["daily_output"])
    run_stamp = now.strftime("%Y-%m-%d")
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"=== 実行日時 {now:%Y-%m-%d %H:%M} (日本時間) ===")

    universe = build_universe(settings, quality)
    print(f"対象銘柄: {len(universe)} 銘柄（モード: {settings['universe']['mode']}）")

    benchmark = settings["benchmark"]["code"]
    codes = list(universe["code"]) + [benchmark]
    print("株価を取得しています…")
    prices, status = update_prices(codes, settings, quality, now)

    status = status.merge(universe[["code", "name"]], on="code", how="left")
    status.loc[status["code"] == benchmark, "name"] = settings["benchmark"]["name"]
    status = status[["code", "name", "latest_date", "close", "rows", "source"]]
    status_path = out_dir / f"{run_stamp}_prices_status.csv"
    status.to_csv(status_path, index=False, encoding="utf-8-sig")
    print(f"株価取得: {len(prices)} / {len(codes)} 銘柄")

    report_path = None
    if prices:
        # データ基準日 = 取れた株価の中で一番新しい日
        data_date = max(df.index[-1] for df in prices.values())
        print(f"データ基準日: {data_date:%Y-%m-%d}。点数を計算しています…")
        stock_prices = {c: df for c, df in prices.items() if c != benchmark}
        events, failed = None, None
        if weights["category_weights"].get("disclosure", 0) > 0:
            decay = weights["disclosure"]["decay_days"]
            start = (data_date - timedelta(days=decay + 2)).date()
            # XBRL は TDnet から約1か月で消えるので、毎回まだ取っていない分を取って保存しておく
            saved = collect_xbrl(settings, set(universe["code"]), quality, now.date())
            if saved:
                print(f"XBRL（決算データ）を {saved} 件保存しました")
            events, failed, _ = prepare_disclosure_events(start, now.date(), settings, weights["disclosure"], quality, now.date())
            details = revision_details(settings)
            for df in events.values():
                df["detail"] = df["id"].map(details)
            print(f"適時開示: {now:%Y-%m-%d %H:%M} 時点までに出た分を使います"
                  "（これより後に出た今日の開示は、次回のランキングに入ります）")
        ranking, details, excluded = rank_stocks(universe, stock_prices, data_date, weights, quality,
                                                 disclosure_events=events, disclosure_failed=failed)

        stamp = data_date.strftime("%Y-%m-%d")
        save_ranking_csv(ranking, out_dir / f"{stamp}_ranking.csv")
        if not ranking.empty:
            append_history(ranking, resolve_path(settings["paths"]["history"]), data_date, now, weights["version"],
                           universe_label(settings))
        text = build_report(ranking, details, excluded, quality, data_date, now, weights, settings["report"]["top_n"])
        report_path = out_dir / f"{stamp}_report.md"
        save_report(text, report_path)
        print(f"ランキング入り: {len(ranking)} 銘柄 / 除外: {len(excluded)} 銘柄")
        if not ranking.empty:
            print("--- 上位5銘柄 ---")
            for r in ranking.head(5).itertuples(index=False):
                print(f"  {r.rank}. {r.code} {r.name}  {r.total_score:.1f}点")
    else:
        quality.error("run_daily", "", "株価が1銘柄も取れなかったため、ランキングを作れませんでした")

    quality_path = out_dir / f"{run_stamp}_quality.csv"
    quality.save(quality_path)

    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)
    print(f"取得状況: {status_path}")
    print(f"品質ログ: {quality_path}")
    if report_path:
        print(f"レポート: {report_path}")

    # 1銘柄も取れなかったら異常終了にする（後で自動実行するときに失敗を検知できるように）
    return 0 if prices else 1


if __name__ == "__main__":
    sys.exit(main())
