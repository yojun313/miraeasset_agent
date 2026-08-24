from pathlib import Path

import pytest

from app.config import CORPUS_DIR
from app.data.parser import list_documents_in_folder, list_sections, parse_file, read_section

ANNUAL_REPORT = (
    CORPUS_DIR / "raw/periodic/삼성전자/20240312000736_annual_2023_12/20240312000736.xml"
)
AUDIT_ATTACHMENT = (
    CORPUS_DIR / "raw/periodic/삼성전자/20240312000736_annual_2023_12/20240312000736_00760.xml"
)
EXCHANGE_FILING = CORPUS_DIR / "raw/exchange/삼성바이오로직스/20240913800007/20240913800007.xml"
HOLDING_FILING = CORPUS_DIR / "raw/holding/셀트리온/20250804000301/20250804000301.xml"


pytestmark = pytest.mark.skipif(not CORPUS_DIR.exists(), reason="corpus/ not present")


def test_list_sections_annual_report():
    sections = list_sections(ANNUAL_REPORT)
    assert "I. 회사의 개요" in sections
    assert "II. 사업의 내용" in sections
    assert "III. 재무에 관한 사항" in sections
    # sections should be in document order and reasonably numerous
    assert len(sections) > 50


def test_read_section_returns_relevant_slice_only():
    text = read_section(ANNUAL_REPORT, "2. 회사의 연혁")
    assert "회사의 연혁" in text
    assert "회사의 개요" not in text.split("\n\n## ")[0]  # doesn't bleed into the next section's own heading text
    assert "자본금 변동사항" not in text  # next section's heading shouldn't leak in


def test_read_section_includes_table_content_as_markdown():
    text = read_section(ANNUAL_REPORT, "4. 주식의 총수 등")
    assert "|" in text  # table rows converted to markdown-ish pipes


def test_audit_report_attachment_parses():
    sections = list_sections(AUDIT_ATTACHMENT)
    assert len(sections) > 0


def test_exchange_html_filing_has_no_sections_but_has_text():
    doc = parse_file(EXCHANGE_FILING)
    assert doc.sections == []
    assert "공급계약" in doc.text
    assert "|" in doc.text  # key-value table converted to markdown-ish pipes


def test_holding_filing_sections():
    sections = list_sections(HOLDING_FILING)
    assert any("대량보유" in s for s in sections)


def test_list_documents_in_folder_flags_main_vs_attachment():
    infos = list_documents_in_folder(ANNUAL_REPORT.parent)
    names = {fi.path.name: fi.is_main for fi in infos}
    assert names["20240312000736.xml"] is True
    assert names["20240312000736_00760.xml"] is False
    assert names["20240312000736_00761.xml"] is False
