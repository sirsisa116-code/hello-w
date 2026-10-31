"""データの欠損・遅延・取得失敗を記録する係。

方針は「黙って進めない」。何か問題があれば必ずここに書き込み、
最後に品質ログ（CSV）と画面表示で知らせる。

記録の重さは3段階:
  ERROR: データが取れず、その銘柄・項目は使えない
  WARN:  使えるが、古い・足りない・予備のデータ源で取った など注意が必要
  INFO:  参考情報（当日分を未確定として捨てた など）
"""

from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

LEVELS = ("ERROR", "WARN", "INFO")


@dataclass
class Issue:
    level: str
    source: str   # どの処理で起きたか（例: prices, universe）
    code: str     # 銘柄コード。全体に関わる問題なら空文字
    message: str


class QualityLog:
    def __init__(self):
        self.issues: list[Issue] = []

    def add(self, level: str, source: str, code: str, message: str) -> None:
        if level not in LEVELS:
            raise ValueError(f"不明なレベル: {level}")
        self.issues.append(Issue(level, source, code, message))

    def error(self, source: str, code: str, message: str) -> None:
        self.add("ERROR", source, code, message)

    def warn(self, source: str, code: str, message: str) -> None:
        self.add("WARN", source, code, message)

    def info(self, source: str, code: str, message: str) -> None:
        self.add("INFO", source, code, message)

    def count(self, level: str) -> int:
        return sum(1 for i in self.issues if i.level == level)

    def to_frame(self) -> pd.DataFrame:
        columns = ["level", "source", "code", "message"]
        return pd.DataFrame([asdict(i) for i in self.issues], columns=columns)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # utf-8-sig（BOM付き）にすると、Windows の Excel で文字化けしない
        self.to_frame().to_csv(path, index=False, encoding="utf-8-sig")

    def summary_lines(self, max_lines: int = 30) -> list[str]:
        """画面に出す要約。ERROR → WARN → INFO の順に並べる。"""
        lines = [
            f"ERROR {self.count('ERROR')}件 / WARN {self.count('WARN')}件 / INFO {self.count('INFO')}件"
        ]
        ordered = sorted(self.issues, key=lambda i: LEVELS.index(i.level))
        for issue in ordered[:max_lines]:
            target = f"[{issue.code}] " if issue.code else ""
            lines.append(f"  {issue.level:5} {issue.source}: {target}{issue.message}")
        if len(ordered) > max_lines:
            lines.append(f"  ……ほか {len(ordered) - max_lines} 件（品質ログCSVを見てください）")
        return lines
