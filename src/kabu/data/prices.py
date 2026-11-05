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

def fetch_yfinance(
    codes: list[str], start: str, chunk_size: int, pause: float, retry_wait: float = 0
) -> dict[str, pd.DataFrame]:
    """yfinance でまとめて取る。取れた銘柄だけを返す（取れなかった銘柄は辞書に入らない）。
    retry_wait を指定すると、取れなかった銘柄をその秒数待ってから1回だけ取り直す
    （一度に大量に問い合わせると、一時的に断られることがあるため）。"""
    results = _fetch_yfinance_once(codes, start, chunk_size, pause)
    missing = [c for c in codes if c not in results]
    if missing and retry_wait > 0:
        print(f"  yfinance で {len(missing)} 銘柄取れなかったので、{retry_wait:.0f}秒待って取り直します…")
        time.sleep(retry_wait)
        results |= _fetch_yfinance_once(missing, start, chunk_size, pause)
    return results


def _fetch_yfinance_once(codes: list[str], start: str, chunk_size: int, pause: float) -> dict[str, pd.DataFrame]:
    results = {}
    for i in range(0, len(codes), chunk_size):
        if len(codes) > chunk_size:
            print(f"  yfinance: {i + 1}〜{min(i + chunk_size, len(codes))} / {len(codes)} 銘柄")
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

def find_suspicious_moves(df: pd.DataFrame, max_move: float) -> pd.DataFrame:
    """ありえないほど大きな値動きの日を探す。株式分割の反映漏れやデータの誤りを見つけるため。

    見るもの（max_move = 0.3 なら ±30% 超）:
      前日比:   前日の終値 → 当日の終値
      寄り付き: 前日の終値 → 当日の始値
      日中:     当日の始値 → 当日の終値
      0円以下:  値として成り立たない
    """
    prev_close = df["close"].shift(1)
    checks = {
        "前日比": df["close"] / prev_close - 1,
        "寄り付き": df["open"] / prev_close - 1,
        "日中": df["close"] / df["open"] - 1,
    }
    rows = []
    for kind, change in checks.items():
        for date in change.index[change.abs() > max_move]:
            rows.append({"date": date.date(), "kind": kind, "prev_close": prev_close.get(date),
                         "open": df.at[date, "open"], "close": df.at[date, "close"],
                         "change_pct": round(change[date] * 100, 1)})
    for date in df.index[(df[["open", "close"]] <= 0).any(axis=1)]:
        rows.append({"date": date.date(), "kind": "0円以下", "prev_close": prev_close.get(date),
                     "open": df.at[date, "open"], "close": df.at[date, "close"], "change_pct": np.nan})
    columns = ["date", "kind", "prev_close", "open", "close", "change_pct"]
    return pd.DataFrame(rows, columns=columns).sort_values(["date", "kind"]).reset_index(drop=True)


SCALE_ERRORS = (10.0, 100.0, 1000.0)
PRICE_FIELDS = ["open", "high", "low", "close", "adj_close"]


def _scale_jump(ratio: float, tolerance: float) -> float | None:
    """前日比がちょうど10倍・100倍・1000倍（またはその逆）付近なら、その倍率を返す。"""
    if not ratio > 0:
        return None
    for scale in SCALE_ERRORS:
        for target in (scale, 1 / scale):
            if abs(ratio / target - 1) <= tolerance:
                return target
    return None


def repair_scale_glitches(
    df: pd.DataFrame, max_days: int = 5, tolerance: float = 0.15
) -> tuple[pd.DataFrame, list[tuple]]:
    """データ源の「桁ずれ」を直す。

    直す条件（両方を満たすときだけ）:
      1. 終値が前日の約10倍（100倍・1000倍、またはその逆）に跳ねる
      2. max_days 営業日以内に、逆の倍率で元の水準に戻る
    実際の値動きでこうなることはまずない。戻らない場合は本当の株式分割の可能性があるので直さない。

    戻り値: (直した株価, 直した期間の一覧 [(開始日, 終了日, 掛けた倍率)])
    出来高は直さない（桁ずれしているかどうか判断できないため）。
    """
    close = df["close"].to_numpy()
    out = df.copy()
    repaired = []
    i = 1
    while i < len(close):
        jump = _scale_jump(close[i] / close[i - 1], tolerance)
        if jump is None:
            i += 1
            continue
        back_at = None
        for j in range(i + 1, min(i + max_days, len(close) - 1) + 1):
            back = _scale_jump(close[j] / close[j - 1], tolerance)
            if back is not None and abs(back * jump - 1) <= tolerance:
                back_at = j
                break
        if back_at is None:
            i += 1
            continue

        rows = out.index[i:back_at]
        reference = close[i - 1]
        for field in PRICE_FIELDS:
            values = out.loc[rows, field]
            # 値ごとに「元の水準」と「ずれた水準」のどちらに近いかを見て、ずれている値だけ直す
            # （始値だけ正しい、といった日があっても壊さないため）
            off = (np.log(values / reference) - np.log(jump)).abs() < np.log(values / reference).abs()
            out.loc[rows[off.to_numpy()], field] = values[off] / jump
        out.loc[rows, "source"] = out.loc[rows, "source"].astype(str) + f"(桁ずれ補正×{1 / jump:g})"
        repaired.append((rows[0].date(), rows[-1].date(), 1 / jump))
        i = back_at + 1
    return out, repaired


MAX_VALID_PRICE = 1e7       # 1株1,000万円超は日本株ではありえない値として捨てる
SPLIT_RATIOS = (2, 3, 4, 5, 10)


def drop_invalid_rows(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """値として成り立たない行（0円以下、1株1,000万円超）を捨てる。再上場銘柄の上場前の日などに入っていることがある。"""
    bad = (df["close"] <= 0) | (df["close"] > MAX_VALID_PRICE) | (df["open"] > MAX_VALID_PRICE)
    return df[~bad], [d.date() for d in df.index[bad]]


def repair_unadjusted_splits(
    df: pd.DataFrame, tolerance: float = 0.08, min_days_after: int = 3, window: int = 20
) -> tuple[pd.DataFrame, list[tuple]]:
    """株式分割が反映されていない株価を直す。

    直す条件（すべて満たすときだけ）:
      1. 終値と始値が、前日の終値のちょうど 1/n（2・3・4・5・10分割）付近に変わる（逆の併合なら n 倍）
      2. その後 min_days_after 日以上たっても、その水準のまま（桁ずれのようにすぐ戻らない）
      3. 出来高が、前後 window 日の中央値で比べて、おおよそ n 倍（併合なら 1/n）に変わっている
    株価と出来高がそろって同じ倍率で変わるのは分割の特徴で、本当の暴落ではまず起きない。
    そもそも東証には1日の値幅制限があり、1日で半値・倍になることは通常ありえない。
    直すのは分割「前」の行で、株価を 1/n 倍、出来高を n 倍にする（分割後の水準にそろえる）。

    戻り値: (直した株価, [(分割の日, 株価に掛けた倍率)])
    """
    out = df.copy()
    repaired = []
    close, open_, volume = df["close"].to_numpy(), df["open"].to_numpy(), df["volume"].to_numpy()
    for i in range(1, len(df) - min_days_after + 1):
        ratio = close[i] / close[i - 1]
        gap = open_[i] / close[i - 1]
        for n in SPLIT_RATIOS:
            for factor in (1 / n, float(n)):   # 1/n: 分割、n: 併合
                if abs(ratio / factor - 1) > tolerance or abs(gap / factor - 1) > tolerance:
                    continue
                # 戻らずにその水準のまま続いているか
                after = close[i:i + min_days_after]
                if np.any(np.abs(after / close[i] - 1) > 0.5):
                    continue
                before_vol = np.median(volume[max(0, i - window):i])
                after_vol = np.median(volume[i:i + window])
                # 出来高の変化を倍率で割り戻して、おおよそ1なら「同じ倍率で変わった」とみる。
                # 分割のあとは株価が安くなって買いやすくなり、出来高が比率以上に増えることがあるので上側を広めにとる
                low, high = (1 / 1.6, 2.5) if factor < 1 else (1 / 2.5, 1.4)
                if before_vol <= 0 or not (low <= (after_vol / before_vol) * factor <= high):
                    continue
                rows = out.index[:i]
                out.loc[rows, PRICE_FIELDS] = out.loc[rows, PRICE_FIELDS] * factor
                out.loc[rows, "volume"] = out.loc[rows, "volume"] / factor
                label = f"1:{n}分割" if factor < 1 else f"{n}:1併合"
                out.loc[rows, "source"] = out.loc[rows, "source"].astype(str) + f"({label}を調整)"
                repaired.append((df.index[i].date(), factor))
                close, volume = out["close"].to_numpy(), out["volume"].to_numpy()
                break
            else:
                continue
            break
    return out, repaired


def clean_prices(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """データ源の誤りを直す。戻り値は (直した株価, 何を直したかの説明)。
    順番: ありえない値の行を捨てる → 桁ずれ（すぐ戻るもの）を直す → 分割の反映漏れを直す"""
    notes = []
    df, dropped = drop_invalid_rows(df)
    if dropped:
        days = ", ".join(map(str, dropped[:5])) + (f" ほか{len(dropped) - 5}日" if len(dropped) > 5 else "")
        notes.append(f"ありえない値（0円以下・1株1,000万円超）の行を捨てました: {days}")
    df, glitches = repair_scale_glitches(df)
    for start, end, factor in glitches:
        period = f"{start}" if start == end else f"{start}〜{end}"
        notes.append(f"{period} の株価が桁ずれしていたため ×{factor:g} で補正しました")
    df, splits = repair_unadjusted_splits(df)
    for date, factor in splits:
        kind = f"1:{round(1 / factor)}の株式分割" if factor < 1 else f"{round(factor)}:1の株式併合"
        notes.append(f"{date} の{kind}が株価に反映されていなかったため、それより前の株価を ×{factor:.4g}・"
                     f"出来高を ×{1 / factor:.4g} で調整しました")
    return df, notes


def _repair_and_log(code: str, df: pd.DataFrame, quality: QualityLog | None) -> tuple[pd.DataFrame, bool]:
    fixed, notes = clean_prices(df)
    if quality is not None:
        for note in notes:
            quality.warn(SOURCE, code, f"{note}（データ源の誤り。株価データの source 列に記録）")
    return fixed, bool(notes)


def expected_latest_weekday(now: datetime, close_time: str) -> pd.Timestamp:
    """今の時刻なら、どの日の株価までそろっているはずか（祝日は考えない。平日だけを見る）。
    引け後（close_time 以降）の平日なら今日、それ以外は直前の平日。"""
    hour, minute = map(int, close_time.split(":"))
    day = pd.Timestamp(now.date())
    if not (day.weekday() < 5 and (now.hour, now.minute) >= (hour, minute)):
        day -= pd.Timedelta(days=1)
        while day.weekday() >= 5:
            day -= pd.Timedelta(days=1)
    return day


def check_prices(prices: dict[str, pd.DataFrame], conf: dict, now: datetime, quality: QualityLog) -> pd.Timestamp | None:
    """古さ・長さ・出来高・異常な値動きを点検する。全銘柄の中で一番新しい日付（基準日）を返す。"""
    if not prices:
        return None
    latest = {code: df.index[-1] for code, df in prices.items()}
    reference = max(latest.values())

    stale_limit = pd.Timestamp(now.date()) - timedelta(days=conf["max_stale_days"])
    if reference < stale_limit:
        quality.warn(SOURCE, "", f"全銘柄の最新データが {reference.date()} 止まりです。"
                                 f"データ源の遅延か、長い休場の可能性があります")
    else:
        expected = expected_latest_weekday(now, conf["market_close_jst"])
        if reference < expected:
            quality.warn(SOURCE, "", f"直近の平日 {expected.date()} の株価が、どの銘柄にもありません"
                                     f"（最新は {reference.date()}）。祝日・休場日でなければデータ源の遅れなので、"
                                     "後でもう一度実行してください（次回、直近の分を取り直します）")

    for code, df in prices.items():
        if latest[code] < reference:
            quality.warn(SOURCE, code, f"最新データが {latest[code].date()} で、"
                                       f"基準日 {reference.date()} より古い（売買停止・取得遅延の可能性）")
        if len(df) < conf["min_history_rows"]:
            quality.warn(SOURCE, code, f"履歴が {len(df)} 日分しかありません（上場間もない・取得不足）")
        if df["volume"].iloc[-1] == 0:
            quality.info(SOURCE, code, f"{latest[code].date()} の出来高が0です（売買不成立の可能性）")
        # 毎日の点検は直近分だけ（全期間は scripts/check_prices.py で見る）
        recent = df.iloc[-(conf["suspicious_check_rows"] + 1):]
        moves = find_suspicious_moves(recent, conf["max_daily_move"])
        if not moves.empty:
            dates = ", ".join(sorted({str(d) for d in moves["date"]}))
            quality.warn(SOURCE, code, f"異常に大きな値動きがあります（{dates}）。株式分割の反映漏れ・データ誤りの可能性。"
                                       "scripts/check_prices.py で詳細を確認してください")
    return reference


# ---------- 全体の流れ ----------

def load_cached_prices(codes: list[str], settings: dict, quality: QualityLog | None = None) -> dict[str, pd.DataFrame]:
    """手元に保存済みの株価だけを読む（ネットにはつながない）。保存がない銘柄は辞書に入らない。
    桁ずれは読み込んだ時点で直す（保存し直すのは run_daily.py の役目）。"""
    prices = {}
    for code in codes:
        df = load_cache(cache_path(settings, code))
        if df is not None and not df.empty:
            prices[code], _ = _repair_and_log(code, df, quality)
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
        fetched |= fetch_yfinance(new_codes, full_start, conf["chunk_size"], conf["pause_seconds"], conf["retry_wait_seconds"])
    if update_codes:
        last = min(caches[c].index[-1] for c in update_codes)
        start = (last - timedelta(days=conf["overlap_days"])).strftime("%Y-%m-%d")
        fetched |= fetch_yfinance(update_codes, start, conf["chunk_size"], conf["pause_seconds"], conf["retry_wait_seconds"])

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
                prices[code], _ = _repair_and_log(code, cached, quality)
                sources[code] = "手元のみ"
                continue

        # 新しく取ったデータも先に直しておく（手元の直し済みデータと突き合わせて、食い違いと誤判定しないため）
        fresh, _ = _repair_and_log(code, fresh, quality)
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
        merged, _ = _repair_and_log(code, merged, quality)

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
