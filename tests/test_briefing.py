"""決算の中身のまとめと、レポートの「材料の整理」の欄のテスト（ネットにはつながない）。"""

import numpy as np
import pandas as pd
import pytest

from kabu.analysis.earnings_brief import earnings_brief, previous_forecast
from kabu.briefing import movers_section, my_stocks_section, new_disclosures_section


def fig(doc_id, pubdate, doc_type, item, period, kind, value, stage="", consolidated="連結"):
    return {"id": doc_id, "code": "9059", "pubdate": pd.Timestamp(pubdate), "item": item, "period": period,
            "consolidated": consolidated, "stage": stage, "kind": kind, "timing": "", "value": value,
            "name": item, "doc_type": doc_type}


# 実物（カンダ）に近い形: 本決算 → 1Q → 2Q
FIGURES = pd.DataFrame([
    fig("A", "2026-05-12 15:00", "acedjpsm", "operating_income", "CurrentYearDuration", "result", 3_646e6),
    fig("A", "2026-05-12 15:00", "acedjpsm", "operating_income", "NextYearDuration", "forecast", 3_810e6),
    fig("Q1", "2026-08-05 15:00", "qcedjpsy", "operating_income", "CurrentAccumulatedQ1Duration", "result", 800e6),
    fig("Q1", "2026-08-05 15:00", "qcedjpsy", "operating_income", "CurrentYearDuration", "forecast", 3_810e6),
    fig("Q2", "2026-11-04 17:00", "scedjpsy", "operating_income", "CurrentAccumulatedQ2Duration", "result", 1_710e6),
    fig("Q2", "2026-11-04 17:00", "scedjpsy", "operating_income", "PriorAccumulatedQ2Duration", "result", 1_926e6),
    fig("Q2", "2026-11-04 17:00", "scedjpsy", "operating_income", "CurrentYearDuration", "forecast", 3_500e6),
])


def test_quarterly_brief_with_progress_and_revision():
    text, change = earnings_brief(FIGURES, "Q2")
    assert "営業利益 1,710百万円（前年同期比 -11.2%）" in text
    assert "通期予想 3,500百万円、進捗率 49%" in text
    assert "2026-08-05の前回の決算短信から -8.1% 修正" in text
    assert change == pytest.approx(3500 / 3810 - 1)


def test_previous_forecast_stops_at_annual_report():
    # 1Q の前回の予想は、本決算で出た来期予想
    assert previous_forecast(FIGURES, pd.Timestamp("2026-08-05 15:00"), "operating_income")[0] == 3_810e6
    # 本決算より前にはさかのぼらない（別の決算期のため）
    older = pd.concat([FIGURES, pd.DataFrame([fig("OLD", "2025-11-01", "scedjpsy", "operating_income",
                                                   "CurrentYearDuration", "forecast", 9_999e6)])])
    assert previous_forecast(older, pd.Timestamp("2026-05-12 15:00"), "operating_income")[0] == 9_999e6
    assert previous_forecast(older, pd.Timestamp("2026-08-05 15:00"), "operating_income")[0] == 3_810e6


def test_annual_brief_with_surprise_and_next_year():
    figs = pd.concat([FIGURES, pd.DataFrame([
        fig("FY", "2027-05-12 15:00", "acedjpsm", "operating_income", "CurrentYearDuration", "result", 3_850e6),
        fig("FY", "2027-05-12 15:00", "acedjpsm", "operating_income", "PriorYearDuration", "result", 3_646e6),
        fig("FY", "2027-05-12 15:00", "acedjpsm", "operating_income", "NextYearDuration", "forecast", 4_000e6),
    ])])
    text, change = earnings_brief(figs, "FY")
    assert "前期比 +5.6%" in text and "直前の会社予想 3,500百万円 に対し +10.0%" in text
    assert "来期予想 4,000百万円（+3.9%）" in text
    assert change == pytest.approx(3850 / 3500 - 1)


def test_sections_render():
    names = {"9059": "カンダ", "7203": "トヨタ自動車"}
    indicators = pd.DataFrame({
        "close": [1500.0, 3000.0], "day_change_pct": [1.2, -3.5], "ma_alignment": [1.0, -1.0],
        "deviation25": [3.0, -8.0], "high52_distance": [0.0, -20.0], "volume_ratio_signed": [3.5, -4.0],
        "turnover20_oku": [5.0, 500.0]}, index=["9059", "7203"])
    classified = pd.DataFrame([
        {"id": "Q2", "pubdate": pd.Timestamp("2026-11-04 17:00"), "code": "9059", "company_name": "カンダ",
         "title": "第２四半期決算短信", "category": "earnings", "label": "決算発表"},
        {"id": "X", "pubdate": pd.Timestamp("2026-11-04 16:00"), "code": "7203", "company_name": "トヨタ",
         "title": "業績予想の下方修正に関するお知らせ", "category": "downward_revision", "label": "下方修正"},
    ])
    mine = "\n".join(my_stocks_section({"9059": "保有中"}, indicators, names, classified, FIGURES,
                                       pd.Timestamp("2026-11-05")))
    assert "| 9059 | カンダ | 保有中 | 1,500 | +1.20% | 上昇配列" in mine and "進捗率 49%" in mine
    new = "\n".join(new_disclosures_section(classified, FIGURES, names, pd.Timestamp("2026-11-04 15:00"),
                                            {"9059", "7203"}))
    assert "### 業績予想の修正（1件）" in new and "### 決算発表（1件）" in new
    movers = "\n".join(movers_section(indicators, names))
    assert "52週高値を更新（1銘柄" in movers and "7203 トヨタ自動車 -3.50%、出来高 4.0倍" in movers
    assert "まだ登録されていません" in "\n".join(my_stocks_section({}, indicators, names, classified, FIGURES,
                                                                 pd.Timestamp("2026-11-05")))
