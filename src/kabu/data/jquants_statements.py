"""J-Quants（V2）の決算データ（/fins/summary）を、銘柄ごとに取って保存する。

無料プランは1分5回までなので、約500銘柄で1時間半以上かかる。途中で止めても続きから取れるよう、
1銘柄取るたびに data/raw/jquants/fins_summary/銘柄コード.json に保存し、保存済みの銘柄は取りに行かない。

項目名（実物で確認したもの）:
  DiscDate / DiscTime（開示日時）、Code（5桁）、DocType（書類の種類）、CurPerType（1Q/2Q/3Q/FY）、
  CurFYEn（決算期の終わりの日）、Sales / OP / OdP / NP（実績）、FSales / FOP / FOdP / FNP（会社予想）
値はすべて文字列で返ってくるので、数値の列は数値に直す。
"""

import json
import time
from datetime import datetime

import pandas as pd

from kabu.config import resolve_path
from kabu.data.jquants import JQuantsError, get_all
from kabu.quality import QualityLog

SOURCE = "jquants"
TEXT_COLUMNS = {"DiscDate", "DiscTime", "Code", "DiscNo", "DocType", "CurPerType", "CurPerSt", "CurPerEn",
                "CurFYSt", "CurFYEn", "NxFYSt", "NxFYEn", "SigChgInC", "ChgByASRev", "ChgNoASRev", "ChgAcEst",
                "RetroRst"}


def _folder(settings: dict):
    return resolve_path(settings["paths"]["raw_jquants"]) / "fins_summary"


def fetch_statements(codes: list[str], settings: dict, api_key: str, quality: QualityLog, progress=print,
                     sleep=time.sleep) -> int:
    """まだ保存していない銘柄の決算データを取る。戻り値は今回取った銘柄数。"""
    folder = _folder(settings)
    folder.mkdir(parents=True, exist_ok=True)
    todo = [c for c in codes if not (folder / f"{c}.json").exists()]
    if not todo:
        return 0
    pause = settings["jquants"]["pause_seconds"]
    minutes = len(todo) * pause / 60
    progress(f"J-Quants から {len(todo)} 銘柄の決算データを取ります（{pause:.0f}秒間隔、約{minutes:.0f}分。"
             "途中で止めても、もう一度実行すれば続きから取ります）")
    fetched = 0
    for i, code in enumerate(todo, 1):
        if i > 1:
            sleep(pause)
        try:
            rows, _ = get_all("/fins/summary", {"code": f"{code}0" if len(code) == 4 else code}, api_key,
                              key="data", pause=pause, sleep=sleep)
        except JQuantsError as e:
            quality.error(SOURCE, code, f"決算データを取れませんでした: {e}")
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                progress("  APIキーかプランの問題で取れないため、ここで止めます")
                break
            continue
        (folder / f"{code}.json").write_text(
            json.dumps({"fetched_at": datetime.now().isoformat(timespec="seconds"), "data": rows}, ensure_ascii=False),
            encoding="utf-8")
        fetched += 1
        if i % 20 == 0 or i == len(todo):
            progress(f"  {i} / {len(todo)} 銘柄 処理済み")
    return fetched


def load_statements(codes: list[str], settings: dict) -> pd.DataFrame:
    """保存済みの決算データを1つの表にする。銘柄コードは4桁にそろえ、開示日時（disclosed_at）の列を足す。"""
    folder = _folder(settings)
    frames = []
    for code in codes:
        path = folder / f"{code}.json"
        if path.exists():
            rows = json.loads(path.read_text(encoding="utf-8"))["data"]
            if rows:
                frames.append(pd.DataFrame(rows).assign(code=code))
    if not frames:
        return pd.DataFrame()
    df = pd.concat(frames, ignore_index=True)
    for col in df.columns:
        if col not in TEXT_COLUMNS and col != "code":
            df[col] = pd.to_numeric(df[col].replace("", None), errors="coerce")
    df["disclosed_at"] = pd.to_datetime(df["DiscDate"] + " " + df["DiscTime"].fillna("15:00:00").replace("", "15:00:00"),
                                        errors="coerce")
    return df.sort_values(["code", "disclosed_at", "DiscNo"]).reset_index(drop=True)
