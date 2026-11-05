"""適時開示に付いている XBRL（決算データ）の zip を読む。

zip の中の「inline XBRL」（見た目は HTML で、数値に ix:nonFraction という印が付いたもの）から、
数値と文字の項目を全部取り出す。どの項目を使うか（売上・利益・予想など）はここでは決めない。

読み方の注意（実物を見て分かったこと）:
  - 数値の表が、文章のかたまり（ix:nonNumeric）の中に入れ子になっていることがある
    → 入れ子になっていても、内側の数値をすべて拾う
  - 値が空の項目は <ix:nonFraction ... /> のように1つのタグで書かれる → 値なし（NaN）として扱う
  - 「－」は、format が zerodash なら 0、それ以外は値なし
  - scale は 10 の何乗倍か（6 なら百万円単位、-2 ならパーセント）。sign="-" はマイナス
"""

import io
import re
import zipfile
from html.parser import HTMLParser

import numpy as np
import pandas as pd

MAX_ENTRY_BYTES = 30_000_000   # これより大きいファイルは読まない（念のため）
FACT_TAGS = {"ix:nonfraction": "number", "ix:nonnumeric": "text"}
NUMBER_PATTERN = re.compile(r"[-+]?\d+(?:\.\d+)?")
FACT_COLUMNS = ["kind", "prefix", "name", "context", "unit", "scale", "sign", "format", "nil", "text", "value"]


class _FactParser(HTMLParser):
    """ix:nonFraction / ix:nonNumeric を、入れ子でも全部拾う。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.facts = []
        self.open = []   # 開いている項目（入れ子のとき、文字は外側にも内側にも足す）

    def _new_fact(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        prefix, _, name = a.get("name", "").partition(":")
        return {"kind": FACT_TAGS[tag], "prefix": prefix, "name": name or prefix, "context": a.get("contextref", ""),
                "unit": a.get("unitref", ""), "scale": a.get("scale", ""), "sign": a.get("sign", ""),
                "format": a.get("format", ""), "nil": a.get("xsi:nil", "").lower() == "true", "parts": []}

    def handle_starttag(self, tag, attrs):
        if tag in FACT_TAGS:
            self.open.append(self._new_fact(tag, attrs))

    def handle_startendtag(self, tag, attrs):
        if tag in FACT_TAGS:
            self.facts.append(self._new_fact(tag, attrs))   # 中身のない項目（値なし）

    def handle_endtag(self, tag):
        if tag in FACT_TAGS:
            for i in range(len(self.open) - 1, -1, -1):
                if self.open[i]["kind"] == FACT_TAGS[tag]:
                    self.facts.append(self.open.pop(i))
                    break

    def handle_data(self, data):
        for fact in self.open:
            fact["parts"].append(data)


def to_number(text: str, scale: str, sign: str, fmt: str, nil: bool) -> float:
    """表示されている文字を数値にする（scale・sign を反映）。読めなければ NaN。"""
    if nil:
        return np.nan
    cleaned = text.replace(",", "").replace("，", "").replace(" ", "").strip()
    if cleaned in ("－", "-", "―", "‐", "—", "") and ("zerodash" in fmt or "fixed-zero" in fmt):
        return 0.0
    match = NUMBER_PATTERN.fullmatch(cleaned)
    if match is None:
        return np.nan
    value = float(cleaned)
    try:
        value *= 10 ** int(scale) if scale else 1
    except ValueError:
        return np.nan
    return -value if sign == "-" else value


def parse_ixbrl(html: str) -> pd.DataFrame:
    """inline XBRL の文字列から、項目を全部取り出して表にする（1行1項目）。"""
    parser = _FactParser()
    parser.feed(html)
    parser.close()
    rows = []
    for f in parser.facts:
        text = re.sub(r"\s+", " ", "".join(f.pop("parts"))).strip()
        value = to_number(text, f["scale"], f["sign"], f["format"], f["nil"]) if f["kind"] == "number" else np.nan
        rows.append({**f, "text": text, "value": value})
    return pd.DataFrame(rows, columns=FACT_COLUMNS)


def document_type(filename: str) -> str:
    """ファイル名から書類の種類を見分ける。
    tse-xxxx-...-ixbrl.htm の xxxx 部分（例: scedjpsy = 決算短信サマリー、rvfc = 業績予想の修正）。"""
    base = filename.rsplit("/", 1)[-1]
    match = re.match(r"(?:\d+-\w+-)?tse-([a-z]+)-", base)
    return match.group(1) if match else ""


def read_xbrl_zip(content: bytes) -> pd.DataFrame:
    """TDnet の XBRL zip から、inline XBRL の項目を全部取り出す（展開はしない）。
    列: file（zip の中のファイル名）、doc_type（書類の種類）と、parse_ixbrl の列。"""
    frames = []
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        for info in z.infolist():
            if not info.filename.lower().endswith("-ixbrl.htm") or info.file_size > MAX_ENTRY_BYTES:
                continue
            facts = parse_ixbrl(z.read(info).decode("utf-8", errors="replace"))
            facts.insert(0, "file", info.filename)
            facts.insert(1, "doc_type", document_type(info.filename))
            frames.append(facts)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=["file", "doc_type"] + FACT_COLUMNS)
