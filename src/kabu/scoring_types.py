"""点数の記録に使う型。scoring.py と analysis/ の両方から使うので、別のファイルに分けている。"""

from dataclasses import dataclass, field


@dataclass
class Contribution:
    """1つの指標（または1件の開示）が、点数にどれだけ効いたか。"""
    key: str
    label: str
    value: float
    value_text: str
    points: float
    note: str


@dataclass
class CategoryScore:
    """1つの分野（テクニカル・適時開示など）の点数と、その根拠。"""
    name: str
    score: float | None                 # None は「欠損」（データ不足で点数を付けられない）
    contributions: list[Contribution] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)   # 計算できなかった指標の表示名
