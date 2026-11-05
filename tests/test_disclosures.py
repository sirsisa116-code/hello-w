"""適時開示の取得・分類・点数・イベントスタディのテスト（ネットにはつながない）。"""

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

import kabu.data.disclosures as disc
from kabu.analysis.disclosure import classify_disclosures, classify_title, group_by_code, score_disclosures
from kabu.event_study import base_date, event_returns, summarize_events
from kabu.quality import QualityLog
from kabu.scoring import load_weights, rank_stocks

CONF = load_weights()["disclosure"]


def categories(title):
    return [r["category"] for r in classify_title(title, CONF)]


# ---------- 分類 ----------

@pytest.mark.parametrize("title, expected", [
    ("通期業績予想の上方修正に関するお知らせ", ["upward_revision"]),
    ("業績予想の下方修正及び特別損失の計上に関するお知らせ", ["downward_revision", "impairment"]),
    ("業績予想の修正に関するお知らせ", ["revision_unknown"]),
    ("業績予想の修正及び配当予想の修正（増配）に関するお知らせ", ["revision_unknown", "dividend_up"]),
    ("配当予想の修正（無配）に関するお知らせ", ["dividend_down"]),
    ("無配から復配へ（配当予想の修正）に関するお知らせ", ["dividend_up"]),
    ("自己株式取得に係る事項の決定に関するお知らせ", ["buyback"]),
    ("自己株式の取得状況に関するお知らせ", []),
    ("自己株式の取得結果及び取得終了に関するお知らせ", []),
    ("ストック・オプションとしての新株予約権の発行に関するお知らせ", []),
    ("第三者割当による新株式発行に関するお知らせ", ["dilution"]),
    ("株式分割及び定款の一部変更に関するお知らせ", ["split"]),
    ("2026年３月期　決算短信［日本基準］（連結）", ["earnings"]),
    ("（訂正）「2027年３月期 第２四半期（中間期）決算短信〔日本基準〕（連結）」の一部訂正について", []),
    ("特別調査委員会の設置に関するお知らせ", ["misconduct"]),
    ("役員人事に関するお知らせ", []),
])
def test_classify_title(title, expected):
    assert categories(title) == expected


# ---------- 取得した応答の読み取り ----------

SAMPLE = {"items": [{"Tdnet": {
    "id": "1251171", "pubdate": "2026-05-14 19:30:00", "company_code": "56440", "company_name": "メタルアート",
    "title": "2026年３月期　決算短信［日本基準］（連結）",
    "document_url": "https://example/doc.pdf", "url_xbrl": "https://example/x.zip", "markets_string": "東"}}]}


def test_parse_items_and_code():
    df = disc.parse_items(SAMPLE)
    assert df.loc[0, "code"] == "5644"
    assert df.loc[0, "pubdate"] == pd.Timestamp("2026-05-14 19:30")
    assert bool(df.loc[0, "has_xbrl"])
    assert disc.normalize_tdnet_code("285A0") == "285A"


def make_item(i, code, pubdate, title):
    return {"Tdnet": {"id": str(i), "pubdate": pubdate, "company_code": code + "0",
                      "company_name": f"会社{code}", "title": title, "url_xbrl": None}}


def test_update_disclosures_caches_and_reports_failures(tmp_path, monkeypatch):
    settings = {"paths": {"raw_tdnet": str(tmp_path / "tdnet")}, "sources": {"tdnet_url": "x"},
                "disclosures": {"pause_seconds": 0}}
    monkeypatch.setattr(disc, "resolve_path", lambda p: tmp_path / "tdnet" if p.endswith("tdnet") else p)
    calls = []

    def fake_fetch(url, day):
        calls.append(day)
        if day == date(2026, 11, 2):
            return None, "接続できません"
        if day == date(2026, 11, 3):
            return {"items": [make_item(k, "1111", "2026-11-03 15:00:00", "x") for k in range(disc.LIMIT)]}, ""
        return {"items": [make_item(day.day, "1111", f"{day} 15:30:00", "上方修正のお知らせ")]}, ""

    monkeypatch.setattr(disc, "fetch_day", fake_fetch)
    quality = QualityLog()
    df, failed = disc.update_disclosures(date(2026, 11, 1), date(2026, 11, 5), settings, quality,
                                         today=date(2026, 11, 5), progress=lambda _: None)
    assert failed == [date(2026, 11, 2), date(2026, 11, 3)]          # 取得失敗と、上限に達した日
    assert sorted(df["pubdate"].dt.day) == [1, 4, 5]
    assert quality.count("ERROR") == 2

    # 2回目: 過去の日は手元の分を使い、今日・昨日と、前回取れなかった日だけ取りに行く
    calls.clear()
    disc.update_disclosures(date(2026, 11, 1), date(2026, 11, 5), settings, QualityLog(),
                            today=date(2026, 11, 5), progress=lambda _: None)
    assert calls == [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4), date(2026, 11, 5)]


# ---------- 点数 ----------

def events_for(rows):
    df = pd.DataFrame(rows, columns=["id", "pubdate", "code", "company_name", "title"])
    df["pubdate"] = pd.to_datetime(df["pubdate"])
    return group_by_code(classify_disclosures(df, CONF))


def test_score_disclosures_decay_and_future():
    events = events_for([
        ["1", "2026-11-05 15:30", "1111", "A", "通期業績予想の上方修正に関するお知らせ"],       # 当日 → 満点 +8
        ["2", "2026-10-29 16:00", "1111", "A", "自己株式取得に係る事項の決定に関するお知らせ"],  # 7日前 → +5 × 50%
        ["3", "2026-10-01 16:00", "1111", "A", "下方修正のお知らせ"],                          # 期間外
        ["4", "2026-11-06 08:00", "1111", "A", "下方修正のお知らせ"],                          # 基準日の翌日（未来）
    ])
    s = score_disclosures(events["1111"], pd.Timestamp("2026-11-05"), CONF, [])
    assert s.score == pytest.approx(50 + 8 + 5 * 0.5)
    assert len(s.contributions) == 2


def test_score_disclosures_missing_when_fetch_failed():
    s = score_disclosures(None, pd.Timestamp("2026-11-05"), CONF, [date(2026, 11, 2)])
    assert s.score is None and "取得できず" in s.missing[0]
    ok = score_disclosures(None, pd.Timestamp("2026-11-05"), CONF, [date(2026, 9, 1)])   # 期間外の失敗は関係ない
    assert ok.score == 50


def test_rank_stocks_uses_disclosures():
    weights = load_weights()
    dates = pd.bdate_range(end="2026-11-05", periods=300, name="date")
    closes = np.linspace(1000, 1200, 300)
    frame = pd.DataFrame({"open": closes, "high": closes, "low": closes, "close": closes, "adj_close": closes,
                          "volume": 1e6, "source": "t"}, index=dates)
    universe = pd.DataFrame({"code": ["1111", "2222"], "name": ["A", "B"], "sector33": ["x", "y"]})
    events = events_for([["1", "2026-11-05 15:30", "1111", "A", "上方修正に関するお知らせ"]])
    ranking, details, _ = rank_stocks(universe, {"1111": frame, "2222": frame}, dates[-1], weights, QualityLog(),
                                      disclosure_events=events, disclosure_failed=[])
    assert list(ranking["code"]) == ["1111", "2222"]
    assert ranking.loc[0, "disclosure_score"] == 58 and ranking.loc[1, "disclosure_score"] == 50
    assert "上方修正" in ranking.loc[0, "plus_reasons"]

    ranking, _, _ = rank_stocks(universe, {"1111": frame, "2222": frame}, dates[-1], weights, QualityLog(),
                                disclosure_events=events, disclosure_failed=[date(2026, 11, 4)])
    assert ranking["disclosure_score"].isna().all()
    assert "取得できず" in ranking.loc[0, "missing"]


# ---------- イベントスタディ ----------

def test_base_date():
    assert base_date(pd.Timestamp("2026-11-05 08:30")) == pd.Timestamp("2026-11-04")   # 寄り付き前 → 当日の始値で買える
    assert base_date(pd.Timestamp("2026-11-05 15:30")) == pd.Timestamp("2026-11-05")   # 引け後 → 翌営業日の始値


def test_event_returns_separates_gap_and_drift():
    dates = pd.bdate_range("2026-10-01", periods=30, name="date")
    bench = pd.DataFrame({"open": 100.0, "close": 100.0}, index=dates)
    # 開示の翌朝に 1000 → 1100 と窓を開けて始まり、その後は毎日 +10 円
    close = np.r_[np.full(10, 1000.0), 1100.0 + 10 * np.arange(20)]
    open_ = np.r_[np.full(10, 1000.0), 1100.0, close[10:-1]]
    stock = pd.DataFrame({"open": open_, "close": close}, index=dates)
    events = pd.DataFrame({"pubdate": [dates[9] + pd.Timedelta(hours=15, minutes=30)], "code": ["1111"],
                           "company_name": ["A"], "title": ["上方修正"], "category": ["upward_revision"],
                           "label": ["上方修正"], "points": [8.0]})
    out = event_returns(events, {"1111": stock}, bench, [1, 5])
    assert out.loc[0, "gap_excess"] == pytest.approx(10.0)              # 買う前に +10% 動いてしまっている
    assert out.loc[0, "ret_5"] == pytest.approx((1140 / 1100 - 1) * 100, abs=1e-3)  # 買ってから5営業日後（小数第3位で丸め）
    summary = summarize_events(out, [1, 5])
    assert summary.loc[0, "件数"] == 1 and summary.loc[0, "反応（買う前）中央値(%)"] == pytest.approx(10.0)
