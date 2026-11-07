"""適時開示の XBRL を毎日取りに行き、主要な数字だけを保存する。

TDnet は資料を約1か月しか置いておかない（それより古い zip は 404 になる）。
取りそびれたデータは後から手に入らないので、毎日の実行で直近 retention_days 日分のうち
まだ取っていないものを取る。

保存するもの（data/xbrl/）:
  key_figures.csv  取り出した主要な数字（1行1項目）。開示の id・銘柄・時刻と一緒に保存
  fetch_log.csv    開示ごとの取得結果（ok / not_found / error）。ok と not_found は二度と取りに行かない
zip そのものは保存しない（年に数百MBになるため）。
"""

import time
from datetime import date, datetime, timedelta

import pandas as pd
import requests

from kabu.config import resolve_path
from kabu.data.disclosures import HEADERS, update_disclosures
from kabu.data.xbrl import read_xbrl_zip
from kabu.data.xbrl_figures import FIGURE_COLUMNS, extract_key_figures
from kabu.quality import QualityLog

SOURCE = "xbrl"
LOG_COLUMNS = ["id", "code", "pubdate", "title", "status", "message", "fetched_at", "version"]
# 取り出す項目を増やしたら上げる。古い版で取り出した開示は、TDnet に残っている間に読み直す
# 1: 売上・利益・1株利益・配当 / 2: 1株純資産・自己資本・株式数を追加（PBR のため）
EXTRACT_VERSION = 2
TARGET_WORDS = ("決算短信", "業績予想", "配当予想")


def direct_url(url: str) -> str:
    """やのしんの転送用 URL（rd.php?https://...）から、TDnet の元の URL を取り出す。"""
    return url.split("rd.php?", 1)[1] if "rd.php?" in url else url


def _paths(settings: dict):
    folder = resolve_path(settings["paths"]["xbrl"])
    return folder / "key_figures.csv", folder / "fetch_log.csv"


def load_key_figures(settings: dict) -> pd.DataFrame:
    figures_path, _ = _paths(settings)
    if not figures_path.exists():
        return pd.DataFrame(columns=["id", "code", "pubdate"] + FIGURE_COLUMNS)
    df = pd.read_csv(figures_path, dtype={"id": str, "code": str}, encoding="utf-8-sig", keep_default_na=False,
                     na_values=[""])
    df["pubdate"] = pd.to_datetime(df["pubdate"])
    for col in ("consolidated", "stage", "kind", "timing"):
        df[col] = df[col].fillna("")
    return df


def load_fetch_log(settings: dict) -> pd.DataFrame:
    _, log_path = _paths(settings)
    if not log_path.exists():
        return pd.DataFrame(columns=LOG_COLUMNS)
    return pd.read_csv(log_path, dtype={"id": str, "code": str}, encoding="utf-8-sig")


def _append(path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, mode="a", header=not path.exists(), index=False, encoding="utf-8-sig")


EMPTY_MESSAGE = "主要な数字が見つかりませんでした（項目名が想定外の可能性）"


def finished_ids(log: pd.DataFrame) -> set[str]:
    """もう取りに行かない開示。最後の取得結果が not_found のもの、または今の版（EXTRACT_VERSION）で
    数字を取り出せた ok のもの。
    数字を1つも取り出せなかった ok や、古い版で取り出した ok は、読み取り方を直したあとに読み直せるよう、
    TDnet に残っている間（retention_days 以内）は取り直す。"""
    if log.empty:
        return set()
    last = log.drop_duplicates("id", keep="last")
    message = last["message"].fillna("").astype(str)
    version = pd.to_numeric(last.get("version", pd.Series(1, index=last.index)), errors="coerce").fillna(1)
    current_ok = (last["status"] == "ok") & (message != EMPTY_MESSAGE) & (version >= EXTRACT_VERSION)
    return set(last.loc[(last["status"] == "not_found") | current_ok, "id"])


def _upgrade_log(log_path) -> None:
    """版の列がない古い取得記録に、版1として列を足す（列の並びがずれないよう、書き直す）。"""
    if log_path.exists():
        log = pd.read_csv(log_path, dtype={"id": str, "code": str}, encoding="utf-8-sig")
        if "version" not in log.columns:
            log["version"] = 1
            log[LOG_COLUMNS].to_csv(log_path, index=False, encoding="utf-8-sig")


def _replace_figures(path, doc_id: str, figures: pd.DataFrame) -> None:
    """開示1件分の数字を保存する。読み直した開示は、前に保存した分を消してから入れ替える。"""
    if path.exists():
        old = pd.read_csv(path, dtype={"id": str, "code": str}, encoding="utf-8-sig", keep_default_na=False,
                          na_values=[""])
        if (old["id"] == doc_id).any():
            pd.concat([old[old["id"] != doc_id], figures], ignore_index=True).to_csv(
                path, index=False, encoding="utf-8-sig")
            return
    _append(path, figures)


def fetch_zip(url: str) -> tuple[bytes | None, str, str]:
    """戻り値: (zip の中身, 状態 ok/not_found/error, 説明)"""
    try:
        response = requests.get(direct_url(url), headers=HEADERS, timeout=60)
    except requests.RequestException as e:
        return None, "error", str(e)
    if response.status_code == 404:
        return None, "not_found", "TDnet に資料がありません（保存期間切れの可能性）"
    if response.status_code != 200:
        return None, "error", f"HTTP {response.status_code}"
    return response.content, "ok", ""


def collect_xbrl(
    settings: dict, codes: set[str], quality: QualityLog, today: date, progress=print
) -> int:
    """直近の開示のうち、対象銘柄の決算短信・業績予想・配当予想の XBRL を取って保存する。
    戻り値は、今回新しく保存できた開示の件数。"""
    conf = settings["xbrl"]
    start = today - timedelta(days=conf["retention_days"])
    disclosures, _ = update_disclosures(start, today, settings, quality, today, progress)
    targets = disclosures[
        disclosures["has_xbrl"] & disclosures["code"].isin(codes)
        & disclosures["title"].map(lambda t: any(w in t for w in TARGET_WORDS))
        & ~disclosures["title"].str.contains("訂正")
    ]
    done = finished_ids(load_fetch_log(settings))
    targets = targets[~targets["id"].isin(done)]
    if targets.empty:
        return 0
    if len(targets) > 5:
        progress(f"XBRL（決算データ）を {len(targets)} 件取得します（{conf['pause_seconds']}秒間隔）…")

    figures_path, log_path = _paths(settings)
    _upgrade_log(log_path)
    saved, errors, not_found, empty_codes = 0, 0, 0, []
    for i, row in enumerate(targets.itertuples(index=False), 1):
        if i > 1:
            time.sleep(conf["pause_seconds"])
        content, status, message = fetch_zip(row.url_xbrl)
        if content is not None:
            try:
                figures = extract_key_figures(read_xbrl_zip(content))
            except Exception as e:  # 壊れた zip や想定外の書き方。何が起きるか読めないので広く拾って記録する
                status, message = "error", f"読み取れません: {e}"
            else:
                figures.insert(0, "id", row.id)
                figures.insert(1, "code", row.code)
                figures.insert(2, "pubdate", row.pubdate)
                _replace_figures(figures_path, row.id, figures)
                saved += 1
                if figures.empty:
                    message = EMPTY_MESSAGE
                    empty_codes.append(row.code)
        errors += status == "error"
        not_found += status == "not_found"
        _append(log_path, pd.DataFrame([{
            "id": row.id, "code": row.code, "pubdate": row.pubdate, "title": row.title, "status": status,
            "message": message, "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "version": EXTRACT_VERSION}], columns=LOG_COLUMNS))
        if len(targets) > 5 and (i % 50 == 0 or i == len(targets)):
            progress(f"  {i} / {len(targets)} 件 処理済み")

    if errors:
        quality.warn(SOURCE, "", f"XBRL の取得・読み取りに {errors} 件失敗しました（次回また取りに行きます。"
                                 "詳細は data/xbrl/fetch_log.csv）")
    if not_found:
        quality.warn(SOURCE, "", f"XBRL が TDnet に見つからなかった開示が {not_found} 件あります（保存期間切れの可能性）")
    if empty_codes:
        codes_text = ", ".join(sorted(set(empty_codes))[:15])
        quality.warn(SOURCE, "", f"XBRL から主要な数字を1つも取り出せなかった開示が {len(empty_codes)} 件あります"
                                 f"（銘柄: {codes_text}）。項目名が想定外の可能性があるので、"
                                 "scripts/check_xbrl.py で確認してください")
    return saved


def revision_details(settings: dict) -> dict[str, str]:
    """保存済みの数字から、開示 id → 業績予想の修正の中身（1行の文章）を作る。"""
    from kabu.data.xbrl_figures import revision_summary

    figures = load_key_figures(settings)
    figures = figures[figures["doc_type"] == "rvfc"]
    return {i: text for i, g in figures.groupby("id") if (text := revision_summary(g))}
