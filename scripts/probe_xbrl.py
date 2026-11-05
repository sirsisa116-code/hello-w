"""適時開示に付いている XBRL（決算データ）の zip の中身を調べる確認用スクリプト。

使い方（プロジェクトのフォルダで）:
    python scripts/probe_xbrl.py --local
        前に取ってきて保存した zip（data/raw/tdnet_xbrl/）を読み直す。ネットにはつながない
    python scripts/probe_xbrl.py --codes 7203,4502,8306
        指定した銘柄について、手元の開示一覧にある最新の決算短信と業績予想の修正の XBRL を取ってきて読む
        （1件ごとに2秒あける。前に取ったものは取り直さない）
    python scripts/probe_xbrl.py
        手元の開示一覧のうち、XBRL 付きの割合だけを数える

読み取りには、本番用の部品（src/kabu/data/xbrl.py）を使う。zip は展開せずに読む。
"""

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402
import requests  # noqa: E402

from kabu.config import load_settings, resolve_path  # noqa: E402
from kabu.data.disclosures import HEADERS, parse_items  # noqa: E402
from kabu.data.xbrl import read_xbrl_zip  # noqa: E402

PAUSE_SECONDS = 2.0
MAX_LINES = 70
KINDS = {
    "決算短信": lambda t: "決算短信" in t,
    "業績予想の修正": lambda t: "業績予想" in t and "修正" in t,
    "配当予想の修正": lambda t: "配当予想" in t and "修正" in t,
}


def direct_url(url: str) -> str:
    """やのしんの転送用 URL（rd.php?https://...）から、TDnet の元の URL を取り出す。"""
    return url.split("rd.php?", 1)[1] if "rd.php?" in url else url


def load_cached_disclosures(folder: Path) -> pd.DataFrame:
    frames = [parse_items(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(folder.glob("*.json"))]
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    return df.drop_duplicates("id").sort_values("pubdate").reset_index(drop=True)


def show_facts(content: bytes) -> None:
    facts = read_xbrl_zip(content)
    if facts.empty:
        print("  inline XBRL（-ixbrl.htm）が見つかりませんでした")
        return
    for (file, doc_type), g in facts.groupby(["file", "doc_type"], sort=False):
        numbers = g[g["kind"] == "number"]
        filled = numbers[numbers["value"].notna()]
        print(f"\n  --- [{doc_type}] {file.rsplit('/', 1)[-1]}: 数値 {len(numbers)} 件（値あり {len(filled)} 件）"
              f" / 文字 {int((g['kind'] == 'text').sum())} 件 ---")
        if filled.empty:
            texts = g[g["kind"] == "text"]["name"].unique()[:20]
            print(f"  文字の項目名（最大20）: {', '.join(texts)}")
            continue
        print("  値のある数値（名前 | contextRef | 値 | 表示されている文字）:")
        for r in filled.head(MAX_LINES).itertuples(index=False):
            value = f"{r.value:,.0f}" if abs(r.value) >= 1000 else f"{r.value:.4g}"
            print(f"    {r.prefix}:{r.name} | {r.context} | {value} | {r.text[:20]}")
        if len(filled) > MAX_LINES:
            print(f"    ……ほか {len(filled) - MAX_LINES} 件")


def fetch_zip(row, save_dir: Path) -> bytes | None:
    path = save_dir / f"{row.id}.zip"
    if path.exists():
        return path.read_bytes()
    url = direct_url(row.url_xbrl)
    try:
        response = requests.get(url, headers=HEADERS, timeout=60)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"  取得失敗: {e}")
        return None
    path.write_bytes(response.content)
    return response.content


def main() -> int:
    parser = argparse.ArgumentParser(description="XBRL の中身を調べる")
    parser.add_argument("--local", action="store_true", help="保存済みの zip を読み直す（ネットにつながない）")
    parser.add_argument("--codes", help="調べる銘柄コード（カンマ区切り）")
    args = parser.parse_args()

    settings = load_settings()
    save_dir = resolve_path("data/raw/tdnet_xbrl")
    save_dir.mkdir(parents=True, exist_ok=True)
    df = load_cached_disclosures(resolve_path(settings["paths"]["raw_tdnet"]))

    if args.local:
        zips = sorted(save_dir.glob("*.zip"))
        if not zips:
            print(f"{save_dir} に zip がありません")
            return 1
        titles = df.set_index("id")[["code", "company_name", "title"]] if not df.empty else None
        for path in zips:
            print("\n" + "=" * 70)
            if titles is not None and path.stem in titles.index:
                info = titles.loc[path.stem]
                print(f"{path.name}: {info['code']} {info['company_name']} / {info['title']}")
            else:
                print(path.name)
            show_facts(path.read_bytes())
        return 0

    if df.empty:
        print("手元に開示一覧がありません。先に backfill.py か run_daily.py を実行してください")
        return 1

    if not args.codes:
        print(f"手元の開示一覧: {df['pubdate'].min():%Y-%m-%d} 〜 {df['pubdate'].max():%Y-%m-%d}、{len(df)} 件")
        for kind, match in KINDS.items():
            sub = df[df["title"].map(match)]
            print(f"  {kind}: {len(sub)} 件中 XBRL 付き {int(sub['has_xbrl'].sum())} 件")
        return 0

    usable = df[df["has_xbrl"] & ~df["title"].str.contains("訂正")]
    first = True
    for code in [c.strip().upper() for c in args.codes.split(",") if c.strip()]:
        for kind in ("決算短信", "業績予想の修正"):
            sub = usable[(usable["code"] == code) & usable["title"].map(KINDS[kind])]
            print("\n" + "=" * 70)
            if sub.empty:
                print(f"[{code} {kind}] 手元の開示一覧に、XBRL 付きのものがありません")
                continue
            row = sub.iloc[-1]
            print(f"[{code} {kind}] {row.pubdate:%Y-%m-%d %H:%M} {row.company_name} / {row.title}")
            if not (save_dir / f"{row.id}.zip").exists():
                if not first:
                    time.sleep(PAUSE_SECONDS)
                first = False
            content = fetch_zip(row, save_dir)
            if content is not None:
                show_facts(content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
