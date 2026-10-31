"""設定ファイル（config/settings.yaml）を読み込む。"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

# このファイルの3つ上がプロジェクトのフォルダ（hello-w）
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 日本時間。日本は夏時間がないので +9時間固定で正しい。
JST = timezone(timedelta(hours=9))

# JPX 銘柄一覧での、プライム市場（国内株）の表記
JPX_MARKET_PRIME = "プライム（内国株式）"


def now_jst() -> datetime:
    return datetime.now(JST)


def load_settings(path: Path | None = None) -> dict:
    path = path or PROJECT_ROOT / "config" / "settings.yaml"
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_path(relative: str) -> Path:
    """設定ファイルに書いた相対パスを、プロジェクトのフォルダ基準の絶対パスにする。"""
    return PROJECT_ROOT / relative
