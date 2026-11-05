"""適時開示に付いている XBRL（決算データ）の zip の中身を調べる確認用スクリプト。

使い方（プロジェクトのフォルダで）:
    python scripts/probe_xbrl.py

1. 手元に保存済みの開示一覧（data/raw/tdnet）から、決算短信・業績予想の修正・配当予想の修正のうち
   XBRL 付きのものがどれくらいあるかを数える
2. 決算短信と業績予想の修正を2件ずつ選び、XBRL の zip を取ってくる（全部で4回、各2秒の間隔）
3. zip の中のファイル一覧と、数値データ（売上・利益・予想など）がどんな名前・形で入っているかを表示する

取ってきた zip は data/raw/tdnet_xbrl/ に保存する（展開はせず、読むだけ）。
"""

import io
import json
import re
import sys
import time
import zipfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import requests  # noqa: E402

from kabu.config import load_settings, resolve_path  # noqa: E402
from kabu.data.disclosures import HEADERS, parse_items  # noqa: E402

PAUSE_SECONDS = 2.0
MAX_ENTRY_BYTES = 30_000_000   # これより大きいファイルは読まない（念のため）
FACT_PATTERN = re.compile(r"<ix:(nonFraction|nonNumeric)\b([^>]*)>(.*?)</ix:\1>", re.S)
ATTR_PATTERN = re.compile(r'([\w:]+)="([^"]*)"')
TAG_PATTERN = re.compile(r"<[^>]+>")

KINDS = {
    "決算短信": lambda t: "決算短信" in t,
    "業績予想の修正": lambda t: "業績予想" in t and "修正" in t,
    "配当予想の修正": lambda t: "配当予想" in t and "修正" in t,
}


def direct_url(url: str) -> str:
    """やのしんの転送用 URL（rd.php?https://...）から、TDnet の元の URL を取り出す。"""
    return url.split("rd.php?", 1)[1] if "rd.php?" in url else url


def load_cached_disclosures(folder: Path):
    import pandas as pd
    frames = [parse_items(json.loads(p.read_text(encoding="utf-8"))) for p in sorted(folder.glob("*.json"))]
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    return df.drop_duplicates("id").sort_values("pubdate")


def show_zip(content: bytes) -> None:
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        infos = z.infolist()
        print(f"  zip の中身（{len(infos)} ファイル）:")
        for info in infos:
            print(f"    {info.file_size:>9,} バイト  {info.filename}")
        targets = [i for i in infos if i.filename.lower().endswith((".htm", ".html", ".xhtml"))
                   and i.file_size <= MAX_ENTRY_BYTES]
        for info in targets:
            text = z.read(info).decode("utf-8", errors="replace")
            facts = FACT_PATTERN.findall(text)
            if not facts:
                continue
            numeric = [(dict(ATTR_PATTERN.findall(attrs)), TAG_PATTERN.sub("", body).strip())
                       for kind, attrs, body in facts if kind == "nonFraction"]
            print(f"\n  --- {info.filename}: 数値 {len(numeric)} 件 / 文字 {len(facts) - len(numeric)} 件 ---")
            contexts = Counter(a.get("contextRef", "") for a, _ in numeric)
            print(f"  contextRef の種類（多い順、最大25）:")
            for ctx, n in contexts.most_common(25):
                print(f"    {n:>3} 件  {ctx}")
            print(f"  数値の例（最大60件。名前 / contextRef / scale / sign / 表示されている値）:")
            for attrs, value in numeric[:60]:
                print(f"    {attrs.get('name', '')} | {attrs.get('contextRef', '')} | scale={attrs.get('scale', '')}"
                      f" | sign={attrs.get('sign', '')} | {value}")
        xmls = [i for i in infos if i.filename.lower().endswith(".xml") and i.file_size <= MAX_ENTRY_BYTES]
        if not any(FACT_PATTERN.search(z.read(i).decode("utf-8", errors="replace")) for i in targets) and xmls:
            biggest = max(xmls, key=lambda i: i.file_size)
            print(f"\n  inline XBRL が見つからなかったので、{biggest.filename} の先頭を表示します:")
            print(z.read(biggest).decode("utf-8", errors="replace")[:1500])


def main() -> int:
    settings = load_settings()
    folder = resolve_path(settings["paths"]["raw_tdnet"])
    df = load_cached_disclosures(folder)
    if df.empty:
        print(f"{folder} に開示一覧がありません。先に backfill.py か run_daily.py を実行してください")
        return 1

    print(f"手元の開示一覧: {df['pubdate'].min():%Y-%m-%d} 〜 {df['pubdate'].max():%Y-%m-%d}、{len(df)} 件")
    print("種類ごとの XBRL 付きの割合:")
    for kind, match in KINDS.items():
        sub = df[df["title"].map(match)]
        print(f"  {kind}: {len(sub)} 件中 {int(sub['has_xbrl'].sum())} 件")

    samples = []
    for kind in ("決算短信", "業績予想の修正"):
        sub = df[df["title"].map(KINDS[kind]) & df["has_xbrl"] & ~df["title"].str.contains("訂正")]
        samples += [(kind, row) for row in sub.tail(2).itertuples(index=False)]

    save_dir = resolve_path("data/raw/tdnet_xbrl")
    save_dir.mkdir(parents=True, exist_ok=True)
    for i, (kind, row) in enumerate(samples):
        if i > 0:
            time.sleep(PAUSE_SECONDS)
        url = direct_url(row.url_xbrl)
        print("\n" + "=" * 70)
        print(f"[{kind}] {row.pubdate:%Y-%m-%d %H:%M} {row.code} {row.company_name}")
        print(f"  タイトル: {row.title}")
        print(f"  URL: {url}")
        try:
            response = requests.get(url, headers=HEADERS, timeout=60)
            response.raise_for_status()
        except requests.RequestException as e:
            print(f"  取得失敗: {e}")
            continue
        (save_dir / f"{row.id}.zip").write_bytes(response.content)
        try:
            show_zip(response.content)
        except zipfile.BadZipFile:
            print(f"  zip として読めません（先頭: {response.content[:120]!r}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
