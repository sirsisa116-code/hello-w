"""会社予想の変化の取り出しと、J-Quants の決算データの保存・読み込みのテスト（ネットにはつながない）。"""

import json

import numpy as np
import pandas as pd
import pytest

import kabu.data.jquants_statements as js
from kabu.analysis.guidance import guidance_events
from kabu.quality import QualityLog


def row(date, doc, per, fy_end, **values):
    base = {"DiscDate": date, "DiscTime": "15:00:00", "DiscNo": date.replace("-", ""), "DocType": doc,
            "CurPerType": per, "CurFYEn": fy_end, "code": "6466"}
    for col in ("OP", "OdP", "NP", "FOP", "FOdP", "FNP"):
        base[col] = values.get(col, np.nan)
    return base


def frame(rows):
    df = pd.DataFrame(rows)
    df["disclosed_at"] = pd.to_datetime(df["DiscDate"] + " " + df["DiscTime"])
    return df


def test_revision_compares_with_previous_forecast():
    # 業績予想の修正の書類には修正後の予想しかないので、前の書類（2Q）の予想と比べる
    df = frame([
        row("2024-05-10", "2QFinancialStatements_Consolidated_JP", "2Q", "2024-09-30", OP=500, FOP=1000),
        row("2024-08-09", "3QFinancialStatements_Consolidated_JP", "3Q", "2024-09-30", OP=800, FOP=1000),
        row("2024-10-29", "EarnForecastRevision", "FY", "2024-09-30", FOP=1150),
        row("2024-11-12", "FYFinancialStatements_Consolidated_JP", "FY", "2024-09-30", OP=1200, FOP=1300),
        row("2025-02-10", "1QFinancialStatements_Consolidated_JP", "1Q", "2025-09-30", OP=300, FOP=1200),
    ])
    ev = guidance_events(df)
    # 3Q は据え置き、修正の書類は +15% の大幅上方修正
    revisions = ev.query("kind == 'revision'")
    assert list(revisions["label"]) == ["据え置き（±1%未満）", "大幅上方修正（+10%以上）", "下方修正（−1〜10%）"]
    assert revisions["change"].iloc[1] == pytest.approx(0.15)
    # 本決算の実績 1200 は、直前の予想 1150 の +4.3%
    surprise = ev.query("kind == 'fy_surprise'").iloc[0]
    assert surprise["fy_end"] == "2024-09-30" and surprise["change"] == pytest.approx(1200 / 1150 - 1)
    # 本決算の F〜 は来期予想とみなす: 1300 は今期実績 1200 の +8.3%（増益）
    guidance = ev.query("kind == 'new_guidance'").iloc[0]
    assert guidance["fy_end"] == "2025-09-30" and guidance["label"] == "来期予想が増益（+5〜20%）"
    # 来期の1Q（1200）は、本決算で出た来期予想（1300）と比べて −7.7% の下方修正
    assert revisions["change"].iloc[2] == pytest.approx(1200 / 1300 - 1)


def test_falls_back_to_ordinary_income_and_skips_losses():
    df = frame([
        row("2024-05-10", "1QFinancialStatements_Consolidated_JP", "1Q", "2025-03-31", FOdP=100),   # 銀行など OP なし
        row("2024-08-09", "2QFinancialStatements_Consolidated_JP", "2Q", "2025-03-31", FOdP=90),
        row("2024-11-10", "3QFinancialStatements_Consolidated_JP", "3Q", "2025-03-31", FOP=-50, FOdP=-40, FNP=-60),
        row("2025-02-10", "EarnForecastRevision", "FY", "2025-03-31", FOP=-30, FOdP=-20, FNP=-10),
    ])
    ev = guidance_events(df)
    assert len(ev) == 2      # 赤字予想どうしの比較（3Q→修正）は出来事にしない
    assert ev["metric"].iloc[0] == "経常利益" and ev["change"].iloc[0] == pytest.approx(-0.1)


def test_leap_year_fiscal_year_end_matches():
    df = frame([
        row("2027-04-10", "FYFinancialStatements_Consolidated_JP", "FY", "2027-02-28", OP=100, FOP=110),
        row("2027-07-10", "1QFinancialStatements_Consolidated_JP", "1Q", "2028-02-29", OP=30, FOP=120),
    ])
    revisions = guidance_events(df).query("kind == 'revision'")
    assert len(revisions) == 1 and revisions["change"].iloc[0] == pytest.approx(120 / 110 - 1)


# ---------- J-Quants の取得・保存 ----------

def test_fetch_and_load_statements(tmp_path, monkeypatch):
    settings = {"paths": {"raw_jquants": "jq"}, "jquants": {"pause_seconds": 0}}
    monkeypatch.setattr(js, "resolve_path", lambda p: tmp_path / p)
    calls = []

    def fake_get_all(path, params, api_key, key=None, pause=0, sleep=None):
        calls.append(params["code"])
        if params["code"] == "99990":
            raise js.JQuantsError("HTTP 500")
        return [{"DiscDate": "2026-08-04", "DiscTime": "14:00:00", "Code": params["code"], "DiscNo": "1",
                 "DocType": "1QFinancialStatements_Consolidated_IFRS", "CurPerType": "1Q", "CurFYEn": "2027-03-31",
                 "OP": "1063473000000", "FOP": "3400000000000", "FOdP": ""}], {}

    monkeypatch.setattr(js, "get_all", fake_get_all)
    quality = QualityLog()
    assert js.fetch_statements(["7203", "9999"], settings, "K", quality, progress=lambda _: None,
                               sleep=lambda _: None) == 1
    assert calls == ["72030", "99990"] and quality.count("ERROR") == 1

    calls.clear()
    js.fetch_statements(["7203", "9999"], settings, "K", QualityLog(), progress=lambda _: None, sleep=lambda _: None)
    assert calls == ["99990"]   # 保存済みの銘柄は取りに行かない（失敗した銘柄だけ取り直す）

    df = js.load_statements(["7203", "9999"], settings)
    assert len(df) == 1 and df.loc[0, "code"] == "7203"
    assert df.loc[0, "FOP"] == 3.4e12 and np.isnan(df.loc[0, "FOdP"])
    assert df.loc[0, "disclosed_at"] == pd.Timestamp("2026-08-04 14:00")
    saved = json.loads((tmp_path / "jq" / "fins_summary" / "7203.json").read_text(encoding="utf-8"))
    assert "fetched_at" in saved


def test_revision_uses_first_changed_profit():
    df = frame([
        row("2024-05-10", "1QFinancialStatements_Consolidated_JP", "1Q", "2025-03-31", FOP=100, FOdP=120, FNP=80),
        row("2024-08-09", "EarnForecastRevision", "FY", "2025-03-31", FOP=100, FOdP=120, FNP=60),
        row("2024-11-10", "2QFinancialStatements_Consolidated_JP", "2Q", "2025-03-31", FOP=100, FOdP=120, FNP=60),
    ])
    ev = guidance_events(df)
    assert list(ev["metric"]) == ["純利益", "営業利益"]
    assert ev["change"].iloc[0] == pytest.approx(-0.25) and ev["change"].iloc[1] == 0
