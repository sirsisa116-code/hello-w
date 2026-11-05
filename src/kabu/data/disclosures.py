"""適時開示（TDnet）の一覧を取ってきて、手元に保存する。

取り先は非公式の TDnet WebAPI（やのしん）。個人が無料で運営しているサービスなので、
  - 1日分を1回の問い合わせで取る（上限10,000件。決算が集中する日でも2,300件ほど）
  - 問い合わせの間は1秒以上あける
  - 一度取った過去の日は手元に保存し、二度と問い合わせない
という使い方にしている。

開示は夜まで出続けるので、「今日」と「昨日」の分は保存してあっても取り直す
（前回取ったあとに増えている可能性があるため）。それより前の日は内容が変わらない。
"""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

from kabu.config import resolve_path
from kabu.quality import QualityLog

SOURCE = "disclosures"
HEADERS = {"User-Agent": "kabu-ranking-tool/0.1 (personal research)"}
LIMIT = 10000
COLUMNS = ["id", "pubdate", "code", "company_name", "title", "has_xbrl", "url_xbrl", "document_url"]


def normalize_tdnet_code(value) -> str:
    """TDnet の5桁コード（例: 72030、285A0）を、JPX と同じ4桁（7203、285A）にそろえる。"""
    text = str(value).strip().upper()
    return text[:4] if len(text) == 5 else text


def parse_items(data: dict) -> pd.DataFrame:
    """WebAPI の応答（JSON）を表にする。1件分は {"Tdnet": {...}} の形で入っている。"""
    rows = []
    for item in data.get("items") or []:
        d = item.get("Tdnet", item) if isinstance(item, dict) else {}
        rows.append({
            "id": str(d.get("id", "")),
            "pubdate": d.get("pubdate"),
            "code": normalize_tdnet_code(d.get("company_code", "")),
            "company_name": d.get("company_name", ""),
            "title": d.get("title", "") or "",
            "has_xbrl": bool(d.get("url_xbrl")),
            "url_xbrl": d.get("url_xbrl"),
            "document_url": d.get("document_url"),
        })
    df = pd.DataFrame(rows, columns=COLUMNS)
    df["pubdate"] = pd.to_datetime(df["pubdate"], errors="coerce")
    return df


def day_path(settings: dict, day: date) -> Path:
    return resolve_path(settings["paths"]["raw_tdnet"]) / f"{day:%Y-%m-%d}.json"


def fetch_day(url: str, day: date) -> tuple[dict | None, str]:
    """1日分を問い合わせる。戻り値は (応答の JSON, 失敗したときの説明)。"""
    try:
        response = requests.get(f"{url}/{day:%Y%m%d}.json", params={"limit": LIMIT}, headers=HEADERS, timeout=60)
        response.raise_for_status()
        data = response.json()
    except (requests.RequestException, ValueError) as e:
        return None, str(e)
    if not isinstance(data, dict) or "items" not in data:
        return None, f"想定外の応答: {str(data)[:120]}"
    return data, ""


def update_disclosures(
    start: date, end: date, settings: dict, quality: QualityLog, today: date, progress=print
) -> tuple[pd.DataFrame, list[date]]:
    """start〜end の開示を手元にそろえて、まとめて返す。

    戻り値: (開示の表, 取れなかった日の一覧)
    取れなかった日は品質ログに ERROR で残す。その日を含む期間の点数は「欠損」として扱うこと。
    """
    conf = settings["disclosures"]
    url = settings["sources"]["tdnet_url"]
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    to_fetch = [d for d in days if not day_path(settings, d).exists() or d >= today - timedelta(days=1)]
    if len(to_fetch) > 5:
        progress(f"適時開示を {len(to_fetch)} 日分取得します（1日1回、{conf['pause_seconds']}秒間隔）…")

    failed = []
    for i, day in enumerate(to_fetch, 1):
        if i > 1:
            time.sleep(conf["pause_seconds"])
        data, error = fetch_day(url, day)
        if data is None:
            if day_path(settings, day).exists():
                quality.warn(SOURCE, "", f"{day} の適時開示を取り直せなかったので、前回取った分を使います"
                                         f"（その後に出た開示が抜けている可能性あり）: {error}")
            else:
                failed.append(day)
                quality.error(SOURCE, "", f"{day} の適時開示を取得できませんでした: {error}")
            continue
        if len(data.get("items") or []) >= LIMIT:
            # 上限ちょうどまで返ってきたら、途中で切れている可能性がある
            failed.append(day)
            quality.error(SOURCE, "", f"{day} の適時開示が上限 {LIMIT} 件に達しました。取りこぼしの可能性があるため使いません")
            continue
        path = day_path(settings, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        data["_fetched_at"] = datetime.now().isoformat(timespec="seconds")
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        if len(to_fetch) > 5 and (i % 30 == 0 or i == len(to_fetch)):
            progress(f"  {i} / {len(to_fetch)} 日 取得済み（{day}）")

    frames = []
    for day in days:
        path = day_path(settings, day)
        if day in failed or not path.exists():
            if day not in failed:
                failed.append(day)
            continue
        frames.append(parse_items(json.loads(path.read_text(encoding="utf-8"))))
    disclosures = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=COLUMNS)
    disclosures = disclosures.drop_duplicates("id").sort_values("pubdate").reset_index(drop=True)
    return disclosures, sorted(failed)


def prepare_disclosure_events(
    start: date, end: date, settings: dict, conf: dict, quality: QualityLog, today: date, progress=print
) -> tuple[dict[str, pd.DataFrame], list[date], pd.DataFrame]:
    """開示を取ってきて分類し、銘柄ごとに分ける（ランキングで使う形にする）。
    戻り値: (銘柄コード → 分類済みの開示, 取れなかった日, 分類済みの開示の表)"""
    from kabu.analysis.disclosure import classify_disclosures, group_by_code

    disclosures, failed = update_disclosures(start, end, settings, quality, today, progress)
    disclosures = disclosures[disclosures["pubdate"].notna()]
    classified = classify_disclosures(disclosures, conf)
    return group_by_code(classified), failed, classified
