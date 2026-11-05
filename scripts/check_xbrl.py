"""保存済みの XBRL の数字（data/xbrl/）を点検する。ネットにはつながない。

使い方（プロジェクトのフォルダで）:
    python scripts/check_xbrl.py

表示するもの:
  1. 取得結果の内訳（ok / not_found / error）
  2. 決算短信で、売上・利益・会社予想が取り出せた割合
  3. 利益の数字を1つも取り出せなかった開示の一覧（項目名が想定外の会社を見つけるため）
  4. 業績予想の修正の中身（修正前 → 修正後）と、上方・下方の件数
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kabu.config import load_settings  # noqa: E402
from kabu.data.xbrl_figures import revision_change, revision_summary  # noqa: E402
from kabu.data.xbrl_store import load_fetch_log, load_key_figures  # noqa: E402

PROFIT_ITEMS = {"operating_income", "ordinary_income", "pretax_income", "net_income"}


def main() -> int:
    settings = load_settings()
    log = load_fetch_log(settings)
    figures = load_key_figures(settings)
    if log.empty:
        print("まだ XBRL を取得していません。先に run_daily.py を実行してください")
        return 1

    print("=== 1. 取得結果 ===")
    latest = log.drop_duplicates("id", keep="last")
    for status, n in latest["status"].value_counts().items():
        print(f"  {status}: {n} 件")

    ok = latest[latest["status"] == "ok"].set_index("id")
    by_id = {i: g for i, g in figures.groupby("id")}
    summaries = ok[ok["title"].str.contains("決算短信")]
    revisions = ok[ok["title"].str.contains("業績予想")]

    print("\n=== 2. 決算短信から取り出せた項目 ===")
    if summaries.empty:
        print("  決算短信はまだありません")
    else:
        def has(i, cond):
            g = by_id.get(i)
            return g is not None and bool(cond(g).any())
        checks = {
            "売上": lambda g: g["item"] == "revenue",
            "営業利益": lambda g: g["item"] == "operating_income",
            "利益（どれか1つ）": lambda g: g["item"].isin(PROFIT_ITEMS),
            "会社の通期予想（利益）": lambda g: g["item"].isin(PROFIT_ITEMS) & (g["kind"] == "forecast")
            & g["period"].isin(["CurrentYearDuration", "NextYearDuration"]),
        }
        for label, cond in checks.items():
            n = sum(has(i, cond) for i in summaries.index)
            print(f"  {label}: {n} / {len(summaries)} 件")

    print("\n=== 3. 利益の数字を1つも取り出せなかった開示（最大20件）===")
    missing = [i for i in ok.index if not (i in by_id and by_id[i]["item"].isin(PROFIT_ITEMS).any())]
    if not missing:
        print("  ありません")
    for i in missing[:20]:
        r = ok.loc[i]
        print(f"  {r['code']} {str(r['pubdate'])[:16]} {r['title'][:50]}")
    if len(missing) > 20:
        print(f"  ……ほか {len(missing) - 20} 件")

    print("\n=== 4. 業績予想の修正の中身 ===")
    up = down = unknown = 0
    lines = []
    for i in revisions.index:
        g = by_id.get(i)
        if g is None:
            unknown += 1
            continue
        change = revision_change(g)
        if change is None:
            unknown += 1
        elif change > 0:
            up += 1
        elif change < 0:
            down += 1
        text = revision_summary(g) or "（修正前後の予想がそろわない）"
        lines.append(f"  {revisions.loc[i, 'code']} {str(revisions.loc[i, 'pubdate'])[:10]} {text}")
    print(f"  上方 {up} 件 / 下方 {down} 件 / 方向を決められない {unknown} 件"
          "（通期の利益予想で判断。赤字予想どうしや通期予想がない場合は「決められない」）")
    for line in lines[:30]:
        print(line)
    if len(lines) > 30:
        print(f"  ……ほか {len(lines) - 30} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
