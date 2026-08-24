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
            "date range). Returns doc_id + report title + receipt date for each match, "
            "newest first. Use this to find candidate filings before reading them. "
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
                "doc_subtype": {"type": "string"},
                "date_from": {"type": "string", "description": "YYYYMMDD, inclusive"},
                "date_to": {"type": "string", "description": "YYYYMMDD, inclusive"},
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
            },
            "required": ["doc_id", "section_title"],
        },
    },
]

_MAX_RESULT_CHARS = 12_000


def _truncate(text: str) -> dict[str, Any]:
    if len(text) <= _MAX_RESULT_CHARS:
        return {"text": text}
    return {
        "text": text[:_MAX_RESULT_CHARS],
        "truncated": True,
        "note": f"truncated to {_MAX_RESULT_CHARS} chars; narrow the section or query for more.",
    }


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
            is_correction=tool_input.get("is_correction"),
            keyword=tool_input.get("keyword"),
            limit=tool_input.get("limit", 20),
        )
        return results or {"note": "no filings matched these filters"}

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
        result = _truncate(text)
        result["doc_id"] = doc_id
        return result

    return {"error": f"unknown tool {name!r}"}
