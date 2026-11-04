"""日足株価を取ってきて、手元に保存する。

流れ:
  1. 手元に保存済みの株価（data/raw/prices/銘柄コード.csv）を読む
  2. 足りない期間を yfinance で取る。取れなかった銘柄は Stooq で取り直す
  3. 手元のデータとつなげて保存する
  4. 古すぎる・短すぎるデータがないか点検し、問題は品質ログに書く

株価の列: open（始値） high（高値） low（安値） close（終値） adj_close（配当込み調整後終値）
         volume（出来高） source（どこから取ったか）
close は株式分割を反映済みの値。テクニカル指標は close で計算する。
"""

import io
import time
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from kabu.config import resolve_path
from kabu.quality import QualityLog

SOURCE = "prices"
PRICE_COLUMNS = ["open", "high", "low", "close", "adj_close", "volume"]

YF_RENAME = {
    "Open": "open", "High": "high", "Low": "low", "Close": "close",
    "Adj Close": "adj_close", "Volume": "volume",
}


# ---------- 取得結果を共通の形にそろえる ----------

def _finish(df: pd.DataFrame, source: str) -> pd.DataFrame | None:
    """列をそろえ、日付を索引にし、終値のない行を捨てる。何も残らなければ None。"""
    if "adj_close" not in df.columns:
        df = df.assign(adj_close=np.nan)
    df = df[PRICE_COLUMNS].apply(pd.to_numeric, errors="coerce").dropna(subset=["close"])
    if df.empty:
        return None
    index = pd.to_datetime(df.index)
    if index.tz is not None:
        index = index.tz_localize(None)
    df.index = index.normalize()
    df.index.name = "date"
    df["source"] = source
    return df.sort_index()


def extract_yfinance(frame: pd.DataFrame, ticker: str) -> pd.DataFrame | None:
    """yf.download の結果（複数銘柄がまとまった表）から1銘柄分を取り出す。"""
    if frame is None or frame.empty:
        return None
    if isinstance(frame.columns, pd.MultiIndex):
        if ticker in frame.columns.get_level_values(0):
            sub = frame[ticker]
        elif ticker in frame.columns.get_level_values(1):
            sub = frame.xs(ticker, axis=1, level=1)
        else:
            return None
    else:
        sub = frame
    return _finish(sub.rename(columns=YF_RENAME), "yfinance")


def parse_stooq_csv(text: str) -> pd.DataFrame | None:
    """Stooq の CSV を読む。データがないと「No data」などの文字が返ってくるので None にする。"""
    if not text.startswith("Date,"):
        return None
    df = pd.read_csv(io.StringIO(text), index_col="Date")
    df = df.rename(columns={c: c.lower() for c in df.columns})
    return _finish(df, "stooq")


# ---------- 取ってくる ----------

def fetch_yfinance(codes: list[str], start: str, chunk_size: int, pause: float) -> dict[str, pd.DataFrame]:
    """yfinance でまとめて取る。取れた銘柄だけを返す（取れなかった銘柄は辞書に入らない）。"""
    results = {}
    for i in range(0, len(codes), chunk_size):
        chunk = codes[i:i + chunk_size]
        tickers = [f"{c}.T" for c in chunk]
        try:
            frame = yf.download(
                tickers, start=start, auto_adjust=False, group_by="ticker",
                threads=False, progress=False,
            )
        except Exception:  # yfinance は通信・仕様変更で色々な例外を出すので、まとめて「取れなかった」扱い
            frame = None
        for code, ticker in zip(chunk, tickers):
            df = extract_yfinance(frame, ticker)
            if df is not None:
                results[code] = df
        if i + chunk_size < len(codes):
            time.sleep(pause)
    return results


def fetch_stooq(code: str, start: str, url: str) -> tuple[pd.DataFrame | None, str]:
    """Stooq で1銘柄取る。戻り値は (株価, 失敗理由)。"""
    params = {"s": f"{code.lower()}.jp", "i": "d", "d1": start.replace("-", "")}
    try:
        response = requests.get(url, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        return None, f"通信エラー: {e}"
    df = parse_stooq_csv(response.text)
    if df is None:
        return None, f"データなし（応答: {response.text[:40]!r}）"
    return df, ""


# ---------- 手元の保存データ ----------

def cache_path(settings: dict, code: str) -> Path:
    return resolve_path(settings["paths"]["raw_prices"]) / f"{code}.csv"


def load_cache(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    return pd.read_csv(path, index_col="date", parse_dates=["date"])


def save_cache(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, date_format="%Y-%m-%d")


def merge_with_cache(
    cached: pd.DataFrame | None, fresh: pd.DataFrame, tolerance: float = 0.01
) -> tuple[pd.DataFrame, bool]:
    """手元のデータと新しく取ったデータをつなぐ。

    重なっている日の終値が tolerance（1%）以上ずれていたら False を返す。
    株式分割などで過去の株価が修正されたときに起きるので、その場合は全期間を取り直す必要がある。
    """
    if cached is None or cached.empty:
        return fresh, True
    overlap = cached.index.intersection(fresh.index)
    if len(overlap) > 0:
        old = cached.loc[overlap, "close"]
        new = fresh.loc[overlap, "close"]
        if ((old - new).abs() / new).max() > tolerance:
            return fresh, False
    merged = pd.concat([cached[~cached.index.isin(fresh.index)], fresh]).sort_index()
    return merged, True


def drop_unfinished_today(df: pd.DataFrame, now: datetime, close_time: str) -> tuple[pd.DataFrame, bool]:
    """取引時間中に実行した場合、今日の行はまだ確定していないので捨てる。"""
    hour, minute = map(int, close_time.split(":"))
    today = pd.Timestamp(now.date())
    before_close = (now.hour, now.minute) < (hour, minute)
    if before_close and len(df) > 0 and df.index[-1] == today:
        return df.iloc[:-1], True
    return df, False


# ---------- 点検 ----------

def check_prices(prices: dict[str, pd.DataFrame], conf: dict, now: datetime, quality: QualityLog) -> pd.Timestamp | None:
    """古さ・長さ・出来高を点検する。全銘柄の中で一番新しい日付（基準日）を返す。"""
    if not prices:
        return None
    latest = {code: df.index[-1] for code, df in prices.items()}
    reference = max(latest.values())

    stale_limit = pd.Timestamp(now.date()) - timedelta(days=conf["max_stale_days"])
    if reference < stale_limit:
        quality.warn(SOURCE, "", f"全銘柄の最新データが {reference.date()} 止まりです。"
                                 f"データ源の遅延か、長い休場の可能性があります")

    for code, df in prices.items():
        if latest[code] < reference:
            quality.warn(SOURCE, code, f"最新データが {latest[code].date()} で、"
                                       f"基準日 {reference.date()} より古い（売買停止・取得遅延の可能性）")
        if len(df) < conf["min_history_rows"]:
            quality.warn(SOURCE, code, f"履歴が {len(df)} 日分しかありません（上場間もない・取得不足）")
        if df["volume"].iloc[-1] == 0:
            quality.info(SOURCE, code, f"{latest[code].date()} の出来高が0です（売買不成立の可能性）")
    return reference


# ---------- 全体の流れ ----------

def load_cached_prices(codes: list[str], settings: dict) -> dict[str, pd.DataFrame]:
    """手元に保存済みの株価だけを読む（ネットにはつながない）。保存がない銘柄は辞書に入らない。"""
    prices = {}
    for code in codes:
        df = load_cache(cache_path(settings, code))
        if df is not None and not df.empty:
            prices[code] = df
    return prices


def update_prices(
    codes: list[str], settings: dict, quality: QualityLog, now: datetime, full_days: int | None = None
) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """全銘柄の株価を最新にする。戻り値は (銘柄コード→株価の表, 取得状況の一覧)。

    full_days を指定すると、手元のデータの有無に関係なく、その日数分を全銘柄取り直す
    （後付けの再現のために履歴を長くしたいとき用）。
    """
    conf = settings["prices"]
    stooq_url = settings["sources"]["stooq_url"]
    today = pd.Timestamp(now.date())
    history_days = full_days or conf["history_days"]
    full_start = (today - timedelta(days=history_days)).strftime("%Y-%m-%d")

    caches = {code: load_cache(cache_path(settings, code)) for code in codes}

    # 手元にデータがある銘柄は「最後の日 − 重ね日数」から、ない銘柄は全期間を取る
    new_codes = [c for c in codes if full_days or caches[c] is None or caches[c].empty]
    update_codes = [c for c in codes if c not in new_codes]
    fetched: dict[str, pd.DataFrame] = {}
    if new_codes:
        fetched |= fetch_yfinance(new_codes, full_start, conf["chunk_size"], conf["pause_seconds"])
    if update_codes:
        last = min(caches[c].index[-1] for c in update_codes)
        start = (last - timedelta(days=conf["overlap_days"])).strftime("%Y-%m-%d")
        fetched |= fetch_yfinance(update_codes, start, conf["chunk_size"], conf["pause_seconds"])

    prices: dict[str, pd.DataFrame] = {}
    sources: dict[str, str] = {}
    for code in codes:
        cached = caches[code]
        fresh = fetched.get(code)
        source = "yfinance"

        if fresh is None:
            start = full_start if (full_days or cached is None) else (cached.index[-1] - timedelta(days=conf["overlap_days"])).strftime("%Y-%m-%d")
            fresh, reason = fetch_stooq(code, start, stooq_url)
            source = "stooq"
            if fresh is not None:
                quality.warn(SOURCE, code, "yfinance で取れず、予備の Stooq で取得しました")
            else:
                if cached is None:
                    quality.error(SOURCE, code, f"yfinance・Stooq とも取得失敗、手元にもデータなし（Stooq: {reason}）")
                    continue
                quality.error(SOURCE, code, f"yfinance・Stooq とも取得失敗。手元の {cached.index[-1].date()} までのデータで続行します（Stooq: {reason}）")
                prices[code] = cached
                sources[code] = "手元のみ"
                continue

        merged, consistent = merge_with_cache(cached, fresh)
        if not consistent:
            # 過去の株価が修正されていた → 全期間を同じデータ源で取り直す
            if source == "yfinance":
                refetched = fetch_yfinance([code], full_start, 1, 0).get(code)
            else:
                refetched, _ = fetch_stooq(code, full_start, stooq_url)
            if refetched is not None:
                merged = refetched
                quality.warn(SOURCE, code, "過去の株価が修正されていたため全期間を取り直しました（株式分割などの可能性）")
            else:
                merged = fresh
                quality.warn(SOURCE, code, "過去の株価が修正されていましたが取り直しに失敗。直近分のみ使います")

        merged, dropped = drop_unfinished_today(merged, now, conf["market_close_jst"])
        if dropped:
            quality.info(SOURCE, code, f"{conf['market_close_jst']} 前の実行のため、当日分は未確定として除外しました")

        save_cache(cache_path(settings, code), merged)
        prices[code] = merged
        sources[code] = source

    check_prices(prices, conf, now, quality)

    status = pd.DataFrame(
        [
            {
                "code": code,
                "latest_date": prices[code].index[-1].date() if code in prices else None,
                "close": prices[code]["close"].iloc[-1] if code in prices else None,
                "rows": len(prices[code]) if code in prices else 0,
                "source": sources.get(code, "取得失敗"),
            }
            for code in codes
        ]
    )
    return prices, status
