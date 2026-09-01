"""Tool schemas (Anthropic tool-use format) and their dispatcher for the
DART filing research agent."""

from __future__ import annotations

import re
from typing import Any

from app.agent.calc import (
    CalcError,
    calculate as _calculate,
    korean_amount_short,
)
from app.data.cache import (
    FilingNotFoundError,
    UnsupportedFileError,
    get_parsed,
    list_available_files,
)
from app.data.parser import slice_section
from app.data.fulltext import search_fulltext as _search_fulltext
from app.data.manifest import resolve_company, search_filings as _search_filings

TOOLS: list[dict[str, Any]] = [
    # 스키마는 의도적으로 짧게 유지한다. tools JSON이 약 4,400자를 넘으면
    # HCX-007이 도구 이력이 있는 요청에서 400 "Unsupported function"(40009)을
    # 반환한다 (실측: 4,399자 3/3 성공 / 4,577자 0/3). 자세한 사용 지침은
    # 시스템 프롬프트에 두고, 여기에는 필수 정보만 적는다.
    {
        "name": "get_company_info",
        "description": "기업명·종목코드를 DART 정식 법인명(corp_name)으로 변환. 기업이 언급되면 항상 먼저 호출.",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "기업명 또는 6자리 종목코드"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_filings",
        "description": (
            "공시 메타데이터 검색. 회계연도로 찾을 때는 base_year, "
            "공시된 날짜로 찾을 때는 date_from/date_to를 쓴다."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "company": {"type": "string", "description": "corp_name"},
                "sector": {"type": "string"},
                "industry": {"type": "string"},
                "doc_group": {
                    "type": "string",
                    "enum": ["periodic", "major", "exchange", "holding"],
                },
                "doc_subtype": {
                    "type": "string",
                    "enum": [
                        "annual", "half", "quarter",
                        "단일판매공급계약체결", "단일판매공급계약해지",
                        "신규시설투자등", "투자판단관련주요경영사항",
                        "대량보유상황보고서",
                    ],
                },
                "base_year": {"type": "integer", "description": "회계연도 (2025년 실적 → 2025)"},
                "base_month": {"type": "integer", "description": "12=사업 6=반기 3/9=분기"},
                "date_from": {"type": "string", "description": "접수일 YYYYMMDD"},
                "date_to": {"type": "string", "description": "접수일 YYYYMMDD"},
                "is_correction": {"type": "boolean", "description": "[기재정정] 여부"},
                "keyword": {"type": "string", "description": "보고서명 부분일치"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "search_fulltext",
        "description": "공시 본문 키워드 검색. 어느 문서인지 모를 때 사용 (계약상대·제품명 등).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "3자 이상 키워드"},
                "company": {"type": "string", "description": "corp_name"},
                "sector": {"type": "string"},
                "doc_group": {
                    "type": "string",
                    "enum": ["periodic", "major", "exchange", "holding"],
                },
                "date_from": {"type": "string", "description": "접수일 YYYYMMDD"},
                "date_to": {"type": "string", "description": "접수일 YYYYMMDD"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_sections",
        "description": "문서의 파일 목록과 목차를 반환. 정기·주요사항·지분 공시는 읽기 전 반드시 호출.",
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "filename": {"type": "string", "description": "생략하면 본문"},
            },
            "required": ["doc_id"],
        },
    },
    {
        "name": "read_section",
        "description": (
            "공시 본문을 읽는다. 섹션이 없는 문서(거래소공시)는 전문을 반환. "
            "큰 금액 뒤 괄호는 억/조 환산값이니 그대로 쓸 것."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "doc_id": {"type": "string"},
                "section_title": {"type": "string", "description": "list_sections의 제목"},
                "filename": {"type": "string", "description": "생략하면 본문"},
                "offset": {"type": "integer", "description": "이어 읽기 시작 위치"},
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


_SUBTYPES_BY_GROUP: dict[str, tuple[str, ...]] = {
    "periodic": ("annual", "half", "quarter"),
    "exchange": (
        "단일판매공급계약체결",
        "단일판매공급계약해지",
        "신규시설투자등",
        "투자판단관련주요경영사항",
    ),
    "holding": ("대량보유상황보고서",),
    "major": (),  # 주요사항보고서는 세부 유형이 없다
}

_MAX_RESULT_CHARS = 12_000


# 1,000,000 이상 자리구분 쉼표가 있는 숫자
_AMOUNT_RE = re.compile(r"(?<![\d,.])(\d{1,3}(?:,\d{3}){2,})(?![\d,.])")
# 재무제표는 표마다 "(단위 : 백만원)" 처럼 배율을 따로 선언하고, 한 섹션 안에서
# 원/백만원이 섞이기도 한다. 이걸 무시하면 32,725,961(백만원)을 3,272만원으로
# 보게 되고, 모델이 스스로 환산하다 "327조"처럼 10배 틀린다.
_UNIT_RE = re.compile(r"단\s*위\s*[:：]?\s*\(?\s*(십억원|백만원|천만원|억원|천원|만원|원)")
_UNIT_SCALES = {
    "원": 1,
    "천원": 10**3,
    "만원": 10**4,
    "백만원": 10**6,
    "천만원": 10**7,
    "억원": 10**8,
    "십억원": 10**9,
}
_AMOUNT_MIN = 100_000_000  # 환산 후 1억 미만은 표기가 무의미
_AMOUNT_MAX_ANNOTATIONS = 250


def _scale_at(marks: list[tuple[int, int]], pos: int) -> int:
    """pos 직전에 선언된 단위 배율. 선언이 없으면 원(1)."""
    scale = 1
    for start, value in marks:
        if start > pos:
            break
        scale = value
    return scale


def annotate_amounts(text: str) -> tuple[str, int]:
    """큰 금액 **바로 뒤에** 한국어 단위 환산을 괄호로 붙인다.

    모델에게 산술 도구를 주면 function calling 자체가 불안정해지므로
    (HCX-007이 40009 "Unsupported function"을 반환) 환산을 미리 실어 보낸다.
    별도 목록으로 주면 숫자가 많은 재무제표에서 정작 필요한 값이 빠지므로
    숫자 옆에 직접 붙이고, 표마다 선언된 단위(백만원 등)를 반영한다."""
    marks = [
        (m.start(), _UNIT_SCALES[m.group(1)])
        for m in _UNIT_RE.finditer(text)
        if m.group(1) in _UNIT_SCALES
    ]
    count = 0

    def repl(m: "re.Match[str]") -> str:
        nonlocal count
        raw = m.group(1)
        if count >= _AMOUNT_MAX_ANNOTATIONS:
            return raw
        value = int(raw.replace(",", "")) * _scale_at(marks, m.start())
        if value < _AMOUNT_MIN:
            return raw
        count += 1
        return f"{raw}({korean_amount_short(value)})"

    return _AMOUNT_RE.sub(repl, text), count


def _as_str(tool_input: dict[str, Any], key: str) -> str:
    """LLM이 채우는 인자는 누락·None·숫자 등 무엇이든 올 수 있다.
    KeyError로 터지면 턴만 낭비하므로 항상 문자열로 정규화한다."""
    value = tool_input.get(key)
    if value is None:
        return ""
    return str(value).strip()


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
        query = _as_str(tool_input, "query")
        if not query:
            return {"error": "query가 비어 있습니다. 조회할 기업명이나 6자리 종목코드를 넣으세요."}
        matches = resolve_company(query)
        return matches or {
            "error": (
                f"{query!r} 와 일치하는 기업이 없습니다. 제공된 70개사 유니버스에 "
                "없는 기업일 수 있습니다 (그럴 경우 답변에서 범위 밖임을 알리세요)."
            )
        }

    if name == "search_filings":
        group = tool_input.get("doc_group")
        subtype = tool_input.get("doc_subtype")
        # 모델이 exchange 전용 subtype을 major에 붙여 호출해 0건을 받고 포기하는
        # 일이 있었다. 조용히 빈 결과를 주는 대신 왜 틀렸는지 알려준다.
        if group and subtype and subtype not in _SUBTYPES_BY_GROUP.get(group, ()):
            valid = _SUBTYPES_BY_GROUP.get(group, ())
            return {
                "error": (
                    f"doc_group='{group}' 에는 doc_subtype='{subtype}' 이 존재하지 않습니다. "
                    + (
                        f"'{group}' 에서 쓸 수 있는 값: {list(valid)}"
                        if valid
                        else f"'{group}' 공시에는 doc_subtype이 없으므로 생략하세요."
                    )
                    + " 올바른 값으로 다시 호출하세요."
                )
            }
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
        fts_query = _as_str(tool_input, "query")
        if len(fts_query) < 3:
            return {"error": "query는 3자 이상이어야 합니다 (trigram 색인 제약)."}
        results = _search_fulltext(
            query=fts_query,
            company=tool_input.get("company"),
            sector=tool_input.get("sector"),
            doc_group=tool_input.get("doc_group"),
            date_from=tool_input.get("date_from"),
            date_to=tool_input.get("date_to"),
            limit=tool_input.get("limit", 15),
        )
        return results or {"note": "no matches; try a shorter keyword or different filters"}

    if name == "list_sections":
        doc_id = _as_str(tool_input, "doc_id")
        if not doc_id:
            return {"error": "doc_id가 비어 있습니다. search_filings 결과의 doc_id를 넣으세요."}
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
        doc_id = _as_str(tool_input, "doc_id")
        if not doc_id:
            return {"error": "doc_id가 비어 있습니다. search_filings 결과의 doc_id를 넣으세요."}
        section_title = _as_str(tool_input, "section_title")
        try:
            doc = get_parsed(doc_id, tool_input.get("filename"))
        except (FilingNotFoundError, UnsupportedFileError) as e:
            return {"error": str(e)}

        # section_title은 required지만 모델이 자주 빠뜨린다. 섹션이 없는 문서
        # (거래소공시)는 어차피 전문을 반환하므로 빈 값이어도 문제없지만,
        # 섹션이 있는 문서에서 빈 값은 첫 섹션에 우연히 매칭돼 엉뚱한 내용을
        # 주게 되므로 목차를 돌려주며 재시도를 유도한다.
        if doc.sections and not section_title:
            return {
                "error": "section_title 이 비어 있습니다.",
                "available_sections": [sec.title for sec in doc.sections][:60],
                "hint": "위 목록에서 읽을 섹션 제목을 골라 다시 호출하세요.",
            }
        text = slice_section(doc, section_title)
        if not text:
            return {
                "error": f"섹션 {section_title!r} 을(를) 찾지 못했습니다.",
                "available_sections": [sec.title for sec in doc.sections][:60],
                "hint": "위 목록에서 정확한 제목을 골라 다시 호출하세요.",
            }
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
        annotated, n = annotate_amounts(result.get("text", ""))
        if n:
            result["text"] = annotated
            result["note_amount"] = (
                "숫자 뒤 괄호는 표에 선언된 단위(원·백만원 등)를 반영해 계산한 "
                "실제 금액입니다. 억/조 단위로 답할 때는 직접 환산하지 말고 이 값을 "
                "그대로 쓰세요."
            )
        return result

    if name == "calculate":
        try:
            return _calculate(str(tool_input.get("expression", "")))
        except CalcError as e:
            return {"error": f"{e}"}

    return {"error": f"unknown tool {name!r}"}
