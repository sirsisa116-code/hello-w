"""毎日1回実行する入口。

使い方（プロジェクトのフォルダで）:
    python scripts/run_daily.py

現在は段階1: 銘柄一覧を作り、株価を最新にして、取得状況と品質ログを保存する。
ランキングは段階2で追加する。
"""

import sys
from pathlib import Path

# src フォルダの中の kabu を import できるようにする
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kabu.config import load_settings, now_jst, resolve_path  # noqa: E402
from kabu.data.prices import update_prices  # noqa: E402
from kabu.data.universe import build_universe  # noqa: E402
from kabu.quality import QualityLog  # noqa: E402


def main() -> int:
    now = now_jst()
    settings = load_settings()
    quality = QualityLog()
    out_dir = resolve_path(settings["paths"]["daily_output"])
    stamp = now.strftime("%Y-%m-%d")

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

    status_path = out_dir / f"{stamp}_prices_status.csv"
    quality_path = out_dir / f"{stamp}_quality.csv"
    out_dir.mkdir(parents=True, exist_ok=True)
    status.to_csv(status_path, index=False, encoding="utf-8-sig")
    quality.save(quality_path)

    ok = len(prices)
    print(f"株価取得: {ok} / {len(codes)} 銘柄")
    print("--- データの問題 ---")
    for line in quality.summary_lines():
        print(line)
    print(f"取得状況: {status_path}")
    print(f"品質ログ: {quality_path}")

    # 1銘柄も取れなかったら異常終了にする（後で自動実行するときに失敗を検知できるように）
    return 0 if ok > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
