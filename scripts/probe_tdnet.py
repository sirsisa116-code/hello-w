"""適時開示（TDnet）の非公式 WebAPI が、どこまで過去にさかのぼれるかを調べる確認用スクリプト。

使い方（プロジェクトのフォルダで）:
    python scripts/probe_tdnet.py

今日から 1週間前・1か月前・3か月前・半年前・1年前・2年前・3年前 の平日について、
その日の開示が何件取れるかを1回ずつ問い合わせる（全部で10回ほど、各2秒の間隔をあける）。
最後に、取れた開示の1件分の中身をそのまま表示する（どんな項目があるかを確かめるため）。

開示が多い日に、1回の問い合わせで全部取れるか（途中で切れないか）を調べる:
    python scripts/probe_tdnet.py --day 20260514
件数の上限を 100・1000・10000 と変えて同じ日を3回問い合わせ、返ってくる件数・時刻の範囲・
1時間ごとの件数などを表示する。日付を省略すると、決算発表が集中しやすい 2026-05-14 と 2026-05-15 を調べる。

何も保存しない・何も書き換えない。調べた結果は画面に出すだけ。
"""

import argparse
import json
import sys
import time
from collections import Counter
from datetime import date, timedelta

import requests

BASE_URL = "https://webapi.yanoshin.jp/webapi/tdnet/list"
# 問い合わせ元が分かるように、ツールの名前を名乗る（個人情報は入れない）
HEADERS = {"User-Agent": "kabu-ranking-tool/0.1 (personal research)"}
PAUSE_SECONDS = 2.0

# 何日前を調べるか（表示名, 日数）
TARGETS = [
    ("1週間前", 7),
    ("1か月前", 30),
    ("3か月前", 91),
    ("半年前", 182),
    ("1年前", 365),
    ("1年半前", 547),
    ("2年前", 730),
    ("3年前", 1095),
]


def last_weekday(d: date) -> date:
    """土日なら直前の金曜日にする（休日は開示がほぼないため）。"""
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch(condition: str, limit: int) -> tuple[dict | None, str]:
    """1回問い合わせる。戻り値は (読めた JSON, 失敗したときの説明)。"""
    url = f"{BASE_URL}/{condition}.json"
    try:
        response = requests.get(url, params={"limit": limit}, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        return None, f"通信エラー: {e}"
    if response.status_code != 200:
        return None, f"HTTP {response.status_code}: {response.text[:200]!r}"
    try:
        return response.json(), ""
    except ValueError:
        return None, f"JSON として読めない応答: {response.text[:300]!r}"


def count_items(data: dict) -> tuple[int | None, int]:
    """全体の件数（分かれば）と、今回返ってきた件数。"""
    items = data.get("items") or []
    total = data.get("total_count")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return total, len(items)


def unwrap(item: dict) -> dict:
    """1件分は {"Tdnet": {...}} の形で入っているので、中身を取り出す。"""
    return item.get("Tdnet", item) if isinstance(item, dict) else {}


def probe_busy_day(day: str) -> None:
    print(f"=== {day} の開示を、件数の上限を変えて問い合わせ ===")
    biggest = None
    for i, limit in enumerate((100, 1000, 10000)):
        if i > 0:
            time.sleep(PAUSE_SECONDS)
        data, error = fetch(day, limit=limit)
        if data is None:
            print(f"  上限 {limit:>5}: 失敗 … {error}")
            continue
        total, returned = count_items(data)
        print(f"  上限 {limit:>5}: 受け取った {returned}件 / total_count {total}")
        if biggest is None or returned > len(biggest.get("items") or []):
            biggest = data
    if not biggest or not biggest.get("items"):
        print("  開示を受け取れませんでした")
        return

    items = [unwrap(i) for i in biggest["items"]]
    times = sorted(i.get("pubdate", "") for i in items)
    days = Counter(t[:10] for t in times)
    hours = Counter(t[11:13] for t in times if t[:10] == f"{day[:4]}-{day[4:6]}-{day[6:]}")
    ids = [i.get("id") for i in items]
    print(f"  一番多く受け取った回の件数: {len(items)}件（id の重複 {len(ids) - len(set(ids))}件）")
    print(f"  時刻の範囲: {times[0]} 〜 {times[-1]}")
    print(f"  日付ごとの件数: {dict(sorted(days.items()))}")
    print(f"  {day} の1時間ごとの件数: {dict(sorted(hours.items()))}")
    with_xbrl = [i for i in items if i.get("url_xbrl")]
    print(f"  XBRL（決算データ）付きの開示: {len(with_xbrl)}件")
    print(f"  応答の actions: {json.dumps(biggest.get('actions'), ensure_ascii=False)[:300]}")
    print(f"  応答の condition_desc: {biggest.get('condition_desc')}")
    if with_xbrl:
        print("  --- XBRL 付き開示の例（そのまま）---")
        print(json.dumps(with_xbrl[0], ensure_ascii=False, indent=2))
    print()


def main() -> int:
    parser = argparse.ArgumentParser(description="TDnet WebAPI の確認")
    parser.add_argument("--day", nargs="?", const="busy", help="開示が多い日を調べる（例: --day 20260514）")
    args = parser.parse_args()
    if args.day:
        days = ["20260514", "20260515"] if args.day == "busy" else [args.day]
        for i, day in enumerate(days):
            if i > 0:
                time.sleep(PAUSE_SECONDS)
            probe_busy_day(day)
        return 0

    today = date.today()
    print(f"調査日: {today}  問い合わせ先: {BASE_URL}")
    print("-" * 60)

    sample = None
    results = []
    conditions = [("直近", "recent")] + [
        (label, last_weekday(today - timedelta(days=days)).strftime("%Y%m%d")) for label, days in TARGETS
    ]
    for i, (label, condition) in enumerate(conditions):
        if i > 0:
            time.sleep(PAUSE_SECONDS)
        data, error = fetch(condition, limit=5)
        if data is None:
            print(f"{label:6} {condition:>10}: 失敗 … {error}")
            results.append((label, condition, None))
            continue
        total, returned = count_items(data)
        total_text = "不明" if total is None else f"{total}件"
        print(f"{label:6} {condition:>10}: その日の開示 {total_text}（今回受け取った {returned}件）")
        results.append((label, condition, total if total is not None else returned))
        if sample is None and data.get("items"):
            sample = data["items"][0]
            sample_keys = sorted(data.keys())

    print("-" * 60)
    reachable = [label for label, _, n in results[1:] if n]
    if reachable:
        print(f"開示が取れた一番古い時点: {reachable[-1]}")
    else:
        print("過去の日付の開示は1件も取れませんでした")

    if sample is not None:
        print("\n--- 応答全体の項目名 ---")
        print(sample_keys)
        print("--- 開示1件分の中身（そのまま） ---")
        print(json.dumps(sample, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
