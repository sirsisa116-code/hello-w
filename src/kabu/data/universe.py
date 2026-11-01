"""対象銘柄の一覧を作る。

- watchlist モード: settings.yaml に書いた銘柄をそのまま使う
- prime モード:     JPX の「東証上場銘柄一覧」からプライム市場の銘柄を選ぶ

JPX の一覧は月1回更新の Excel。毎回ダウンロードせず手元に保存しておき、
ダウンロードに失敗したら手元の古いファイルを使う（そのことは品質ログに残す）。
ファイルの URL は JPX 側で変わることがあるので、案内ページからリンクを探して取る。
"""

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
import requests

from kabu.config import JPX_MARKET_PRIME, now_jst, resolve_path
from kabu.quality import QualityLog

SOURCE = "universe"

# JPX の Excel の列名 → このツールで使う列名
JPX_COLUMNS = {
    "コード": "code",
    "銘柄名": "name",
    "市場・商品区分": "market",
    "33業種区分": "sector33",
    "規模区分": "scale",
}

# 手元の一覧がこれより古ければ取り直す（JPX は月次更新なので週1で十分）
JPX_REFRESH_DAYS = 7


def normalize_code(value) -> str:
    """銘柄コードを文字列にそろえる。Excel だと 7203 が 7203.0 になることがあるため。
    2024年以降の英字入りコード（例: 130A）もそのまま扱える。"""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    return text.upper()


def parse_jpx_list(raw: pd.DataFrame) -> pd.DataFrame:
    """JPX の Excel を読んだ表から、必要な列だけを取り出して整える。"""
    missing = [c for c in JPX_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"JPX一覧の形式が想定と違います。見つからない列: {missing}")
    df = raw[list(JPX_COLUMNS)].rename(columns=JPX_COLUMNS)
    df["code"] = df["code"].map(normalize_code)
    return df.reset_index(drop=True)


def load_jpx_list(settings: dict, quality: QualityLog) -> pd.DataFrame | None:
    """JPX の一覧を返す。取れなければ None（そのことは品質ログに記録済み）。"""
    path = resolve_path(settings["paths"]["jpx_list"])
    age_days = None
    if path.exists():
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=now_jst().tzinfo)
        age_days = (now_jst() - modified).days

    if age_days is None or age_days >= JPX_REFRESH_DAYS:
        try:
            _download_jpx_list(settings["sources"]["jpx_list_page"], path)
        except (requests.RequestException, JpxLinkNotFound) as e:
            if age_days is None:
                quality.error(SOURCE, "", f"JPX銘柄一覧をダウンロードできず、手元にもありません: {e}")
                return None
            quality.warn(SOURCE, "", f"JPX銘柄一覧をダウンロードできず、{age_days}日前の手元のファイルを使います: {e}")

    try:
        return parse_jpx_list(pd.read_excel(path))
    except Exception as e:  # 壊れたファイル・形式変更などは何が起きるか読めないので広く拾う
        quality.error(SOURCE, "", f"JPX銘柄一覧を読み込めません: {e}")
        return None


class JpxLinkNotFound(Exception):
    """案内ページに銘柄一覧ファイルへのリンクが見つからなかった。"""


def find_jpx_list_link(html: str, page_url: str) -> str:
    """案内ページの HTML から data_j.xls（または .xlsx）へのリンクを探し、完全な URL にして返す。"""
    match = re.search(r'href="([^"]*data_j\.xlsx?)"', html)
    if match is None:
        raise JpxLinkNotFound(f"{page_url} に銘柄一覧ファイルへのリンクが見つかりません（ページの作りが変わった可能性）")
    return urljoin(page_url, match.group(1))


def _download_jpx_list(page_url: str, path: Path) -> None:
    page = requests.get(page_url, timeout=30)
    page.raise_for_status()
    page.encoding = page.apparent_encoding
    file_url = find_jpx_list_link(page.text, page_url)

    response = requests.get(file_url, timeout=30)
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        raise requests.HTTPError(f"案内ページで見つけたリンクが開けません（{file_url}）: {e}") from e
    path.parent.mkdir(parents=True, exist_ok=True)
    # 拡張子が .xls でも .xlsx でも、pandas は中身を見て読み分けるので保存名は固定でよい（.xlsx の読み込みには openpyxl が必要）
    path.write_bytes(response.content)


def select_prime(jpx: pd.DataFrame, scales: list[str]) -> pd.DataFrame:
    df = jpx[jpx["market"] == JPX_MARKET_PRIME]
    if scales:
        df = df[df["scale"].isin(scales)]
    return df.reset_index(drop=True)


def build_universe(settings: dict, quality: QualityLog) -> pd.DataFrame:
    """対象銘柄の表（列: code, name, market, sector33, scale）を返す。"""
    conf = settings["universe"]
    mode = conf["mode"]

    if mode == "watchlist":
        df = pd.DataFrame(
            [(normalize_code(c), n) for c, n in conf["watchlist"].items()],
            columns=["code", "name"],
        )
        # 業種などの情報は JPX 一覧から補う。取れなくても watchlist だけで動く。
        jpx = load_jpx_list(settings, quality)
        if jpx is not None:
            df = df.merge(jpx.drop(columns="name"), on="code", how="left")
            for code in df.loc[df["market"].isna(), "code"]:
                quality.warn(SOURCE, code, "JPX銘柄一覧に見つかりません（上場廃止・コード誤りの可能性）")
        else:
            for col in ("market", "sector33", "scale"):
                df[col] = pd.NA
        return df

    if mode == "prime":
        jpx = load_jpx_list(settings, quality)
        if jpx is None:
            raise RuntimeError("prime モードですが、JPX銘柄一覧が取れないため銘柄を決められません")
        df = select_prime(jpx, conf.get("prime_scales") or [])
        if df.empty:
            quality.error(SOURCE, "", "条件に合う銘柄が0件です。prime_scales の書き方を確認してください")
        return df

    raise ValueError(f"universe.mode の値が不明です: {mode}（watchlist か prime）")
