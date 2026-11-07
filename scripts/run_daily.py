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

import pandas as pd

# src フォルダの中の kabu を import できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.disclosures import prepare_disclosure_events  # noqa: E402
from kabu.data.prices import update_prices  # noqa: E402
from kabu.data.xbrl_store import collect_xbrl, revision_details  # noqa: E402
from kabu.data.universe import build_universe, load_jpx_list, universe_label  # noqa: E402
from kabu.analysis.technical import compute_indicator_series, indicators_at  # noqa: E402
from kabu.briefing import load_my_stocks, movers_section, my_stocks_section, new_disclosures_section  # noqa: E402
from kabu.data.xbrl_store import load_key_figures  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402
from kabu.report import append_history, build_report, save_ranking_csv, save_report  # noqa: E402
from kabu.scoring import load_weights, rank_stocks  # noqa: E402


def build_briefing(settings, quality, universe, ranking, prices, my_stocks, classified, data_date) -> list[str]:
    """レポートの「材料の整理」の欄を作る。"""
    jpx = load_jpx_list(settings, quality)
    names = dict(zip(jpx["code"], jpx["name"])) if jpx is not None else {}
    names |= dict(zip(universe["code"], universe["name"]))
    indicators = ranking.set_index("code") if not ranking.empty else pd.DataFrame()
    # 注目銘柄のうちランキングに入っていないもの（対象外の銘柄など）は、ここで指標を計算する
    extra = {c: indicators_at(compute_indicator_series(prices[c]), data_date)
             for c in my_stocks if c in prices and c not in indicators.index}
    if extra:
        indicators = pd.concat([indicators, pd.DataFrame.from_dict(extra, orient="index")])
    figures = load_key_figures(settings)
    if classified is None:
        classified = pd.DataFrame(columns=["id", "pubdate", "code", "company_name", "title", "category", "label"])
    # 「新しい開示」は前の営業日の15時以降（前回のレポートと少し重なるが、抜けは出ない）
    calendar = max((df.index for df in prices.values()), key=len) if prices else pd.DatetimeIndex([data_date])
    earlier = calendar[calendar < data_date]
    since = (earlier[-1] if len(earlier) else data_date - pd.Timedelta(days=1)) + pd.Timedelta(hours=15)
    lines = my_stocks_section(my_stocks, indicators, names, classified, figures, data_date)
    lines += new_disclosures_section(classified, figures, names, since, set(universe["code"]) | set(my_stocks))
    if not indicators.empty:
        lines += movers_section(indicators[indicators.index.isin(universe["code"])], names)
    return lines


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

    # 自分の注目銘柄（config/local/my_stocks.yaml）は、対象銘柄に入っていなくても株価と開示を取る
    my_stocks = load_my_stocks()
    if my_stocks:
        print(f"自分の注目銘柄: {len(my_stocks)} 銘柄")
    benchmark = settings["benchmark"]["code"]
    codes = list(dict.fromkeys(list(universe["code"]) + list(my_stocks) + [benchmark]))
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
        events, failed, classified = None, None, None
        if weights["category_weights"].get("disclosure", 0) > 0:
            decay = weights["disclosure"]["decay_days"]
            start = (data_date - timedelta(days=decay + 2)).date()
            # XBRL は TDnet から約1か月で消えるので、毎回まだ取っていない分を取って保存しておく
            saved = collect_xbrl(settings, set(universe["code"]) | set(my_stocks), quality, now.date())
            if saved:
                print(f"XBRL（決算データ）を {saved} 件保存しました")
            events, failed, classified = prepare_disclosure_events(start, now.date(), settings, weights["disclosure"],
                                                                   quality, now.date())
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
        briefing = build_briefing(settings, quality, universe, ranking, stock_prices, my_stocks, classified, data_date)
        text = build_report(ranking, details, excluded, quality, data_date, now, weights, settings["report"]["top_n"],
                            briefing)
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
