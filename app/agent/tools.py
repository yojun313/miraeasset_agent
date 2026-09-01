"""Tool schemas (Anthropic tool-use format) and their dispatcher for the
DART filing research agent."""

from __future__ import annotations

from typing import Any

from app.data.cache import (
    FilingNotFoundError,
    UnsupportedFileError,
    get_parsed,
    list_available_files,
    read_section as _read_section,
)
from app.data.fulltext import search_fulltext as _search_fulltext
from app.data.manifest import resolve_company, search_filings as _search_filings

TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_company_info",
        "description": (
            "Resolve a company reference (official DART name, commonly-used market name, "
            "or 6-digit stock code) to its universe info: corp_name (use this for other "
            "tools), sector, industry, market cap. Always call this first when a question "
            "names a company, since the commonly-used name often differs from the DART "
            "corp_name (e.g. '현대차' -> '현대자동차', 'KT' -> '케이티', '엔씨소프트' -> 'NC')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "company name or stock code"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_filings",
        "description": (
            "Search filing metadata by structured filters (company, sector, document type, "
            "period). Returns doc_id + report title + receipt date + base_year for each "
            "match, newest first. Use this to find candidate filings before reading them.\n"
            "IMPORTANT — 회계연도 vs 접수일: 정기공시는 회계기간이 끝난 뒤에 제출된다. "
            "FY2025 사업보고서는 2026년 3월에 접수되므로, '2025년 실적/매출'을 찾을 때 "
            "date_from/date_to(접수일)를 쓰면 FY2024 보고서가 잡혀 틀린 답이 나온다. "
            "특정 회계연도의 실적을 찾을 때는 반드시 base_year를 사용할 것.\n"
            "doc_group values: periodic (사업/반기/분기보고서), major (주요사항보고서 - "
            "이사회 결정사항), exchange (거래소공시 - 공급계약/투자/경영사항), holding "
            "(지분공시 - 5% 대량보유). doc_subtype for periodic: annual/half/quarter."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "corp_name from get_company_info"},
                "sector": {"type": "string"},
                "industry": {"type": "string"},
                "doc_group": {
                    "type": "string",
                    "enum": ["periodic", "major", "exchange", "holding"],
                },
                "doc_subtype": {
                    "type": "string",
                    "enum": [
                        "annual",
                        "half",
                        "quarter",
                        "단일판매공급계약체결",
                        "단일판매공급계약해지",
                        "신규시설투자등",
                        "투자판단관련주요경영사항",
                        "대량보유상황보고서",
                    ],
                    "description": (
                        "정확히 이 값들 중 하나만 사용 (가운뎃점·공백 없음). major 공시는 "
                        "doc_subtype이 없으므로 생략할 것."
                    ),
                },
                "date_from": {
                    "type": "string",
                    "description": "접수일 YYYYMMDD, inclusive. 공시가 '제출된' 날짜 기준 (회계연도가 아님)",
                },
                "date_to": {"type": "string", "description": "접수일 YYYYMMDD, inclusive"},
                "base_year": {
                    "type": "integer",
                    "description": (
                        "정기공시가 다루는 회계연도 (예: 2025년 실적 → base_year=2025). "
                        "연도별 실적·재무 질의에는 date_from/date_to 대신 이것을 쓸 것."
                    ),
                },
                "base_month": {
                    "type": "integer",
                    "description": "보고 기준월. 사업보고서=12, 반기=6, 1분기=3, 3분기=9",
                },
                "is_correction": {
                    "type": "boolean",
                    "description": "true for [기재정정] filings only, false to exclude them",
                },
                "keyword": {
                    "type": "string",
                    "description": "substring match on the report title only, not document content",
                },
                "limit": {"type": "integer", "default": 20},
            },
        },
    },
    {
        "name": "search_fulltext",
        "description": (
            "Full-text keyword search over filing content (not just titles). Use this when "
            "you need to find filings that mention something specific but you don't know "
            "which filing it's in (e.g. a counterparty name, a risk factor, a product name). "
            "Returns matches with a short snippet plus the doc_id/filename needed to read "
            "more via list_sections/read_section."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "keyword or phrase, min 3 characters"},
                "company": {"type": "string", "description": "corp_name from get_company_info"},
                "sector": {"type": "string"},
                "doc_group": {
                    "type": "string",
                    "enum": ["periodic", "major", "exchange", "holding"],
                },
                "date_from": {"type": "string", "description": "YYYYMMDD, inclusive"},
                "date_to": {"type": "string", "description": "YYYYMMDD, inclusive"},
                "limit": {"type": "integer", "default": 15},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_sections",
        "description": (
            "Lists the files belonging to a filing (main document plus any attachments, "
            "e.g. an annual report's audit report) and, for a chosen file, its section "
            "headings in document order. Call this before read_section on any periodic/"
            "major/holding filing (they can be large) to find the right section title. "
            "Exchange filings and other HTML-format filings have no sections; read_section "
            "returns their full text regardless of the title you pass."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "doc_id from search_filings/search_fulltext"},
                "filename": {
                    "type": "string",
                    "description": "specific file within the filing; defaults to the main document",
                },
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "read_section",
        "description": (
            "Reads the text of one section of a filing (as listed by list_sections), with "
            "tables rendered as pipe-delimited rows. If the filing has no sections (HTML-"
            "format exchange filings), returns the full document regardless of section_title."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string", "description": "doc_id from search_filings/search_fulltext"},
                "section_title": {
                    "type": "string",
                    "description": "a title from list_sections (exact or partial match)",
                },
                "filename": {
                    "type": "string",
                    "description": "specific file within the filing; defaults to the main document",
                },
                "offset": {
                    "type": "integer",
                    "description": (
                        "character offset to start reading from; use the next_offset value "
                        "returned by a truncated read to continue. Defaults to 0."
                    ),
                },
            },
            "required": ["doc_id", "section_title"],
        },
    },
]

def openai_tool_specs() -> list[dict[str, Any]]:
    """TOOLS converted to the OpenAI function-calling wire format used by
    CLOVA Studio's compatible endpoint. TOOLS stays the single source of
    truth for names/descriptions/schemas."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


_MAX_RESULT_CHARS = 12_000


def _as_int(value: Any) -> int | None:
    """LLM이 숫자를 문자열("2025")로 채워 넣는 일이 흔하므로 관대하게 해석한다."""
    if value is None or value == "":
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _page(text: str, offset: int = 0) -> dict[str, Any]:
    """Returns one page of a long body plus where to continue.

    Blind truncation loses whatever sits past the cut (in filings that is
    often the totals row). Paging instead lets the agent ask for the rest
    only when it actually needs it, which keeps the common case cheap."""
    total = len(text)
    offset = max(0, min(offset, total))
    window = text[offset : offset + _MAX_RESULT_CHARS]
    result: dict[str, Any] = {"text": window}
    if offset:
        result["offset"] = offset
    if offset + len(window) < total:
        result["truncated"] = True
        result["total_chars"] = total
        result["next_offset"] = offset + len(window)
        result["note"] = (
            f"전체 {total:,}자 중 {offset:,}~{offset + len(window):,}자 구간입니다. "
            f"이어서 읽으려면 offset={offset + len(window)} 으로 다시 호출하세요."
        )
    return result


def dispatch(name: str, tool_input: dict[str, Any]) -> Any:
    if name == "get_company_info":
        matches = resolve_company(tool_input["query"])
        return matches or {"error": f"no company matched {tool_input['query']!r}"}

    if name == "search_filings":
        results = _search_filings(
            company=tool_input.get("company"),
            sector=tool_input.get("sector"),
            industry=tool_input.get("industry"),
            doc_group=tool_input.get("doc_group"),
            doc_subtype=tool_input.get("doc_subtype"),
            date_from=tool_input.get("date_from"),
            date_to=tool_input.get("date_to"),
            base_year=_as_int(tool_input.get("base_year")),
            base_month=_as_int(tool_input.get("base_month")),
            is_correction=tool_input.get("is_correction"),
            keyword=tool_input.get("keyword"),
            limit=tool_input.get("limit", 20),
        )
        if results:
            return results
        return {
            "note": (
                "이 필터 조합에 맞는 공시가 없습니다. 포기하지 말고 조건을 넓혀 다시 시도하세요: "
                "doc_subtype을 빼거나, 기간을 넓히거나, doc_group만으로 검색하거나, "
                "본문 검색(search_fulltext)을 사용해 보세요. "
                "연도로 찾는 중이라면 date_from/date_to(접수일) 대신 base_year(회계연도)를 "
                "쓰고 있는지 확인하세요 — FY2025 사업보고서는 2026년에 접수됩니다."
            )
        }

    if name == "search_fulltext":
        results = _search_fulltext(
            query=tool_input["query"],
            company=tool_input.get("company"),
            sector=tool_input.get("sector"),
            doc_group=tool_input.get("doc_group"),
            date_from=tool_input.get("date_from"),
            date_to=tool_input.get("date_to"),
            limit=tool_input.get("limit", 15),
        )
        return results or {"note": "no matches; try a shorter keyword or different filters"}

    if name == "list_sections":
        doc_id = tool_input["doc_id"]
        try:
            files = list_available_files(doc_id)
        except FilingNotFoundError as e:
            return {"error": str(e)}
        if not files:
            return {"error": "no parseable xml/html file for this doc_id (pdf-only filing not supported)"}

        filename = tool_input.get("filename") or files[0].path.name
        available = [{"filename": f.path.name, "label": f.label, "is_main": f.is_main} for f in files]
        try:
            doc = get_parsed(doc_id, filename)
        except UnsupportedFileError as e:
            return {"error": str(e), "available_files": available}

        result: dict[str, Any] = {"doc_id": doc_id, "filename": filename, "available_files": available}
        if doc.sections:
            result["sections"] = [s.title for s in doc.sections]
        else:
            result["sections"] = []
            result["note"] = "no section headers in this file; read_section returns the full text"
        return result

    if name == "read_section":
        doc_id = tool_input["doc_id"]
        try:
            text = _read_section(doc_id, tool_input["section_title"], tool_input.get("filename"))
        except (FilingNotFoundError, UnsupportedFileError) as e:
            return {"error": str(e)}
        if not text:
            return {"error": "section not found; call list_sections to see available titles"}
        try:
            offset = int(tool_input.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        if offset >= len(text):
            return {
                "doc_id": doc_id,
                "text": "",
                "note": f"offset={offset} 가 본문 길이({len(text):,}자)를 넘었습니다. 더 읽을 내용이 없습니다.",
            }
        result = _page(text, offset)
        result["doc_id"] = doc_id
        return result

    return {"error": f"unknown tool {name!r}"}
