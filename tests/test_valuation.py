"""予想PER・PBR・予想配当利回りのテスト（ネットにはつながない）。"""

import numpy as np
import pandas as pd
import pytest

from kabu.analysis.valuation import compute_valuation, format_valuation, jquants_inputs, latest_inputs, xbrl_inputs


def fig(doc_id, pubdate, doc_type, item, period, kind, value, consolidated="連結", stage="", timing=""):
    return {"id": doc_id, "code": "9059", "pubdate": pd.Timestamp(pubdate), "item": item, "period": period,
            "consolidated": consolidated, "stage": stage, "kind": kind, "timing": timing, "value": value,
            "name": item, "doc_type": doc_type}


def test_xbrl_inputs_from_quarterly_summary():
    # 実物（カンダの第2四半期）に近い形: 1株純資産の直接の値はなく、自己資本と株式数から出す
    figures = pd.DataFrame([
        fig("Q2", "2026-11-04 17:00", "scedjpsy", "eps", "CurrentYearDuration", "forecast", 119.25),
        fig("Q2", "2026-11-04 17:00", "scedjpsy", "dividend_per_share", "CurrentYearDuration", "forecast", 26.0,
            consolidated="個別", timing="Annual"),
        fig("Q2", "2026-11-04 17:00", "scedjpsy", "equity", "CurrentAccumulatedQ2Instant", "result", 28_898e6),
        fig("Q2", "2026-11-04 17:00", "scedjpsy", "shares_issued", "CurrentAccumulatedQ2Instant", "result",
            23_308_720, consolidated="個別"),
        fig("Q2", "2026-11-04 17:00", "scedjpsy", "treasury_shares", "CurrentAccumulatedQ2Instant", "result",
            1_883_098, consolidated="個別"),
    ])
    inputs = xbrl_inputs(figures).set_index("metric")
    assert inputs.loc["eps", "value"] == pytest.approx(119.25)
    assert inputs.loc["dps", "value"] == 26.0
    assert inputs.loc["bps", "value"] == pytest.approx(28_898e6 / (23_308_720 - 1_883_098))
    assert (inputs["source"] == "XBRL").all()


def test_xbrl_annual_uses_next_year_forecast():
    figures = pd.DataFrame([
        fig("FY", "2026-05-12", "acedjpsm", "eps", "CurrentYearDuration", "result", 110.0),
        fig("FY", "2026-05-12", "acedjpsm", "eps", "NextYearDuration", "forecast", 120.0),
        fig("FY", "2026-05-12", "acedjpsm", "bps", "CurrentYearInstant", "result", 1300.0),
    ])
    inputs = xbrl_inputs(figures).set_index("metric")
    assert inputs.loc["eps", "value"] == 120.0 and inputs.loc["bps", "value"] == 1300.0


def test_jquants_inputs_with_real_toyota_fields():
    # 実物（トヨタの第1四半期、J-Quants）の項目と値
    statements = pd.DataFrame([{
        "code": "7203", "disclosed_at": pd.Timestamp("2026-08-04 14:00"), "DocType": "1QFinancialStatements_Consolidated_IFRS",
        "CurPerType": "1Q", "FEPS": 272.17, "FDivAnn": 100.0, "ShEq": 37_308_828e6,
        "ShOutFY": 14_594_987_460, "TrShFY": 2_753_153_800}])
    inputs = jquants_inputs(statements).set_index("metric")
    assert inputs.loc["eps", "value"] == pytest.approx(272.17)
    assert inputs.loc["bps", "value"] == pytest.approx(37_308_828e6 / (14_594_987_460 - 2_753_153_800))
    assert (inputs["source"] == "J-Quants").all()


def test_latest_inputs_prefers_newer_and_compute():
    old = pd.DataFrame([{"code": "7203", "metric": "eps", "value": 272.17, "date": pd.Timestamp("2026-08-04"),
                         "source": "J-Quants"},
                        {"code": "7203", "metric": "bps", "value": 3150.0, "date": pd.Timestamp("2026-08-04"),
                         "source": "J-Quants"}])
    new = pd.DataFrame([{"code": "7203", "metric": "eps", "value": 250.0, "date": pd.Timestamp("2026-11-05"),
                         "source": "XBRL"},
                        {"code": "9999", "metric": "eps", "value": -10.0, "date": pd.Timestamp("2026-11-05"),
                         "source": "XBRL"}])
    inputs = latest_inputs(new, old)
    v = compute_valuation(inputs, pd.Series({"7203": 3000.0, "9999": 500.0, "1111": 100.0}))
    assert v.loc["7203", "per_forecast"] == pytest.approx(12.0)                # 新しい XBRL の予想を使う
    assert v.loc["7203", "pbr"] == pytest.approx(3000 / 3150)                  # PBR は J-Quants しかない
    assert v.loc["7203", "eps_date"] == "2026-11-05（XBRL）"
    assert np.isnan(v.loc["9999", "per_forecast"]) and v.loc["9999", "per_note"] == "赤字予想"
    assert format_valuation(v.loc["9999"]) == ("赤字予想", "—", "—")
    assert format_valuation(v.loc["7203"])[:2] == ("12.0倍", "0.95倍")
    assert format_valuation(v.loc["1111"]) == ("—", "—", "—")                 # 数字がない銘柄
