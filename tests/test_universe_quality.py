"""銘柄一覧と品質ログのテスト（ネットにはつながない）。"""

import pandas as pd

from kabu.data.universe import normalize_code, parse_jpx_list, select_prime
from kabu.quality import QualityLog


def test_normalize_code():
    assert normalize_code(7203) == "7203"
    assert normalize_code(7203.0) == "7203"
    assert normalize_code("130a") == "130A"


def test_parse_and_select_prime():
    raw = pd.DataFrame({
        "日付": [20261031] * 3,
        "コード": [7203, "130A", 1306],
        "銘柄名": ["トヨタ自動車", "テスト", "TOPIX連動"],
        "市場・商品区分": ["プライム（内国株式）", "グロース（内国株式）", "ETF・ETN"],
        "33業種コード": [3700, 5250, "-"],
        "33業種区分": ["輸送用機器", "情報・通信業", "-"],
        "17業種コード": [6, 10, "-"],
        "17業種区分": ["自動車・輸送機", "情報通信・サービスその他", "-"],
        "規模コード": [1, "-", "-"],
        "規模区分": ["TOPIX Core30", "-", "-"],
    })
    jpx = parse_jpx_list(raw)
    assert list(jpx["code"]) == ["7203", "130A", "1306"]
    assert list(select_prime(jpx, [])["code"]) == ["7203"]
    assert select_prime(jpx, ["TOPIX Mid400"]).empty


def test_quality_log_save_and_summary(tmp_path):
    log = QualityLog()
    log.info("prices", "7203", "参考")
    log.error("prices", "6758", "取得失敗")
    lines = log.summary_lines()
    assert lines[0].startswith("ERROR 1件")
    assert "ERROR" in lines[1]  # ERROR が先頭に来る
    path = tmp_path / "q.csv"
    log.save(path)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")  # Excel 用の BOM 付き
