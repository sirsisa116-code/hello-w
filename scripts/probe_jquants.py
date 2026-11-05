"""J-Quants にログインでき、どんなデータが取れるかを調べる確認用スクリプト。

使い方（プロジェクトのフォルダで）:
    python scripts/probe_jquants.py

初回はメールアドレスとパスワードを聞かれる（パスワードは保存しない。1週間有効なトークンだけを
config/local/ に保存する）。そのあと、トヨタ（7203）と TVE（6466）の決算データを取り、
  - 何年分さかのぼれるか / 一番新しいデータは何日前か（無料プランの遅れ）
  - 書類の種類（決算短信・業績予想の修正など）ごとの件数
  - 1件分の中身（項目名と値）
を表示する。ログイン情報やトークンは画面に出さない。
"""

import sys
from collections import Counter
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pandas as pd  # noqa: E402

from kabu.data.jquants import JQuantsError, get_all, get_id_token  # noqa: E402

CODES = ["72030", "64660"]   # J-Quants の銘柄コードは5桁（4桁 + 0）


def main() -> int:
    try:
        token = get_id_token()
    except JQuantsError as e:
        print(f"失敗: {e}")
        return 1
    print("ログインできました\n")

    for code in CODES:
        print("=" * 70)
        try:
            rows = get_all("/fins/statements", {"code": code}, token, "statements")
        except JQuantsError as e:
            print(f"[{code}] 失敗: {e}")
            continue
        if not rows:
            print(f"[{code}] データが0件でした")
            continue
        df = pd.DataFrame(rows)
        dates = pd.to_datetime(df["DisclosedDate"])
        print(f"[{code}] {len(df)} 件 / 開示日 {dates.min().date()} 〜 {dates.max().date()}"
              f"（一番新しいものは {(pd.Timestamp(date.today()) - dates.max()).days} 日前）")
        print("  書類の種類ごとの件数:")
        for kind, n in Counter(df.get("TypeOfDocument", pd.Series(dtype=str))).most_common():
            print(f"    {n:>3} 件  {kind}")
        revision = df[df.get("TypeOfDocument", pd.Series(dtype=str)).astype(str).str.contains("Revision")]
        sample = (revision if not revision.empty else df).iloc[-1]
        print(f"  1件分の中身（{'業績予想の修正' if not revision.empty else '最新の書類'}。値が空の項目は省略）:")
        for key, value in sample.items():
            if value not in ("", None):
                print(f"    {key}: {value}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
