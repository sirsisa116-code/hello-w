"""J-Quants（V2）に APIキーでつながり、どんな決算データが取れるかを調べる確認用スクリプト。

使い方（プロジェクトのフォルダで）:
    python scripts/probe_jquants.py

初回は APIキーを聞かれる（J-Quants のダッシュボードで発行したもの。入力は画面に出さず、
config/local/jquants_api_key.txt に保存する）。そのあと、トヨタ（7203）と TVE（6466）の
決算データ（/v2/fins/summary）を取り、
  - 応答の形（どんなキーで返ってくるか）
  - 何年分さかのぼれるか / 一番新しいデータは何日前か（無料プランの遅れ）
  - 書類の種類ごとの件数
  - 1件分の中身（項目名と値）
を表示する。無料プランは1分5回までなので、問い合わせの間に13秒あける。APIキーは画面に出さない。
"""

import sys
import time
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.data.jquants import DEFAULT_PAUSE_SECONDS, JQuantsError, forget_api_key, get_all, get_api_key  # noqa: E402

CODES = ["72030", "64660"]   # J-Quants の銘柄コードは5桁（4桁 + 0）


def guess_column(df: pd.DataFrame, words: tuple[str, ...]) -> str | None:
    """項目名が分からないので、それらしい名前の列を探す（例: 開示日なら Date を含むもの）。"""
    for col in df.columns:
        if any(w.lower() in col.lower() for w in words):
            return col
    return None


def main() -> int:
    try:
        api_key = get_api_key()
    except JQuantsError as e:
        print(f"失敗: {e}")
        return 1

    for i, code in enumerate(CODES):
        if i > 0:
            print(f"\n（無料プランの回数制限のため {DEFAULT_PAUSE_SECONDS:.0f} 秒待ちます）")
            time.sleep(DEFAULT_PAUSE_SECONDS)
        print("=" * 70)
        try:
            rows, first = get_all("/fins/summary", {"code": code}, api_key)
        except JQuantsError as e:
            print(f"[{code}] 失敗: {e}")
            if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                forget_api_key()
                print("  保存したAPIキーを消しました。キーを確かめて、もう一度実行してください"
                      "（次回また聞かれます）")
            return 1
        print(f"[{code}] 応答のキー: {list(first.keys())}")
        if not rows:
            print(f"[{code}] データが0件でした")
            continue
        df = pd.DataFrame(rows)
        date_col = guess_column(df, ("DiscDate", "DisclosedDate", "Date"))
        if date_col:
            dates = pd.to_datetime(df[date_col], errors="coerce")
            print(f"[{code}] {len(df)} 件 / {date_col}: {dates.min().date()} 〜 {dates.max().date()}"
                  f"（一番新しいものは {(pd.Timestamp(date.today()) - dates.max()).days} 日前）")
        else:
            print(f"[{code}] {len(df)} 件（日付の列が見つかりません）")
        type_col = guess_column(df, ("DocType", "TypeOfDocument"))
        if type_col:
            print(f"  書類の種類（{type_col}）ごとの件数:")
            for kind, n in Counter(df[type_col]).most_common():
                print(f"    {n:>3} 件  {kind}")
        revision = df[df[type_col].astype(str).str.contains("Revision|Forecast")] if type_col else df.iloc[0:0]
        sample = (revision if not revision.empty else df).iloc[-1]
        print(f"  1件分の中身（{'業績予想の修正' if not revision.empty else '最新の書類'}。値が空の項目は省略）:")
        for key, value in sample.items():
            if value not in ("", None):
                print(f"    {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
