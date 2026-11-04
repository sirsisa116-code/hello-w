"""手元に保存した全銘柄の株価を、全期間にわたって点検する。

使い方（プロジェクトのフォルダで）:
    python scripts/check_prices.py

前日比 ±30%（settings.yaml の max_daily_move）を超える値動きなど、
株式分割の反映漏れやデータ誤りが疑われる日を一覧にする。ネットにはつながない。
結果は output/price_check.csv にも保存する。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.config import load_settings, resolve_path  # noqa: E402
from kabu.data.prices import find_suspicious_moves, load_cache  # noqa: E402


def main() -> int:
    settings = load_settings()
    max_move = settings["prices"]["max_daily_move"]
    folder = resolve_path(settings["paths"]["raw_prices"])
    files = sorted(folder.glob("*.csv"))
    if not files:
        print(f"{folder} に株価がありません。先に run_daily.py を実行してください")
        return 1

    found = []
    for path in files:
        df = load_cache(path)
        moves = find_suspicious_moves(df, max_move)
        if not moves.empty:
            moves.insert(0, "code", path.stem)
            moves["source"] = [df.at[pd.Timestamp(d), "source"] for d in moves["date"]]
            found.append(moves)

    print(f"{len(files)} 銘柄を点検しました（基準: 1日の値動き ±{max_move * 100:.0f}% 超）")
    if not found:
        print("異常な値動きは見つかりませんでした")
        return 0

    result = pd.concat(found, ignore_index=True)
    out = resolve_path("output") / "price_check.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"{result['code'].nunique()} 銘柄で {len(result)} 件見つかりました:")
    with pd.option_context("display.width", 200, "display.max_rows", 100):
        print(result.to_string(index=False))
    print(f"保存しました: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
