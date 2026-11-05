"""XBRL の読み取りのテスト。実物で見つかった書き方（入れ子・空のタグ・「－」）を再現して確かめる。"""

import io
import zipfile

import numpy as np
import pytest

from kabu.data.xbrl import document_type, parse_ixbrl, read_xbrl_zip, to_number

SAMPLE = """<html xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body>
<ix:nonFraction name="tse-ed-t:NetSales" contextRef="CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"
  unitRef="JPY" decimals="-6" scale="6" format="ixt:num-dot-decimal">8,451</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:ChangeInOperatingIncome" contextRef="CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"
  unitRef="Pure" scale="-2" sign="-" format="ixt:num-dot-decimal">11.2</ix:nonFraction>
<ix:nonFraction name="tse-ed-t:DilutedNetIncomePerShare" contextRef="CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"
  unitRef="JPYPerShares" xsi:nil="true"/>
<p>－</p><p>長い文章がここに続く</p>
<ix:nonNumeric name="tse-rvfc-t:RevisionTable" contextRef="CurrentYearDuration">
  <table><tr><td>前回予想</td><td>
    <ix:nonFraction name="tse-rvfc-t:OperatingIncome" contextRef="CurrentYearDuration_PreviousMember" scale="6"
      format="ixt:num-dot-decimal">1,000</ix:nonFraction></td>
  <td>今回修正</td><td>
    <ix:nonFraction name="tse-rvfc-t:OperatingIncome" contextRef="CurrentYearDuration_ForecastMember" scale="6"
      format="ixt:num-dot-decimal">1,250</ix:nonFraction></td>
  <td><ix:nonFraction name="tse-rvfc-t:Dividend" contextRef="X" format="ixt:fixed-zero">－</ix:nonFraction></td>
  </tr></table>
</ix:nonNumeric>
</body></html>"""


def test_parse_ixbrl_handles_nesting_nil_and_scale():
    facts = parse_ixbrl(SAMPLE).set_index(["name", "context"])
    assert facts.loc[("NetSales", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"), "value"] == 8_451e6
    assert facts.loc[("ChangeInOperatingIncome", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember"),
                     "value"] == pytest.approx(-0.112)
    # 空のタグは値なしで、後ろの文章を取り込まない
    nil = facts.loc[("DilutedNetIncomePerShare", "CurrentAccumulatedQ2Duration_ConsolidatedMember_ResultMember")]
    assert np.isnan(nil["value"]) and nil["text"] == ""
    # 文章のかたまりの中に入れ子になった数値も拾える
    assert facts.loc[("OperatingIncome", "CurrentYearDuration_PreviousMember"), "value"] == 1_000e6
    assert facts.loc[("OperatingIncome", "CurrentYearDuration_ForecastMember"), "value"] == 1_250e6
    assert facts.loc[("Dividend", "X"), "value"] == 0.0
    assert facts.loc[("RevisionTable", "CurrentYearDuration"), "kind"] == "text"


def test_to_number():
    assert to_number("1,234", "6", "", "", False) == 1_234e6
    assert to_number("31.63", "0", "", "", False) == pytest.approx(31.63)
    assert np.isnan(to_number("－", "6", "", "ixt:num-dot-decimal", False))
    assert to_number("－", "", "", "ixt-sec:numwordsen zerodash", False) == 0.0
    assert np.isnan(to_number("12", "6", "", "", True))


def test_read_xbrl_zip_and_document_type():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("XBRLData/Summary/tse-scedjpsy-97990-20261104397990-ixbrl.htm", SAMPLE)
        z.writestr("XBRLData/Summary/tse-scedjpsy-97990-20261104397990-def.xml", "<x/>")
        z.writestr("tse-rvfc-61470-20261026556269-ixbrl.htm", SAMPLE)
    facts = read_xbrl_zip(buf.getvalue())
    assert set(facts["doc_type"]) == {"scedjpsy", "rvfc"}
    assert document_type("XBRLData/Attachment/0600000-scpl15-tse-scedjpfr-97990-2026-09-30-01-2026-11-04-ixbrl.htm") == "scedjpfr"
