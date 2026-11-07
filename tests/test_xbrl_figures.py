"""XBRL から主要な数字を取り出す部分と、毎日の取得・保存のテスト（ネットにはつながない）。"""

import io
import zipfile
from datetime import date

import pandas as pd
import pytest

import kabu.data.xbrl_store as store
from kabu.data.xbrl import parse_ixbrl
from kabu.data.xbrl_figures import extract_key_figures, parse_context, revision_change, revision_summary
from kabu.quality import QualityLog


def fact(name, context, value, scale="6", sign=""):
    sign_attr = f' sign="{sign}"' if sign else ""
    return (f'<ix:nonFraction name="tse-ed-t:{name}" contextRef="{context}" scale="{scale}"{sign_attr} '
            f'format="ixt:num-dot-decimal">{value}</ix:nonFraction>')


# 実物（TVE の業績予想の修正）と同じ形
TVE = "<html><body><ix:nonNumeric name='tse-ed-t:Table' contextRef='X'><table>" + "".join([
    fact("NetSales", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "11,200"),
    fact("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "1,150"),
    fact("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "820"),
    fact("NetSales", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "11,524"),
    fact("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "1,205"),
    fact("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "1,104"),
    fact("OperatingIncome", "PriorYearDuration_ConsolidatedMember_CurrentMember_ResultMember", "595"),
]) + "</table></ix:nonNumeric></body></html>"

# 実物（ヤマザキ）: 中間期の予想で、営業損益が赤字（sign="-"）
YAMAZAKI = "<html><body>" + "".join([
    fact("OperatingIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_PreviousMember_ForecastMember", "30", sign="-"),
    fact("OperatingIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_CurrentMember_ForecastMember", "36", sign="-"),
]) + "</body></html>"

# 実物（武田薬品の決算短信サマリー、IFRS）と同じ項目名
TAKEDA = "<html><body>" + "".join([
    fact("SalesIFRS", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember", "2,406,997"),
    fact("OperatingIncomeIFRS", "CurrentYearDuration_ConsolidatedMember_ForecastMember", "453,000"),
    fact("BasicEarningsPerShareIFRS", "CurrentYearDuration_ConsolidatedMember_ForecastMember", "131.28", scale="0"),
    fact("DividendPerShare", "CurrentYearDuration_AnnualMember_NonConsolidatedMember_ForecastMember", "204.00", scale="0"),
    fact("CapitalAdequacyRatio", "CurrentAccumulatedQ2Instant_ConsolidatedMember_ResultMember", "48.6", scale="-2"),
]) + "</body></html>"


def figures_of(html, doc_type):
    facts = parse_ixbrl(html)
    facts.insert(0, "file", "x")
    facts.insert(1, "doc_type", doc_type)
    return extract_key_figures(facts)


def test_parse_context():
    c = parse_context("CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember")
    assert c == {"period": "CurrentYearDuration", "consolidated": "連結", "stage": "previous",
                 "kind": "forecast", "timing": ""}
    d = parse_context("CurrentYearDuration_AnnualMember_NonConsolidatedMember_ForecastMember")
    assert d["consolidated"] == "個別" and d["timing"] == "Annual"


def test_revision_summary_and_change():
    tve = figures_of(TVE, "rvfc")
    assert revision_summary(tve) == ("通期連結予想: 営業利益 1,150→1,205百万円（+4.8%） / "
                                     "純利益 820→1,104百万円（+34.6%）")
    assert revision_change(tve) == pytest.approx(1205 / 1150 - 1)

    yamazaki = figures_of(YAMAZAKI, "rvfc")
    assert "中間期連結予想: 営業利益 -30→-36百万円" in revision_summary(yamazaki)
    assert revision_change(yamazaki) is None   # 通期予想がなく、赤字予想どうしの率には意味がない


def test_extract_key_figures_ifrs_summary():
    figures = figures_of(TAKEDA, "scedifsy").set_index("item")
    assert figures.loc["revenue", "value"] == 2_406_997e6
    assert figures.loc["operating_income", "kind"] == "forecast"
    assert figures.loc["eps", "value"] == pytest.approx(131.28)
    assert figures.loc["dividend_per_share", "consolidated"] == "個別"
    assert "CapitalAdequacyRatio" not in set(figures["name"])     # 取り出す項目以外は捨てる
    assert figures_of(TAKEDA, "scediffr").empty                  # 財務諸表の本体は今は使わない


# ---------- 毎日の取得と保存 ----------

def make_zip(html, filename):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(filename, html)
    return buf.getvalue()


def test_collect_xbrl_saves_and_skips_done(tmp_path, monkeypatch):
    settings = {"paths": {"xbrl": "xbrl"}, "xbrl": {"retention_days": 31, "pause_seconds": 0}}
    monkeypatch.setattr(store, "resolve_path", lambda p: tmp_path / p)
    disclosures = pd.DataFrame([
        {"id": "1", "pubdate": pd.Timestamp("2026-11-04 16:00"), "code": "6466", "company_name": "TVE",
         "title": "業績予想の修正に関するお知らせ", "has_xbrl": True, "url_xbrl": "u1", "document_url": ""},
        {"id": "2", "pubdate": pd.Timestamp("2026-10-01 16:00"), "code": "6466", "company_name": "TVE",
         "title": "2027年3月期 第1四半期決算短信", "has_xbrl": True, "url_xbrl": "u2", "document_url": ""},
        {"id": "3", "pubdate": pd.Timestamp("2026-11-04 16:00"), "code": "9999", "company_name": "対象外",
         "title": "業績予想の修正に関するお知らせ", "has_xbrl": True, "url_xbrl": "u3", "document_url": ""},
        {"id": "4", "pubdate": pd.Timestamp("2026-11-04 16:00"), "code": "6466", "company_name": "TVE",
         "title": "役員人事のお知らせ", "has_xbrl": True, "url_xbrl": "u4", "document_url": ""},
    ])
    monkeypatch.setattr(store, "update_disclosures", lambda *a, **k: (disclosures, []))
    calls = []

    def fake_fetch(url):
        calls.append(url)
        if url == "u2":
            return None, "not_found", "保存期間切れ"
        return make_zip(TVE, "tse-rvfc-64660-20261104564423-ixbrl.htm"), "ok", ""

    monkeypatch.setattr(store, "fetch_zip", fake_fetch)
    quality = QualityLog()
    saved = store.collect_xbrl(settings, {"6466"}, quality, date(2026, 11, 5), progress=lambda _: None)
    assert saved == 1 and sorted(calls) == ["u1", "u2"]           # 対象外の銘柄・関係ない開示は取らない
    assert any("見つからなかった" in i.message for i in quality.issues)

    log = store.load_fetch_log(settings).set_index("id")
    assert log.loc["1", "status"] == "ok" and log.loc["2", "status"] == "not_found"
    details = store.revision_details(settings)
    assert details["1"].startswith("通期連結予想: 営業利益 1,150→1,205百万円")

    calls.clear()
    assert store.collect_xbrl(settings, {"6466"}, QualityLog(), date(2026, 11, 5), progress=lambda _: None) == 0
    assert calls == []                                          # 一度取ったもの・404 だったものは取りに行かない


def test_direct_url():
    assert store.direct_url("https://webapi.yanoshin.jp/rd.php?https://www.release.tdnet.info/inbs/1.zip") == \
        "https://www.release.tdnet.info/inbs/1.zip"


def test_revision_change_uses_first_changed_profit():
    """実際にあった例: 営業利益・経常利益の予想は据え置きで、純利益だけ修正。"""
    html = "<html><body>" + "".join([
        fact("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "53,500"),
        fact("OperatingIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "53,500"),
        fact("OrdinaryIncome", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "67,500"),
        fact("OrdinaryIncome", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "67,500"),
        fact("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_PreviousMember_ForecastMember", "40,000"),
        fact("ProfitAttributableToOwnersOfParent", "CurrentYearDuration_ConsolidatedMember_CurrentMember_ForecastMember", "36,000"),
    ]) + "</body></html>"
    assert revision_change(figures_of(html, "rvfc")) == pytest.approx(-0.1)
    flat = html.replace(">36,000<", ">40,000<")
    assert revision_change(figures_of(flat, "rvfc")) == 0.0


def test_non_consolidated_summary():
    """実際にあった例: 非連結の会社（4733）は書類の種類が snedjpsy、純利益が NetIncome。"""
    html = "<html><body>" + "".join([
        fact("NetIncome", "CurrentAccumulatedQ2Duration_NonConsolidatedMember_ResultMember", "10,135"),
        fact("OperatingIncome", "CurrentYearDuration_NonConsolidatedMember_ForecastMember", "27,600"),
    ]) + "</body></html>"
    figures = figures_of(html, "snedjpsy").set_index("item")
    assert figures.loc["net_income", "value"] == 10_135e6
    assert figures.loc["operating_income", "consolidated"] == "個別"


def test_empty_or_old_results_are_retried():
    v = store.EXTRACT_VERSION
    log = pd.DataFrame([
        {"id": "1", "status": "ok", "message": "", "version": v},
        {"id": "2", "status": "ok", "message": store.EMPTY_MESSAGE, "version": v},
        {"id": "3", "status": "not_found", "message": "x", "version": v},
        {"id": "4", "status": "error", "message": "x", "version": v},
        {"id": "5", "status": "ok", "message": store.EMPTY_MESSAGE, "version": v},
        {"id": "5", "status": "ok", "message": "", "version": v},          # 読み直して数字が取れた
        {"id": "6", "status": "ok", "message": "", "version": v - 1},      # 古い版で取り出した → 読み直す
    ])
    assert store.finished_ids(log) == {"1", "3", "5"}
    # 版の列がない古い記録は、すべて版1とみなす
    assert store.finished_ids(log.drop(columns="version")) == {"3"}


def test_refetched_figures_replace_old_rows(tmp_path):
    path = tmp_path / "key_figures.csv"
    first = pd.DataFrame([{"id": "A", "code": "1111", "item": "eps", "value": 1.0},
                          {"id": "B", "code": "2222", "item": "eps", "value": 2.0}])
    store._append(path, first)
    store._replace_figures(path, "A", pd.DataFrame([{"id": "A", "code": "1111", "item": "bps", "value": 9.0}]))
    saved = pd.read_csv(path, dtype={"id": str}, encoding="utf-8-sig")
    assert sorted(zip(saved["id"], saved["item"])) == [("A", "bps"), ("B", "eps")]


def test_old_log_gets_version_column(tmp_path):
    path = tmp_path / "fetch_log.csv"
    pd.DataFrame([{"id": "1", "code": "1", "pubdate": "x", "title": "t", "status": "ok", "message": "",
                   "fetched_at": "x"}]).to_csv(path, index=False, encoding="utf-8-sig")
    store._upgrade_log(path)
    assert pd.read_csv(path, encoding="utf-8-sig")["version"].tolist() == [1]
