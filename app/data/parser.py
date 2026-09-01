"""Parses DART filing files into clean text.

Two source formats appear in the corpus:
  - "dart_xml": DART's own <DOCUMENT>/<BODY> schema (periodic/major/holding
    filings, and periodic report attachments like audit reports). Section
    headers are <TITLE ATOC="Y" ATOCID="..."> elements in document order.
  - "html": exchange filings (saved with a .xml extension but are plain
    HTML forms) and the "_viewer.html" fallback for the 3 pdf+html filings.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from bs4 import BeautifulSoup
from lxml import etree

CELL_TAGS = {"TD", "TE", "TH", "TU"}


@dataclass(frozen=True)
class Section:
    atocid: str | None
    title: str
    offset: int


@dataclass(frozen=True)
class ParsedDoc:
    text: str
    sections: list[Section]


@dataclass(frozen=True)
class FileInfo:
    path: Path
    is_main: bool
    label: str


def sniff_format(path: Path) -> str:
    head = path.read_bytes()[:500].decode("utf-8", errors="ignore").lower()
    return "html" if "<html" in head else "dart_xml"


def _elem_text(el) -> str:
    return " ".join("".join(el.itertext()).split())


def _table_to_markdown(table_el) -> str:
    rows: list[str] = []
    for tr in table_el.iter("TR"):
        cells = [c for c in tr if c.tag in CELL_TAGS]
        if not cells:
            continue
        row_text = " | ".join(_elem_text(c) for c in cells)
        if row_text.strip(" |"):
            rows.append(row_text)
    return "\n".join(rows)


def _linearize(body_el) -> tuple[str, list[Section]]:
    parts: list[str] = []
    sections: list[Section] = []
    pos = 0

    def emit(s: str) -> None:
        nonlocal pos
        if not s:
            return
        parts.append(s)
        pos += len(s)

    def walk(el) -> None:
        tag = el.tag
        if not isinstance(tag, str):  # comments / processing instructions
            return

        if tag == "TABLE":
            md = _table_to_markdown(el)
            if md:
                emit("\n" + md + "\n")
            tail = (el.tail or "").strip()
            if tail:
                emit(tail + " ")
            return

        if tag == "TITLE":
            text = _elem_text(el)
            if el.get("ATOC") == "Y":
                sections.append(Section(el.get("ATOCID"), text, pos))
            if text:
                emit("\n\n## " + text + "\n")
            tail = (el.tail or "").strip()
            if tail:
                emit(tail + " ")
            return

        text = (el.text or "").strip()
        if text:
            emit(text + " ")
        for child in el:
            walk(child)
        tail = (el.tail or "").strip()
        if tail:
            emit(tail + " ")

    walk(body_el)
    return "".join(parts), sections


def _parse_dart_xml(path: Path) -> tuple[str, list[Section]]:
    parser = etree.XMLParser(recover=True, huge_tree=True)
    tree = etree.parse(str(path), parser)
    root = tree.getroot()
    if root is None:
        return "", []
    body = root.find("BODY")
    target = body if body is not None else root
    return _linearize(target)


def _parse_html_filing(path: Path) -> str:
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["style", "script"]):
        tag.decompose()

    table_lines: list[str] = []
    for table in soup.find_all("table"):
        if table.find_parent("table") is not None:
            continue  # nested table's rows are already covered by the outer table
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            row = " | ".join(" ".join(c.get_text(" ", strip=True).split()) for c in cells)
            if row.strip(" |"):
                table_lines.append(row)

    for table in soup.find_all("table"):
        table.decompose()

    remaining = " ".join(soup.get_text(" ", strip=True).split())
    parts = [p for p in [remaining, "\n".join(table_lines)] if p]
    return "\n\n".join(parts)


def parse_file(path: Path) -> ParsedDoc:
    if sniff_format(path) == "html":
        return ParsedDoc(text=_parse_html_filing(path), sections=[])
    text, sections = _parse_dart_xml(path)
    return ParsedDoc(text=text, sections=sections)


def list_sections(path: Path) -> list[str]:
    return [s.title for s in parse_file(path).sections]


def slice_section(doc: ParsedDoc, section_title: str) -> str:
    """Returns the text between the matched section title and the next one.
    Falls back to substring match, then returns the whole document if the
    filing has no sections (e.g. an HTML-format exchange filing)."""
    if not doc.sections:
        return doc.text

    query = section_title.strip()
    matches = [
        i for i, s in enumerate(doc.sections) if query == s.title or query in s.title or s.title in query
    ]
    if not matches:
        lowered = query.lower()
        matches = [i for i, s in enumerate(doc.sections) if lowered in s.title.lower()]
    if not matches:
        # 회사마다 목차 표기가 다르다: 삼성전자 "연결 손익계산서" vs
        # SK하이닉스 "연결 포괄손익계산서". 공백으로 나눈 토큰이 모두 들어간
        # 제목을 찾으면 이런 표기 차이를 흡수한다 (LLM이 채우는 제목이므로
        # 관대해야 한다). 여러 개면 가장 짧은(=가장 특정한) 제목을 고른다.
        tokens = [t for t in query.split() if t]
        if len(tokens) >= 2:
            candidates = [
                i for i, s in enumerate(doc.sections)
                if all(tok in s.title for tok in tokens)
            ]
            if candidates:
                matches = sorted(candidates, key=lambda i: len(doc.sections[i].title))[:1]
    if not matches:
        return ""

    i = matches[0]
    start = doc.sections[i].offset
    end = doc.sections[i + 1].offset if i + 1 < len(doc.sections) else len(doc.text)
    return doc.text[start:end].strip()


def read_section(path: Path, section_title: str) -> str:
    return slice_section(parse_file(path), section_title)


def _document_label(path: Path) -> str:
    """Cheap DOCUMENT-NAME / <title> extraction without a full parse, used
    only to label files for the agent (e.g. "감사보고서")."""
    head = path.read_bytes()[:2000].decode("utf-8", errors="ignore")
    if sniff_format(path) == "html":
        start = head.find("<title>")
        end = head.find("</title>")
        if start != -1 and end != -1:
            return head[start + 7 : end].strip()
        return path.name
    start = head.find("DOCUMENT-NAME")
    if start == -1:
        return path.name
    gt = head.find(">", start)
    lt = head.find("<", gt)
    if gt == -1 or lt == -1:
        return path.name
    return head[gt + 1 : lt].strip() or path.name


def list_documents_in_folder(folder: Path) -> list[FileInfo]:
    """Lists the readable filing documents in a manifest file_path folder.
    Main filing has no "_" in its filename stem; attachments (audit report
    XML, "_viewer.html") do."""
    infos: list[FileInfo] = []
    for p in sorted(folder.iterdir()):
        if p.suffix.lower() not in (".xml", ".html"):
            continue
        infos.append(FileInfo(path=p, is_main="_" not in p.stem, label=_document_label(p)))
    infos.sort(key=lambda fi: (not fi.is_main, fi.path.name))
    return infos
