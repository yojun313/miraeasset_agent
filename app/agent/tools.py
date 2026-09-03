"""Tool schemas (Anthropic tool-use format) and their dispatcher for the
DART filing research agent."""

from __future__ import annotations

import datetime
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
from app.data.manifest import (
    aggregate_filings as _aggregate_filings,
    get_filing,
    resolve_company,
    resolve_sector,
    search_filings as _search_filings,
)

TOOLS: list[dict[str, Any]] = [
    # 스키마는 의도적으로 짧게 유지한다. tools JSON이 약 4,400자를 넘으면
    # HCX-007이 도구 이력이 있는 요청에서 400 "Unsupported function"(40009)을
    # 반환한다 (실측: 4,399자 3/3 성공 / 4,577자 0/3). 자세한 사용 지침은
    # 시스템 프롬프트에 두고, 여기에는 필수 정보만 적는다.
    {
        "name": "get_company_info",
        "description": "기업명·종목코드를 정식 corp_name으로 변환. 섹터명(예: 방산·항공우주)을 넣으면 소속 기업 목록을 반환. 기업/섹터가 언급되면 먼저 호출.",
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
                "company": {"type": "string", "description": "corp_name (유니버스 70개사의 발행회사)"},
                "filer": {
                    "type": "string",
                    "description": "제출인(보고자). 'A가 B 지분을 보고' 질문은 company=B, filer=A",
                },
                "sector": {"type": "string", "description": "부분일치 (예: '방산')"},
                "industry": {"type": "string"},
                "market": {"type": "string", "enum": ["KOSPI", "KOSDAQ"]},
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
                "report_type": {
                    "type": "string",
                    "description": (
                        "주요사항보고서 세부 유형 부분일치 (예: 전환사채권발행, 유상증자, "
                        "자기주식취득, 회사합병). 발행/매도/행사 등이 섞이지 않게 정확히 거른다"
                    ),
                },
                "base_year": {"type": "integer", "description": "회계연도 (2025년 실적 → 2025)"},
                "base_month": {"type": "integer", "description": "12=사업 6=반기 3/9=분기"},
                "date_from": {"type": "string", "description": "접수일 YYYYMMDD"},
                "date_to": {"type": "string", "description": "접수일 YYYYMMDD"},
                "is_correction": {"type": "boolean", "description": "[기재정정] 여부"},
                "keyword": {"type": "string", "description": "보고서명 부분일치"},
                "order": {
                    "type": "string",
                    "enum": ["desc", "asc"],
                    "description": "asc=오래된 순 ('최초/가장 이른' 질문에 사용). 기본 desc",
                },
                "group_by": {
                    "type": "string",
                    "enum": ["corp_name", "flr_nm", "doc_subtype", "report_type", "doc_group", "sector", "year"],
                    "description": (
                        "지정하면 목록 대신 집계를 반환 (건수·정정건수·정정비율·기간). "
                        "'가장 많은/높은 기업', '몇 곳', '보고자별' 같은 통계 질문에 사용"
                    ),
                },
                "min_count": {"type": "integer", "description": "group_by 시 최소 건수 (비율 비교에 유용)"},
                "min_amount": {"type": "integer", "description": "계약·투자금액 하한(원). 예: 1조 이상 → 1000000000000"},
                "min_ratio": {"type": "number", "description": "지분공시 보유비율 하한(%). 예: 13% 이상 → 13"},
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
        "name": "calculate",
        "description": "산술 계산기. 합계·차이·비율은 암산하지 말고 이걸 쓸 것",
        "input_schema": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "예: (1200+3400)/2"},
            },
            "required": ["expression"],
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
                "keyword": {"type": "string", "description": "긴 표에서 특정 회사·항목을 찾을 때 — 그 위치로 바로 이동"},
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
_MAJOR_ENRICH_MAX = 30  # 주요사항보고서 검색 시 핵심 필드를 뽑아줄 최대 건수
_HOLDING_ENRICH_MAX = 60  # 지분공시 검색 시 비율을 함께 뽑아줄 최대 건수
_EXCHANGE_ENRICH_MAX = 80  # 거래소공시 검색 시 계약금액을 함께 뽑아줄 최대 건수
_RATIO_TREND_MAX = 150  # 비율 추세 계산 시 훑을 최대 공시 수 (전 기간 포함용)
_AUTO_WIDEN_LIMIT = 80  # limit 미지정 결과가 잘렸을 때 자동으로 넓히는 상한
_RATIO_MIN_SAMPLE = 10  # 비율 순위에 넣을 최소 표본 (1~2건짜리 100% 배제)


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


_HOLDING_FIELDS = ("보고구분", "보고사유", "보유주식등의 수 및 보유비율", "직전 보고서", "이번 보고서")


_CONTRACT_PATTERNS = (
    ("계약금액", re.compile(r"계약금액\(원\)\s*\|\s*([\d,]+)")),
    ("해지금액", re.compile(r"해지금액\(원\)\s*\|\s*([\d,]+)")),
    ("매출액대비", re.compile(r"매출액대비\(%\)\s*\|\s*([\d.]+)")),
    ("투자금액", re.compile(r"투자금액\(원\)\s*\|\s*([\d,]+)")),
)
_CONTRACT_NAME_RE = re.compile(r"(?:체결|해지)계약명\s*\|\s*([^\n|]{1,60})")
_CONTRACT_TEXT_PATTERNS = (
    ("해지사유", re.compile(r"해지\s*주요\s*사유\s*\|\s*([^\n|]{1,80})")),
    ("해지일자", re.compile(r"해지일자\s*\|\s*([^\n|]{1,20})")),
    ("계약상대", re.compile(r"계약상대\s*\|\s*([^\n|]{1,60})")),
    ("계약기간_시작", re.compile(r"계약기간\s*\|\s*시작일\s*\|\s*([^\n|]{1,20})")),
    ("계약기간_종료", re.compile(r"계약기간\s*\|\s*시작일\s*\|[^\n]*\n종료일\s*\|\s*([^\n|]{1,20})")),
)


def _duration_between(start: str, end: str) -> str | None:
    """YYYY-MM-DD 두 날짜 사이 기간을 "X년 Y개월"로. 파싱 실패 시 None."""
    try:
        s_ = datetime.date(*map(int, start.split("-")))
        e_ = datetime.date(*map(int, end.split("-")))
    except (ValueError, AttributeError):
        return None
    if e_ < s_:
        return None
    months = (e_.year - s_.year) * 12 + (e_.month - s_.month) - (1 if e_.day < s_.day else 0)
    days = (e_ - s_).days
    y, m = divmod(months, 12)
    label = (f"{y}년 " if y else "") + f"{m}개월"
    return f"약 {label.strip()} ({days}일)"


def _contract_fields(body: str) -> dict[str, str]:
    """거래소 계약공시 본문에서 금액·계약명을 뽑는다 (표 형태가 정형적이다)."""
    out: dict[str, str] = {}
    name = _CONTRACT_NAME_RE.search(body)
    if name:
        out["계약명"] = name.group(1).strip()
    for label, pattern in _CONTRACT_TEXT_PATTERNS:
        m = pattern.search(body)
        if m:
            out[label] = m.group(1).strip()
    for label, pattern in _CONTRACT_PATTERNS:
        m = pattern.search(body)
        if m:
            raw = m.group(1)
            if label == "매출액대비":
                out[label] = f"{raw}%"
            else:
                try:
                    out[label] = f"{raw} ({korean_amount_short(int(raw.replace(',', '')))})"
                except ValueError:
                    out[label] = raw
    # 해지 공시라면 체결(계약 시작)부터 해지까지의 유지 기간을 코드로 계산한다
    # — 4건의 기간을 비교하랬더니 1년 6개월보다 2년 1개월이 짧다고 결론낸
    # 실제 사례(J-01). 날짜 차이도 계산이므로 모델에 맡기지 않는다.
    if out.get("해지일자") and out.get("계약기간_시작"):
        dur = _duration_between(out["계약기간_시작"], out["해지일자"])
        if dur:
            out["체결후_유지기간"] = dur + " — 비교는 일수 기준으로"
        # "같은 해 체결·해지 존재?" 질문에서 다른 연도 증거를 모아놓고 존재한다고
        # 결론낸 논리 반전(V3-15) — 판정 자체를 코드가 한다
        s_, e_ = out["계약기간_시작"][:4], out["해지일자"][:4]
        if s_.isdigit() and e_.isdigit():
            out["체결·해지_같은해"] = "예" if s_ == e_ else f"아니오 ({s_}년 체결, {e_}년 해지)"
    # 계약기간 길이도 코드로 — "가장 긴 계약" 비교에서 종료일을 못 보고
    # 시작일만으로 동일하다고 결론낸 실제 사례(V3-14)
    if out.get("계약기간_시작") and out.get("계약기간_종료"):
        dur = _duration_between(out["계약기간_시작"], out["계약기간_종료"])
        if dur:
            out["계약기간_길이"] = dur
    return out


_NUM_RE = re.compile(r"^([\d,]+)")


def _diagnose_empty(**filters: Any) -> str | None:
    """0건 검색에서 필터를 하나씩 완화해 보고, 결과가 생기는 완화를 알려준다."""
    # base_year는 정기보고서 회계연도 필터라 사건 공시엔 항상 0건이다.
    # 접수일 범위로 바꿔 보고, 되면 그 변환을 정확히 안내한다 (막연한
    # "조건을 빼면"보다 다음 호출을 확실히 유도한다).
    base_year = filters.get("base_year")
    if base_year and filters.get("doc_group") != "periodic":
        converted = {k: v for k, v in filters.items() if k != "base_year"}
        converted["date_from"] = f"{base_year}0101"
        converted["date_to"] = f"{base_year}1231"
        try:
            rows = _search_filings(**converted, limit=200)
        except Exception:  # noqa: BLE001
            rows = []
        if rows:
            return (
                f"base_year는 정기보고서 회계연도 필터입니다. 접수일 기준이면 "
                f"date_from={base_year}0101, date_to={base_year}1231 로 바꾸세요 — "
                f"그러면 {len(rows)}건이 있습니다."
            )
    relaxations = (
        ("base_year", ("base_year",), "base_year를 빼면"),
        ("date", ("date_from", "date_to"), "접수일 기간 조건을 빼면"),
        ("subtype", ("doc_subtype",), "doc_subtype을 빼면"),
        ("report_type", ("report_type",), "report_type을 빼면"),
        ("keyword", ("keyword",),
         "keyword는 보고서명만 검색합니다(사업·현장 이름은 본문에만 있어 안 잡힙니다) — "
         "search_fulltext로 본문을 찾거나 keyword를 빼면"),
        ("correction", ("is_correction",), "is_correction을 빼면"),
    )
    for _name, drop, label in relaxations:
        if not any(filters.get(k) is not None for k in drop):
            continue
        relaxed = {k: (None if k in drop else v) for k, v in filters.items()}
        try:
            rows = _search_filings(**relaxed, limit=200)
        except Exception:  # noqa: BLE001
            continue
        if rows:
            latest = rows[0].get("rcept_dt", "")
            latest_fmt = f"{latest[:4]}-{latest[4:6]}-{latest[6:]}" if len(latest) == 8 else latest
            return (
                f"{label} {len(rows)}건이 있습니다 (최신 접수일 {latest_fmt}). "
                "그 조건을 제거하고 다시 호출하세요."
            )
    return None


def _amount_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """계약금액이 뽑힌 행들의 연도별·전체 합계와 최대 건을 계산한다."""
    by_year: dict[str, int] = {}
    counts: dict[str, int] = {}
    total = 0
    biggest: tuple[int, dict[str, Any]] | None = None
    for r in rows:
        raw = str(r.get("계약금액") or r.get("투자금액") or "")
        m = _NUM_RE.match(raw)
        if not m:
            continue
        try:
            value = int(m.group(1).replace(",", ""))
        except ValueError:
            continue
        year = str(r.get("rcept_dt") or "")[:4]
        by_year[year] = by_year.get(year, 0) + value
        counts[year] = counts.get(year, 0) + 1
        total += value
        if biggest is None or value > biggest[0]:
            biggest = (value, r)
    if not by_year:
        return {}
    # "가장 큰 계약/투자" 비교를 모델이 13자리 수끼리 눈으로 하다 틀린 실제
    # 사례(V3-10: 4조를 두고 8천억을 최대로 선택) — 최대 건은 코드가 뽑는다
    max_info = {}
    if biggest:
        v, r = biggest
        max_info = {
            "최대_건": {
                "corp_name": r.get("corp_name"), "doc_id": r.get("doc_id"),
                "금액": f"{v:,} ({korean_amount_short(v)})",
                # 계약명·상대를 다른 행에서 빌려와 섞은 실제 사례(V4-07) 방지
                **{k: r[k] for k in ("계약명", "계약상대", "rcept_date", "rcept_dt")
                   if r.get(k)},
            }
        }
    return {
        "by_year": [
            {
                "year": y,
                "count": counts[y],
                "total": f"{by_year[y]:,}",
                "total_korean": korean_amount_short(by_year[y]),
            }
            for y in sorted(by_year)
        ],
        "grand_total": f"{total:,}",
        "grand_total_korean": korean_amount_short(total),
        **max_info,
    }


_RATIO_RE = re.compile(r"([\d,]+)\s*\|\s*([\d.]+)\s*$")


def _ratio_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """지분공시 시계열에서 보유비율 추세를 뽑는다 (오래된 순 입력 가정)."""
    series: list[dict[str, Any]] = []
    for r in rows:
        try:
            parsed = get_parsed(r["doc_id"])
        except Exception:  # noqa: BLE001
            continue
        head = parsed.text[: parsed.sections[0].offset] if parsed.sections else parsed.text[:1500]
        hs = _holding_summary(head)
        m = _RATIO_RE.search(hs.get("이번 보고서(주식수 | 비율)", ""))
        if not m:
            continue
        series.append({
            "date": r.get("rcept_date") or r.get("rcept_dt"),
            "filer": r.get("flr_nm"),
            "ratio": float(m.group(2)),
            "shares": m.group(1),
        })
    if not series:
        return {}
    ratios = [p["ratio"] for p in series]
    first, last = series[0], series[-1]
    return {
        "points": len(series),
        "first": {"date": first["date"], "ratio": first["ratio"]},
        "last": {"date": last["date"], "ratio": last["ratio"]},
        "min_ratio": min(ratios),
        "max_ratio": max(ratios),
        "change": round(last["ratio"] - first["ratio"], 2),
        "series": series[-15:],
    }


# 주요사항보고서(유상증자·CB 등)의 핵심 필드. 정정 이력 질문은 "11개의 거의
# 동일한 8천 자 문서에서 무엇이 바뀌었나"인데, 이 diff를 모델에게 시키면
# 항목을 빠뜨리고 변경 시점을 섞는다(실측: 11건 중 5건만 답함). 코드가 뽑고
# 코드가 비교한다.
_MAJOR_FIELD_PATTERNS = (
    ("신주수", re.compile(r"신주의 종류와 수 \| 보통주식 \(주\) \| ([\d,]+)"), False),
    ("신주배정비율(1주당)", re.compile(r"1주당 신주배정 주식수 \| 보통주식 \(주\) \| ([\d.]+)"), False),
    ("증자전주식총수", re.compile(r"증자전 발행주식총수 \(주\) \| 보통주식 \(주\) \| ([\d,]+)"), False),
    ("시설자금", re.compile(r"시설자금 \(원\) \| ([\d,]+)"), True),
    ("타법인취득자금", re.compile(r"타법인 증권취득자금 \(원\) \| ([\d,]+)"), True),
    ("운영자금", re.compile(r"운영자금 \(원\) \| ([\d,]+)"), True),
    ("채무상환자금", re.compile(r"채무상환자금 \(원\) \| ([\d,]+)"), True),
    ("예정발행가", re.compile(r"예정발행가 \| 보통주식 \(원\) \| ([\d,]+)"), False),
    ("사채총액", re.compile(r"사채의 권면\(전자등록\)총액 \(원\) \| ([\d,]+)"), True),
    ("전환가액", re.compile(r"전환가액 \(원/주\) \| ([\d,]+)"), False),
    ("표면이자율(%)", re.compile(r"표면이자율 \(%\) \| ([\d.]+)"), False),
    ("사채만기일", re.compile(r"사채만기일 \| (\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"), False),
    ("이사회결의일", re.compile(r"이사회결의일\(결정일\) \| (\d{4}년\s*\d{1,2}월\s*\d{1,2}일)"), False),
)


def _major_fields(body: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for label, pattern, is_amount in _MAJOR_FIELD_PATTERNS:
        m = pattern.search(body)
        if not m:
            continue
        raw = m.group(1).strip()
        if is_amount:
            try:
                value = int(raw.replace(",", ""))
                out[label] = f"{raw} ({korean_amount_short(value)})"
            except ValueError:
                out[label] = raw
        else:
            out[label] = raw
    # 증자비율은 결정론적 파생값이다 — 모델이 나눗셈을 하다 틀리거나(실측 사례)
    # "증자 전 총수를 모른다"며 포기하지 않도록 코드가 계산해 붙인다.
    try:
        new_shares = int(out["신주수"].replace(",", ""))
        pre_shares = int(out["증자전주식총수"].replace(",", ""))
        if pre_shares > 0:
            out["증자비율"] = (
                f"{new_shares / pre_shares * 100:.2f}% "
                f"(신주 {out['신주수']} ÷ 증자전 {out['증자전주식총수']}, 코드 계산)"
            )
    except (KeyError, ValueError):
        pass
    return out


_CORRECTION_ENRICH_MAX = 40  # 정정 공시에 원본 연결 정보를 붙일 최대 건수
_ORIGIN_DATE_RE = re.compile(
    r"(?:정정관련 공시서류제출일|정정대상 공시서류의 최초제출일)\s*:?\s*\|\s*"
    r"(\d{4})[-.년]\s*(\d{1,2})[-.월]\s*(\d{1,2})"
)
_CORRECTION_REASON_RE = re.compile(r"\d\.\s*정정사유\s*\|\s*([^\n|]{2,100})")


def _link_corrections_to_originals(results: list[dict[str, Any]]) -> None:
    """정정([기재정정]) 행에 최초 공시 정보를 코드로 붙인다.

    정정본만 검색된 경우(기간 필터로 원본이 밖에 있을 때) 모델이 "최초 결정일은
    확인할 수 없다"고 답한 실제 오답 대응 — 정정 서식 머리에는 원본 제출일이
    항상 명시돼 있으므로 그걸 뽑고, 코퍼스에 원본이 있으면 doc_id까지 연결한다."""
    corrections = [r for r in results if r.get("is_correction")]
    if not corrections or len(results) > _CORRECTION_ENRICH_MAX:
        return
    for r in corrections:
        try:
            head = get_parsed(r["doc_id"]).text[:3000]
        except Exception:  # noqa: BLE001 - 못 읽는 문서는 건너뛴다
            continue
        rm = _CORRECTION_REASON_RE.search(head)
        if rm and "정정사유" not in r:
            r["정정사유"] = rm.group(1).strip()[:120]
        # 정정신고 머리의 정정사항 표(정정전/정정후)는 무엇이 바뀌었는지에 대한
        # 공식 기록이다 — 본문 전체를 비교하지 않아도 대조할 수 있게 발췌한다.
        hm = re.search(r"\d\.\s*정정사항\s*\n?(.{20,400})", head, re.DOTALL)
        if hm:
            excerpt = " ".join(hm.group(1).split())
            r["정정사항_발췌"] = excerpt[:400]
        dm = _ORIGIN_DATE_RE.search(head)
        if not dm:
            continue
        origin_date = f"{dm.group(1)}-{int(dm.group(2)):02d}-{int(dm.group(3)):02d}"
        r["최초공시일"] = origin_date
        row_group = str(r.get("doc_id", "")).split("_")[0]
        try:
            originals = _search_filings(
                company=r.get("corp_name"),
                doc_group=row_group or None,
                doc_subtype=r.get("doc_subtype") if row_group == "exchange" else None,
                report_type=r.get("report_type") if row_group == "major" else None,
                date_from=origin_date.replace("-", ""),
                date_to=origin_date.replace("-", ""),
                is_correction=False,
                limit=2,
            )
        except Exception:  # noqa: BLE001
            originals = []
        if originals:
            r["최초공시"] = f"{originals[0]['doc_id']} (접수일 {origin_date})"


# 타법인출자 현황(상세)은 DART 표준 서식이다: 회사명 뒤 15개 열의 순서가 고정.
# 중첩 헤더(기초/증가/기말 각 수량·지분율·장부가액) 탓에 모델이 열을 오독한
# 실제 사례(수량 510,810을 장부가액으로 답변) — 행을 코드로 라벨링해 준다.
_EQUITY_ROW_LABELS = (
    "상장여부", "최초취득일자", "출자목적", "최초취득금액",
    "기초 수량", "기초 지분율(%)", "기초 장부가액",
    "증감 취득(처분) 수량", "증감 취득(처분) 금액", "증감 평가손익",
    "기말 수량", "기말 지분율(%)", "기말 장부가액",
    "최근사업연도 총자산", "최근사업연도 당기순손익",
)


def _equity_row_parse(window: str, keyword: str) -> dict[str, str] | None:
    i = window.find(keyword)
    if i < 0:
        return None
    end = window.find("\n", i)
    row = window[i:end if end > 0 else len(window)]
    fields = [f.strip() for f in row.split("|")]
    if len(fields) != len(_EQUITY_ROW_LABELS) + 1:
        return None
    if fields[1] not in ("상장", "비상장", "코스닥상장", "유가증권상장", "해외상장"):
        return None
    return dict(zip(_EQUITY_ROW_LABELS, fields[1:]))


# 정기보고서 요약재무정보의 핵심 3항목을 코드로 추출한다. 다사·다년 실적
# 취합에서 모델이 자본총계·총영업이익 등 이웃 행을 잘못 집은 실제 사례(H-03)
# 대응 — 행 시작 앵커로 정확한 행만 잡고, 표의 단위 선언을 반영한다.
_FIN_PATTERNS = (
    ("매출액", re.compile(r"(?m)^\[?(?:매출액|영업수익)(?:\(손실\))?\]?\s*\|\s*(-?[\d,]+)")),
    ("영업이익", re.compile(r"(?m)^(?:[IVX]+\.\s*)?\[?영업이익(?:\(손실\))?\]?\s*\|\s*(-?[\d,]+)")),
    # 전체 순이익 우선, 없으면 지배지분 순이익 (라벨 변형: [당기연결순이익],
    # 연결당기순이익, 지배기업주주지분순이익, 지배기업소유주지분순이익 등)
    ("당기순이익", re.compile(r"(?m)^(?:[IVX]+\.\s*)?\[?(?:연결)?당기(?:연결)?순이익(?:\(손실\))?\]?\s*\|\s*(-?[\d,]+)")),
    ("당기순이익(지배지분)", re.compile(
        r"(?m)^\[?지배(?:주주지분|기업주주지분|기업소유주지분)(?:당기)?순이익\]?\s*\|\s*(-?[\d,]+)")),
    # 표가 파이프 없이 한 줄로 눕는 문서(신한지주) — 로마숫자 머리 인라인 폴백
    ("영업이익", re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧ]\.\s*영업이익\s*(-?[\d,]+)")),
    ("당기순이익", re.compile(r"[ⅠⅡⅢⅣⅤⅥⅦⅧ]\.\s*당기순이익\s*(-?[\d,]+)")),
)


def _periodic_financials(doc_id: str) -> tuple[dict[str, str], dict[str, int]]:
    """요약재무정보 3항목의 (표시 문자열, 원화 정수) 쌍. 합산은 정수로 한다."""
    try:
        doc = get_parsed(doc_id)
        body = slice_section(doc, "1. 요약재무정보")
        if not body:
            # ATOC가 성긴 문서(신한지주 사업보고서: 섹션 4개뿐) — 본문에서
            # 요약재무정보 위치를 직접 찾아 그 구간을 쓴다
            i = doc.text.find("요약재무정보")
            if i >= 0:
                body = doc.text[i:i + 10_000]
    except Exception:  # noqa: BLE001
        return {}, {}
    if not body:
        return {}, {}
    marks = [
        (m.start(), _UNIT_SCALES[m.group(1)])
        for m in _UNIT_RE.finditer(body)
        if m.group(1) in _UNIT_SCALES
    ]
    out: dict[str, str] = {}
    values: dict[str, int] = {}
    for label, pattern in _FIN_PATTERNS:
        if label in out:
            continue  # 같은 라벨의 폴백 패턴은 1차가 실패했을 때만
        m = pattern.search(body)
        if not m:
            continue
        raw = m.group(1)
        try:
            value = int(raw.replace(",", ""))
        except ValueError:
            continue
        scale = _scale_at(marks, m.start()) if marks else 1
        sign = "-" if value < 0 else ""
        won = value * scale
        # 표시를 원 단위로 정규화한다 — 문서마다 단위(원/천원/백만원)가 달라
        # 원시 표값끼리 나눠 136,469% 증가율을 낸 실제 사례(V4-06)
        out[label] = f"{sign}{abs(won):,}원 ({sign}{korean_amount_short(abs(won))})"
        values[label] = won
    if "당기순이익" in out and "당기순이익(지배지분)" in out:
        del out["당기순이익(지배지분)"]  # 전체 값이 있으면 지배지분은 소음이다
        del values["당기순이익(지배지분)"]
    # 별도(개별) 재무제표 — 요약재무정보에 연결 표가 먼저 나와 "별도 기준"을
    # 물어도 연결 값으로 답한 실제 사례(V4-05). 별도 표 구간에서 3항목을 따로 뽑는다.
    sep = re.search(r"(?:요약\s*)?별도\s*재무\s*(?:정보|제표)|별도\s*재무상태표|개별\s*재무", body)
    if sep:
        sep_body = body[sep.start():]
        sep_marks = [
            (m.start(), _UNIT_SCALES[m.group(1)])
            for m in _UNIT_RE.finditer(sep_body) if m.group(1) in _UNIT_SCALES
        ]
        for label, pattern in _FIN_PATTERNS:
            key = f"별도_{label}"
            if label not in ("매출액", "영업이익", "당기순이익") or key in out:
                continue
            m = pattern.search(sep_body)
            if not m:
                continue
            try:
                value = int(m.group(1).replace(",", ""))
            except ValueError:
                continue
            scale = _scale_at(sep_marks, m.start()) if sep_marks else (
                _scale_at(marks, sep.start() + m.start()) if marks else 1)
            won = value * scale
            sign = "-" if won < 0 else ""
            out[key] = f"{sign}{abs(won):,}원 ({sign}{korean_amount_short(abs(won))})"
            values[key] = won
    # 배당 2항목 — 배당 섹션을 두 번이나 읽고도 값을 못 옮긴 실제 사례(V3-12)
    try:
        dividend = slice_section(doc, "6. 배당에 관한 사항")
    except Exception:  # noqa: BLE001
        dividend = ""
    if dividend:
        m = re.search(r"주당 현금배당금\(원\)\s*\|\s*보통주\s*\|\s*([\d,]+)", dividend)
        if m:
            out["주당현금배당금(보통주,당기)"] = m.group(1) + "원"
        m = re.search(r"현금배당성향\(%\)\s*\|\s*([\d.]+)", dividend)
        if m:
            out["현금배당성향(당기,%)"] = m.group(1)
    return out, values


def _field_change_log(rows: list[dict[str, Any]], fields: tuple[str, ...]) -> list[str]:
    """접수일 오름차순으로 인접 공시 간 필드 변경을 계산한다 (결정론적 diff)."""
    ordered = sorted(rows, key=lambda r: (str(r.get("rcept_dt") or ""), str(r.get("rcept_no") or "")))
    log: list[str] = []
    prev: dict[str, str] = {}
    for r in ordered:
        current = {f: r[f] for f in fields if r.get(f)}
        if not current:
            continue
        date = r.get("rcept_date") or r.get("rcept_dt")
        as_is = ", ".join(f"{k}={v}" for k, v in current.items())
        if not r.get("is_correction"):
            # 원본은 새 이사회 결의다. 직전 문서(다른 결의의 정정일 수 있음)와의
            # diff로 보여주면 별개 결정이 "변경"처럼 보여 혼동을 준다 — 절대값으로.
            label = "최초 결정" if not prev else "새 이사회 결의 (별개 결정)"
            log.append(f"{date} [원본 결정] {label} — {as_is}")
        else:
            diffs = [
                f"{k}: {prev.get(k, '(없음)')} → {v}"
                for k, v in current.items()
                if prev.get(k) != v
            ]
            log.append(f"{date} [기재정정]: " + ("; ".join(diffs) if diffs else "핵심 필드 변경 없음"))
        prev = current
    return log


def _holding_summary(head_text: str) -> dict[str, str]:
    """지분공시 머리 요약정보에서 보고사유·보고구분·비율 대비를 뽑는다."""
    out: dict[str, str] = {}
    lines = [l.strip() for l in head_text.split("\n") if l.strip()]
    for i, line in enumerate(lines):
        if line.startswith("보고사유"):
            out["보고사유"] = line.split("|", 1)[-1].strip() if "|" in line else line
        elif line.startswith("보고구분"):
            out["보고구분"] = line.split("|", 1)[-1].strip() if "|" in line else line
        elif line.startswith("직전 보고서") and "직전 보고서(주식수 | 비율)" not in out:
            out["직전 보고서(주식수 | 비율)"] = line.split("|", 1)[-1].strip()
        elif line.startswith("이번 보고서") and "이번 보고서(주식수 | 비율)" not in out:
            out["이번 보고서(주식수 | 비율)"] = line.split("|", 1)[-1].strip()
    if out:
        out["note"] = "지분 변동의 '사유'를 물으면 보고사유를 반드시 포함해 답하세요."
    return out


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


def _norm_date(value: Any, end: bool) -> str | None:
    """'2023-02-20' → '20230220', '2024' → '20240101'/'20241231'. 하이픈 날짜가
    FTS·검색 필터를 조용히 무력화한 실제 사례(V3-16) 대응."""
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) >= 8:
        return digits[:8]
    if len(digits) == 6:
        return digits + ("31" if end else "01")
    if len(digits) == 4:
        return digits + ("1231" if end else "0101")
    return None


_SEARCH_PARAMS: set[str] = set(
    next(spec for spec in (t.get("function", t) for t in TOOLS)
         if spec.get("name") == "search_filings")["input_schema"]["properties"]
)


def dispatch(name: str, tool_input: dict[str, Any]) -> Any:
    if isinstance(tool_input, dict):
        for _k, _end in (("date_from", False), ("date_to", True)):
            if tool_input.get(_k):
                tool_input[_k] = _norm_date(tool_input[_k], _end)
    if name == "get_company_info":
        query = _as_str(tool_input, "query")
        if not query:
            return {"error": "query가 비어 있습니다. 조회할 기업명이나 6자리 종목코드를 넣으세요."}
        # 시장명 질의("KOSDAQ", "코스닥 시총 1위") — 기업 아님 오류로 끝내지 않고
        # 시총순 구성 목록을 준다 (V4-09: 오류 후 즉시 포기한 실제 사례)
        _mkt = next((v for k, v in (("KOSDAQ", "KOSDAQ"), ("코스닥", "KOSDAQ"),
                                     ("KOSPI", "KOSPI"), ("코스피", "KOSPI"))
                     if k.lower() in query.lower()), None)
        if _mkt:
            from app.data.manifest import load_corpus as _lc3
            uni = _lc3().universe
            rows = uni[uni["market"].astype(str).str.upper() == _mkt].sort_values(
                "market_cap", ascending=False)
            return {
                "note": (
                    f"{_mkt} 소속 유니버스 기업 {len(rows)}곳, 시가총액(억원) 내림차순 — "
                    "1위가 목록 첫 번째입니다."
                ),
                "companies": [
                    {"corp_name": r["corp_name"], "market_cap": int(r["market_cap"]),
                     "sector": r["sector"]}
                    for _, r in rows.iterrows()
                ],
            }
        matches = resolve_company(query)
        if not matches:
            sector_hit = resolve_sector(query)
            if sector_hit:
                filter_key = "sector" if sector_hit["kind"] == "섹터" else "industry"
                comp = sector_hit.get("섹터_구성") or sector_hit.get("업종_구성") or {}
                comp_txt = ", ".join(f"{k} {v}곳" for k, v in comp.items())
                sector_hit["note"] = (
                    (f"{sector_hit['kind']} '{sector_hit['matched']}' {sector_hit['count']}곳의 구성: "
                     f"{comp_txt} — '어떤 섹터/업종으로 구성되는가'에는 이 구성값을 답하세요. " if comp_txt else "")
                    + f"'{query}' 를 {sector_hit['kind']} '{sector_hit['matched']}' 로 해석해 "
                    f"소속 기업 {sector_hit['count']}곳을 반환합니다. "
                    + "회사별로 나눠 부르지 "
                    f"말고 {filter_key}='{sector_hit['matched']}' 필터로 한 번에 검색하세요 — "
                    "정기보고서 검색이면 매출액·영업이익·당기순이익과 재무_합계(코드 합산)가 "
                    "자동으로 붙습니다."
                )
                return sector_hit
        if matches:
            # resolve_company 결과엔 market(KOSPI/KOSDAQ)·상장일·결산월이 없어
            # "시장 구분은?"에 답을 못 하고 시가총액 단위도 빠졌던 결함(V1-E-12).
            try:
                from app.data.manifest import load_corpus as _lc4
                uni = _lc4().universe.set_index("corp_code")
                for m in matches:
                    u = uni.loc[m.get("corp_code")] if m.get("corp_code") in uni.index else None
                    if u is None:
                        continue
                    m["market"] = str(u.get("market") or "")
                    m["상장일"] = str(u.get("listing_date") or "")
                    m["결산월"] = str(u.get("fiscal_month") or "")
                    m["corp_eng_name"] = str(u.get("corp_eng_name") or "")
                    cap = m.pop("market_cap", None)
                    if cap is not None:
                        m["시가총액_억원"] = int(cap)
                        m["시가총액"] = korean_amount_short(int(cap) * 100_000_000) + "원"
                    m["수집_공시_건수"] = {
                        "periodic": int(u.get("n_periodic") or 0), "major": int(u.get("n_major") or 0),
                        "exchange": int(u.get("n_exchange") or 0), "holding": int(u.get("n_holding") or 0),
                    }
            except Exception:  # noqa: BLE001 — 보강 실패가 기본 조회를 막으면 안 된다
                pass
        return matches or {
            "error": (
                f"{query!r} 와 일치하는 기업이 없습니다. 제공된 70개사 유니버스에 "
                "없는 기업일 수 있습니다 (그럴 경우 답변에서 범위 밖임을 알리세요)."
            )
        }

    if name == "search_filings":
        unknown = sorted(set(tool_input) - _SEARCH_PARAMS)
        if unknown:
            return {
                "error": (
                    f"search_filings에 없는 파라미터: {', '.join(unknown)}. 무시되지 않고 거부됩니다. "
                    f"사용 가능한 파라미터: {', '.join(sorted(_SEARCH_PARAMS))}. "
                    "발행회사는 company, 보고자(제출인)는 filer 입니다."
                )
            }
        group = tool_input.get("doc_group")
        subtype = tool_input.get("doc_subtype")
        # doc_subtype만 주고 doc_group을 빠뜨리는 일이 잦다. subtype은 그룹마다
        # 고유하므로 역추론할 수 있고, 그래야 그룹별 부가 집계(금액·비율 추세)가
        # 붙는다.
        if not group and subtype:
            for g, subs in _SUBTYPES_BY_GROUP.items():
                if subtype in subs:
                    group = g
                    break
        base_year = _as_int(tool_input.get("base_year"))
        date_from = tool_input.get("date_from")
        date_to = tool_input.get("date_to")
        # base_year(회계연도)는 정기공시에만 존재한다. 이벤트성 공시(major/
        # exchange/holding)에 base_year를 걸면 0건이 나와 모델이 같은 호출을
        # 반복하며 턴을 낭비한다(실제 사례). 이 경우 의도는 명백히 "그 해에
        # 공시된 것"이므로 접수연도 범위로 바꿔서 실행한다.
        # company가 유니버스에 없으면 조용한 0건 대신 원인을 알려준다.
        # 지분공시의 제출인(삼성물산, 국민연금 등)은 유니버스 밖이 대부분이라
        # company가 아니라 filer로 찾아야 한다.
        company_arg = _as_str(tool_input, "company")
        sector_arg = _as_str(tool_input, "sector") or None
        sector_note = None
        if company_arg and not resolve_company(company_arg):
            # company 자리에 섹터명이 오면 (예: company="방산·항공우주")
            # 오류 대신 sector 필터로 바꿔서 실행한다
            sector_hit = resolve_sector(company_arg)
            if sector_hit and not sector_arg:
                sector_arg = sector_hit["matched"]
                company_arg = ""
                sector_note = (
                    f"company='{_as_str(tool_input, 'company')}' 를 "
                    f"{sector_hit['kind']} '{sector_hit['matched']}' 필터로 해석해 실행했습니다."
                )
        industry_arg = tool_input.get("industry")
        # company가 특정되면 sector/industry는 아무것도 좁히지 못하고, 모델이
        # 추측한 값이 틀리면 검색 전체를 침몰시킨다 (company=카카오에
        # sector="IT"를 얹어 전 턴 0건 — 카카오의 업종은 커뮤니케이션서비스).
        if company_arg and resolve_company(company_arg) and (sector_arg or industry_arg):
            dropped = ", ".join(v for v in (sector_arg, str(industry_arg or "")) if v)
            sector_arg = None
            industry_arg = None
            sector_note = (
                f"company가 특정되어 sector/industry 필터({dropped})는 불필요하므로 "
                "무시하고 실행했습니다."
            )
        # sector 자리에 업종(industry) 대분류가 오는 경우 (sector="IT"로 전 턴
        # 0건을 받은 실제 사례 — "IT"는 industry 값이다). 컬럼을 바꿔 실행한다.
        if sector_arg:
            sh = resolve_sector(sector_arg)
            if sh and sh["kind"] == "업종":
                industry_arg = industry_arg or sh["matched"]
                sector_note = (
                    f"sector='{sector_arg}' 는 섹터가 아니라 업종(industry) 값이라 "
                    f"industry='{sh['matched']}' 필터로 해석해 실행했습니다."
                )
                sector_arg = None
        # 역방향 혼동도 실제로 있다: sector 자리에 기업명이 오는 경우
        # (sector="카카오"로 8턴 내내 0건을 받다 포기한 사례). 섹터로 해석되지
        # 않고 기업으로 해석되면 company 필터로 바꿔 실행한다.
        if sector_arg and not resolve_sector(sector_arg):
            corp_hit = resolve_company(sector_arg)
            if len(corp_hit) == 1:
                if not company_arg:
                    company_arg = corp_hit[0]["corp_name"]
                sector_arg = None
                sector_note = (
                    f"sector='{_as_str(tool_input, 'sector')}' 는 섹터가 아니라 기업명이라 "
                    f"company='{corp_hit[0]['corp_name']}' 필터로 해석해 실행했습니다."
                )
            else:
                # 섹터도 기업도 아니면 조용히 0건을 만들지 말고 필터를 버린다
                sector_arg = None
                sector_note = (
                    f"sector='{_as_str(tool_input, 'sector')}' 는 존재하지 않는 섹터라 "
                    "무시하고 실행했습니다."
                )
        if company_arg and not resolve_company(company_arg):
            return {
                "error": (
                    f"{company_arg!r} 은(는) 유니버스 70개사에 없습니다. "
                    "지분공시의 보고자(제출인)라면 company 대신 filer 파라미터로 "
                    f"검색하세요: filer={company_arg!r} (+ company에는 발행회사, "
                    "doc_group='holding'). 유니버스 밖 기업 자체에 대한 질문이면 "
                    "답변에서 데이터 범위 밖임을 알리세요."
                )
            }
        base_month = _as_int(tool_input.get("base_month"))
        converted_note = None
        _periodic_kw = ("사업보고서", "반기보고서", "분기보고서")
        event_signal = bool(
            (_as_str(tool_input, "report_type"))
            or tool_input.get("is_correction") is not None
            or (_as_str(tool_input, "keyword")
                and not any(k in _as_str(tool_input, "keyword") for k in _periodic_kw))
        )
        # base_month는 정기보고서에만 있는 개념이라, 이 값이 오면 doc_group이
        # 비어 있어도 정기보고서 의도다. is_correction 등 이벤트 신호로 접수일
        # 범위로 바꿔버리면 분기가 사라진다 (2024 1분기 정정 → 2024년 정정 16건
        # 으로 뒤바뀌어 3분기 문서를 1분기로 요약한 실제 사례 V2-05).
        if (group in ("major", "exchange", "holding")
                or (group is None and event_signal and base_month is None)) and (
            base_year is not None or base_month is not None
        ):
            # base_year/base_month는 정기공시 전용이다. 이벤트성 공시에 걸면
            # 무조건 0건이라(전부 NULL), 접수일 범위로 해석해 실행한다.
            # base_month=6으로 '하반기'를 표현하려던 실제 사례: 이 값이 그대로
            # 통과돼 7턴 내내 0건을 받다 포기했다 — 여기서 반드시 걷어낸다.
            year = base_year or ""
            if year:
                if not date_from:
                    date_from = f"{year}0101"
                if not date_to:
                    date_to = f"{year}1231"
            converted_note = (
                "base_year/base_month는 정기공시 전용이라 무시하고 접수일"
                + (f" {date_from}~{date_to}" if date_from or date_to else "")
                + " 조건으로 실행했습니다. 상반기/하반기 등 기간은 date_from/date_to로 "
                "지정하세요 (예: 하반기 → 0701~1231)."
            )
            base_year = None
            base_month = None
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
        # 모델이 limit을 안 주면 기본 20인데, 결과가 딱 20이면 잘린 것이다.
        # 경고만으로는 모델이 다시 부르지 않아 "2023~2024는 데이터에 없다"는
        # 오답이 나왔다. limit 미지정 시에는 잘림을 감지해 자동으로 넓힌다.
        explicit_limit = _as_int(tool_input.get("limit"))
        requested_limit = explicit_limit or 20
        group_by = _as_str(tool_input, "group_by")
        if group_by and _as_int(tool_input.get("min_amount")):
            # 금액 필터는 본문 추출이 필요해 집계 경로에서는 적용되지 않는다 —
            # group_by와 함께 오면 집계가 필터를 무시한 채 건수를 돌려줘 '73건'을
            # 답한 사례(V4-07). 개별 행 경로(원본_기준_건수·최대_건 계산)로 넘긴다.
            group_by = None
            converted_note = " ".join(n for n in (
                converted_note,
                "min_amount는 group_by 집계와 함께 쓸 수 없어 개별 행 검색으로 처리했습니다 "
                "(건수는 원본_기준_건수, 최대 계약은 amount_totals.최대_건).",
            ) if n)
        if group_by:
            try:
                # 비율 순위를 정확히 주려면 limit 이전의 넓은 집합이 필요하다.
                # 모델이 limit=3 을 걸면 '건수 상위 3곳'만 보고 그 안에서 비율을
                # 매기게 되어 실제 비율 순위와 어긋난다.
                wide = _aggregate_filings(
                    group_by,
                    company=company_arg or None,
                    sector=sector_arg,
                    industry=industry_arg,
                    doc_group=group,
                    doc_subtype=subtype,
                    report_type=_as_str(tool_input, "report_type") or None,
                    filer=_as_str(tool_input, "filer") or None,
                    market=_as_str(tool_input, "market") or None,
                    date_from=date_from,
                    date_to=date_to,
                    base_year=base_year,
                    is_correction=tool_input.get("is_correction"),
                    keyword=tool_input.get("keyword"),
                    min_count=_as_int(tool_input.get("min_count")) or 1,
                    limit=200,
                )
            except ValueError as e:
                return {"error": str(e)}
            # 단건 조회(회사×보고자×한 달)에 group_by를 걸어 보유비율 같은 행 단위
            # 보강 필드를 통째로 놓친 사례(M-08). 조건 전체가 몇 건 안 되면 집계가
            # 무의미하므로 개별 행 검색으로 넘긴다.
            _tiny_total = sum(g["count"] for g in wide)
            if len(wide) == 1 and 0 < _tiny_total <= 5:
                group_by = None
                converted_note = " ".join(n for n in (
                    converted_note,
                    f"그룹이 하나뿐(전체 {_tiny_total}건)이라 group_by 집계 대신 개별 행을 돌려줍니다.",
                ) if n)
        if group_by:
            for g in wide:
                if "correction_count" in g and "original_count" not in g:
                    g["original_count"] = int(g["count"]) - int(g.get("correction_count") or 0)
            groups = wide[: _as_int(tool_input.get("limit")) or 30]
            # 집계 결과에 doc_id가 하나도 없으면 모델이 인용을 요구받을 때 번호를
            # 지어낸다 (실제로 20260119800002 같은 연속 번호를 창작 — V2-14·H-07).
            # 그룹마다 대표 문서(최신)를 붙이고, 정기보고서 기업 집계면 그 문서의
            # 핵심 재무값까지 붙여 비교가 바로 되게 한다.
            _key_param = {"corp_name": "company", "flr_nm": "filer",
                          "report_type": "report_type", "doc_subtype": "doc_subtype",
                          "sector": "sector", "industry": "industry"}.get(group_by)
            for g in groups[:40]:
                try:
                    rep_kwargs = dict(
                        doc_group=group, date_from=date_from, date_to=date_to,
                        base_year=base_year, is_correction=tool_input.get("is_correction"),
                        keyword=tool_input.get("keyword"),
                        report_type=_as_str(tool_input, "report_type") or None,
                        doc_subtype=subtype, filer=_as_str(tool_input, "filer") or None,
                        market=_as_str(tool_input, "market") or None,
                        company=company_arg or None, sector=sector_arg, industry=industry_arg,
                    )
                    if group_by == "year":
                        rep_kwargs["date_from"] = f"{g['group_key']}0101"
                        rep_kwargs["date_to"] = f"{g['group_key']}1231"
                    elif _key_param:
                        rep_kwargs[_key_param] = str(g["group_key"])
                    rep_rows = _search_filings(**rep_kwargs, limit=1)
                except Exception:  # noqa: BLE001
                    rep_rows = []
                if not rep_rows:
                    continue
                rr = rep_rows[0]
                d_ = str(rr.get("rcept_dt") or "")
                g["대표_doc_id"] = rr.get("doc_id")
                g["대표_접수일"] = f"{d_[:4]}-{d_[4:6]}-{d_[6:]}" if len(d_) == 8 else d_
                g["대표_보고서명"] = rr.get("report_nm")
                if group_by == "corp_name" and (group == "periodic" or subtype in ("annual", "half", "quarter")) \
                        and groups.index(g) < 15:
                    try:
                        fin, _v = _periodic_financials(rr["doc_id"])
                        for k_ in ("매출액", "영업이익", "당기순이익"):
                            if k_ in fin:
                                g[k_] = fin[k_]
                    except Exception:  # noqa: BLE001
                        pass
            if not groups:
                return {"note": "집계 결과가 없습니다. 필터를 넓혀 다시 시도하세요."}
            # 비율 순위는 표본이 충분한 그룹만 — 1~2건짜리 100%가 상위를 채우면
            # 통계적으로 무의미하다. 호출자가 min_count를 주면 그 값을 존중한다.
            # 비율 순위에는 항상 최소 표본을 적용한다. 모델이 min_count=1 을
            # 넘겨 1~2건짜리 100% 기업이 상위를 채우는 일이 실제로 있었다.
            ratio_floor = max(_as_int(tool_input.get("min_count")) or 0, _RATIO_MIN_SAMPLE)
            # 거래소 계약공시를 집계하면 "규모 추이"를 묻는 경우가 대부분인데
            # 집계는 건수만 준다. 금액 합계까지 함께 계산해 붙인다
            # (모델이 group_by=year 로 건수만 받고 규모를 못 답한 사례).
            amount_summary: dict[str, Any] = {}
            ratio_summary: dict[str, Any] = {}
            if group == "holding":
                # 지분공시 집계는 "누가 얼마나 보고했나"만 준다. 대부분의 질문은
                # 보유비율 추세를 함께 묻기 때문에 비율 시계열도 계산해 붙인다.
                rows = _search_filings(
                    company=company_arg or None,
                    doc_group=group,
                    filer=_as_str(tool_input, "filer") or None,
                    date_from=date_from,
                    date_to=date_to,
                    is_correction=tool_input.get("is_correction"),
                    order="asc",
                    limit=_RATIO_TREND_MAX,
                )
                ratio_summary = _ratio_trend(rows)
            if group == "exchange":
                rows = _search_filings(
                    company=company_arg or None,
                    sector=sector_arg,
                    industry=industry_arg,
                    doc_group=group,
                    doc_subtype=subtype,
                    filer=_as_str(tool_input, "filer") or None,
                    market=_as_str(tool_input, "market") or None,
                    date_from=date_from,
                    date_to=date_to,
                    is_correction=tool_input.get("is_correction"),
                    keyword=tool_input.get("keyword"),
                    limit=_EXCHANGE_ENRICH_MAX,
                )
                for r in rows:
                    try:
                        r.update(_contract_fields(get_parsed(r["doc_id"]).text))
                    except Exception:  # noqa: BLE001
                        continue
                amount_summary = _amount_totals(rows)

            # is_correction 필터가 걸린 집계의 비율은 자명하게 0/100% — 그 값을
            # 답으로 옮긴 사례가 반복돼(J-02) 아예 제거하고 재검색을 안내한다
            if tool_input.get("is_correction") is not None:
                for g in wide:
                    g.pop("correction_pct", None)
                ranked = []
                # 정정만 걸러 집계하면 비율이 자명한 100%가 된다 — 같은 조건의
                # 전체(원본+정정) 집계를 한 번 더 돌려 '전체 대비 정정 비율'을 코드로
                # 붙인다. 재검색 안내만 받고 같은 호출을 반복하다 포기한 사례(ADV-J-02).
                if tool_input.get("is_correction") is True and group_by == "corp_name":
                    try:
                        base_groups = _aggregate_filings(
                            group_by, company=company_arg or None, sector=sector_arg,
                            industry=industry_arg, doc_group=group, doc_subtype=subtype,
                            report_type=_as_str(tool_input, "report_type") or None,
                            filer=_as_str(tool_input, "filer") or None,
                            market=_as_str(tool_input, "market") or None,
                            date_from=date_from, date_to=date_to, base_year=base_year,
                            is_correction=None, keyword=tool_input.get("keyword"),
                            min_count=1, limit=400,
                        )
                        base_count = {str(g["group_key"]): int(g["count"]) for g in base_groups}
                        for g in wide:
                            tot = base_count.get(str(g["group_key"]))
                            if tot:
                                g["전체_공시_건수"] = tot
                                g["correction_pct"] = round(100 * g["count"] / tot, 1)
                        ranked = sorted(
                            (g for g in wide if g.get("전체_공시_건수", 0) >= ratio_floor),
                            key=lambda g: -g.get("correction_pct", 0),
                        )[:5]
                    except Exception:  # noqa: BLE001
                        ranked = []
            else:
                ranked = sorted(
                    (g for g in wide if g["count"] >= ratio_floor),
                    key=lambda g: -g["correction_pct"],
                )[:5]
            # 1위 판독을 모델에 맡기면 21건 둘을 적고 20건을 최다라 결론내는
            # 자기모순이 나온다(V3-11) — 최다 그룹과 동률 여부를 코드로 박는다.
            top_count = wide[0]["count"] if wide else 0
            top_groups = [g["group_key"] for g in wide if g["count"] == top_count]
            fin_top: dict[str, Any] = {}
            for fld in ("매출액", "영업이익", "당기순이익"):
                scored = []
                for g in groups:
                    m_ = re.match(r"^(-?)([\d,]+)원", str(g.get(fld) or ""))
                    if m_:
                        scored.append((int(m_.group(2).replace(",", "")) * (-1 if m_.group(1) else 1),
                                       g["group_key"]))
                if len(scored) >= 2:
                    scored.sort(reverse=True)
                    fin_top[fld] = {"1위": scored[0][1], "값": f"{scored[0][0]:,}원",
                                    "2위": scored[1][1], "동률": scored[0][0] == scored[1][0]}
            result: dict[str, Any] = {
                "group_by": group_by,
                "group_count": len(wide),
                "최다": {
                    "count": top_count,
                    "그룹": top_groups,
                    "동률": len(top_groups) > 1,
                },
                "total_count": sum(g["count"] for g in wide),
                "총_공시_건수": sum(g["count"] for g in wide),
                "그룹_수": len(wide),
                "total_correction_count": sum(g.get("correction_count", 0) for g in wide),
                **({"재무_1위": fin_top} if fin_top else {}),
                "groups": groups,
                "top_by_correction_pct": [
                    {k: g[k] for k in ("group_key", "count", "correction_count", "correction_pct")}
                    for g in ranked
                ],
                "ratio_min_sample": ratio_floor,
                "note": (
                    ("'최다'는 공시 건수 기준입니다 — 매출·이익 비교는 재무_1위(코드 계산)를 "
                     "쓰고 건수 동률을 금액 동률로 옮기지 마세요. " if fin_top else "")
                    + "최다 그룹은 '최다' 값을 그대로 쓰세요 — 동률이면 모두 언급해야 "
                    "합니다. 조건 전체 건수는 total_count, 그룹 수는 group_count — "
                    "직접 합산·비교하지 마세요. "
                    f"groups는 건수 순, top_by_correction_pct는 정정 비율 순입니다"
                    f"(표본 {ratio_floor}건 이상만). "
                    "비율을 묻는 질문이면 top_by_correction_pct 를, 건수를 묻는 질문이면 "
                    "groups 를 쓰세요. 값을 직접 세거나 계산하지 마세요."
                ),
            }
            if tool_input.get("is_correction") is not None:
                result["note"] += (
                    " 정정 비율이 필요하면 is_correction 필터를 빼고 다시 집계하세요 — "
                    "이 결과의 비율은 필터 때문에 무의미해 제거했습니다."
                )
            if amount_summary:
                result["amount_totals"] = amount_summary
                result["note"] += " 연도별 금액 합계는 amount_totals 에 있습니다."
            if ratio_summary:
                result["ratio_trend"] = ratio_summary
                result["note"] += " 보유비율 추세는 ratio_trend 에 있습니다."
            # 계열 지분("유니버스 기업이 유니버스 기업을 보고") 질문에서 발행회사
            # 집계로 축을 뒤집은 실제 사례(V3-19) — 보고자∈유니버스 쌍을 코드로 계산
            if group_by == "flr_nm" and (group or "holding") == "holding":
                try:
                    from app.data.manifest import load_corpus as _lc2
                    uni_names = set(_lc2().universe["corp_name"].astype(str))
                    pair_rows = _search_filings(doc_group="holding", limit=3000)
                    from collections import Counter as _Counter
                    pairs = _Counter(
                        (str(r2["flr_nm"]), str(r2["corp_name"])) for r2 in pair_rows
                        if str(r2.get("flr_nm")) in uni_names
                    )
                    result["유니버스_보고자_상위쌍"] = [
                        {"보고자": a, "발행회사": b, "count": c}
                        for (a, b), c in pairs.most_common(5)
                    ]
                    result["note"] += (
                        " 보고자가 유니버스 기업인 지분공시의 (보고자→발행회사) 쌍 순위는 "
                        "유니버스_보고자_상위쌍 에 있습니다 (코드 계산)."
                    )
                except Exception:  # noqa: BLE001
                    pass
            # "없었던 기업" 질문(집합차)은 LLM 암산에 맡기면 누락된다 —
            # corp_name 집계에는 조건 매치가 0건인 유니버스 기업 목록을 코드로
            # 계산해 붙인다 (sector/industry/market 범위 내에서).
            if group_by == "corp_name" and not company_arg:
                try:
                    from app.data.manifest import load_corpus as _lc
                    uni = _lc().universe
                    if sector_arg:
                        uni = uni[uni["sector"].astype(str).str.contains(str(sector_arg), na=False)]
                    if industry_arg:
                        uni = uni[uni["industry"].astype(str).str.contains(str(industry_arg), na=False)]
                    mkt = _as_str(tool_input, "market")
                    if mkt:
                        uni = uni[uni["market"].astype(str).str.upper() == mkt.upper()]
                    scope = set(uni["corp_name"].astype(str))
                    matched_names = {str(g["group_key"]) for g in wide}
                    absent = sorted(scope - matched_names)
                    result["매치없는_기업"] = absent
                    result["note"] += (
                        f" 조건 범위 기업 {len(scope)}곳 중 매치가 0건인 기업 "
                        f"{len(absent)}곳은 매치없는_기업 목록에 있습니다 (코드 계산) — "
                        "직접 빼기 연산을 하지 마세요."
                    )
                except Exception:  # noqa: BLE001 — 여집합 실패가 집계를 막으면 안 된다
                    pass
            # 2단계 질문("비율 1위 회사의 최신 정정 사유")에서 회사를 뒤바꾼
            # 실제 사례(J-02) — 그룹이 몇 개 안 되면 그룹별 최신 정정을 붙인다.
            if group_by == "corp_name" and len(groups) <= 6:
                for g in groups:
                    if not g.get("correction_count"):
                        continue
                    try:
                        latest_cor = _search_filings(
                            company=str(g["group_key"]), is_correction=True,
                            doc_group=group, keyword=tool_input.get("keyword"),
                            date_from=date_from, date_to=date_to, limit=1,
                        )
                    except Exception:  # noqa: BLE001
                        latest_cor = []
                    if latest_cor:
                        row = dict(latest_cor[0])
                        _link_corrections_to_originals([row])
                        d = str(row.get("rcept_dt") or "")
                        reason = row.get("정정사유") or (row.get("정정사항_발췌") or "")[:120]
                        g["최신_정정"] = {
                            "doc_id": row.get("doc_id"),
                            "접수일": f"{d[:4]}-{d[4:6]}-{d[6:]}" if len(d) == 8 else d,
                            "보고서명": row.get("report_nm"),
                            **({"정정사유": reason} if reason else {}),
                        }
            try:
                bounds = dict(
                    company=company_arg or None, sector=sector_arg,
                    industry=industry_arg,
                    doc_group=group, doc_subtype=subtype,
                    report_type=_as_str(tool_input, "report_type") or None,
                    filer=_as_str(tool_input, "filer") or None,
                    market=_as_str(tool_input, "market") or None,
                    date_from=date_from, date_to=date_to, base_year=base_year,
                    is_correction=tool_input.get("is_correction"),
                    keyword=tool_input.get("keyword"), limit=1,
                )
                first = _search_filings(order="asc", **bounds)
                last = _search_filings(order="desc", **bounds)
                if first and last:
                    def _edge(r: dict) -> dict:
                        d = str(r.get("rcept_dt") or "")
                        return {
                            "doc_id": r.get("doc_id"), "corp_name": r.get("corp_name"),
                            "report_nm": r.get("report_nm"),
                            "rcept_date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                        }
                    result["date_range"] = {"earliest": _edge(first[0]), "latest": _edge(last[0])}
                    result["note"] += (
                        " 조건에 맞는 최초·최후 개별 공시는 date_range 에 있습니다 — "
                        "직접 추측하지 마세요."
                    )
            except Exception:  # noqa: BLE001 — 경계 첨부 실패가 집계 자체를 막으면 안 된다
                pass
            if sector_note or converted_note:
                result["filter_note"] = " ".join(n for n in (converted_note, sector_note) if n)
            return result

        min_amount = _as_int(tool_input.get("min_amount"))
        search_kwargs = dict(
            company=company_arg or None,
            sector=sector_arg,
            industry=industry_arg,
            doc_group=tool_input.get("doc_group"),
            doc_subtype=tool_input.get("doc_subtype"),
            report_type=_as_str(tool_input, "report_type") or None,
            filer=_as_str(tool_input, "filer") or None,
            market=_as_str(tool_input, "market") or None,
            date_from=date_from,
            date_to=date_to,
            base_year=base_year,
            base_month=base_month,
            is_correction=tool_input.get("is_correction"),
            keyword=tool_input.get("keyword"),
            order=_as_str(tool_input, "order") or "desc",
        )
        results = _search_filings(**search_kwargs, limit=600 if min_amount else requested_limit)
        # 모델이 limit=1 로 스스로 좁혀 놓고 그 1건을 "최대·최신·전부"로 착각한
        # 실제 사례 다수(M-01·M-10·V3-10·V4-09). 표시는 limit대로 하되 건수·
        # 최대값 같은 통계는 조건 전체 집합으로 계산해 함께 알린다.
        wide_for_stats: list[dict[str, Any]] | None = None
        if not min_amount and results and len(results) >= requested_limit:
            try:
                wide_for_stats = _search_filings(**search_kwargs, limit=5000)
            except Exception:  # noqa: BLE001
                wide_for_stats = None
            if wide_for_stats is not None and len(wide_for_stats) <= len(results):
                wide_for_stats = None
        # base_month 로 한 분기만 잡았는데 질문이 "회계연도 전체"일 수 있다 —
        # 같은 회계연도 전체 건수를 함께 알린다 (사업보고서 1건만 답한 사례 M-01)
        fiscal_year_total: int | None = None
        fiscal_year_rows: list[dict[str, Any]] = []
        _narrowed = base_month is not None or subtype in ("annual", "half", "quarter")
        if _narrowed and base_year is not None and group in (None, "periodic"):
            try:
                fy_kwargs = dict(search_kwargs, base_month=None, doc_subtype=None,
                                 doc_group="periodic")
                fiscal_year_rows = _search_filings(**fy_kwargs, limit=600)
                fiscal_year_total = len(fiscal_year_rows)
            except Exception:  # noqa: BLE001
                fiscal_year_total = None
        if min_amount:
            # 금액은 본문에만 있어 SQL로 못 거른다 — 캐시 본문에서 추출해 코드로
            # 거른다 (1조 이상 계약 건수를 목록 눈대중으로 세다 틀린 사례 대응)
            kept = []
            for r in results:
                try:
                    fields = _contract_fields(get_parsed(r["doc_id"]).text)
                except Exception:  # noqa: BLE001
                    continue
                raw = (fields.get("계약금액") or fields.get("투자금액") or "").split(" ")[0]
                try:
                    if int(raw.replace(",", "")) >= min_amount:
                        r.update(fields)
                        kept.append(r)
                except ValueError:
                    continue
            originals_kept = sum(1 for r in kept if not r.get("is_correction"))
            min_amount_originals = originals_kept
            min_amount_note = (
                f" 금액 필터(min_amount={min_amount:,}원) 적용 결과 총 {len(kept)}건 = "
                f"원본 {originals_kept}건 + 정정 {len(kept) - originals_kept}건 — "
                "같은 계약의 원본·정정이 함께 잡히므로 건수는 원본 기준으로 답하세요."
            )
            results = kept
        else:
            min_amount_note = ""
            min_amount_originals = None
        min_ratio = tool_input.get("min_ratio")
        try:
            min_ratio = float(min_ratio) if min_ratio is not None else None
        except (TypeError, ValueError):
            min_ratio = None
        if min_ratio is not None:
            # 지분율 임계 질문에서 건수 1위 기업에 남의 비율을 붙여 답한 혼합
            # 날조(V4-11) 대응 — 각 보고서의 '이번 보고' 비율을 코드로 걸러 준다
            wide_rows = results if len(results) >= 100 else _search_filings(
                company=company_arg or None, doc_group="holding",
                filer=_as_str(tool_input, "filer") or None,
                date_from=date_from, date_to=date_to,
                is_correction=tool_input.get("is_correction"), limit=600,
            )
            kept_r = []
            for r in wide_rows:
                try:
                    p_ = get_parsed(r["doc_id"])
                    head = p_.text[: p_.sections[0].offset] if p_.sections else p_.text[:1500]
                    cur = _holding_summary(head).get("이번 보고서(주식수 | 비율)") or ""
                    ratio = float(cur.split("|")[-1].strip())
                except Exception:  # noqa: BLE001
                    continue
                if ratio >= min_ratio:
                    r["보유비율_이번"] = cur
                    r["비율"] = ratio
                    kept_r.append(r)
            results = sorted(kept_r, key=lambda x: -x["비율"])
            companies_hit = sorted({str(r["corp_name"]) for r in results})
            min_amount_note += (
                f" 보유비율 {min_ratio}% 이상 보고는 총 {len(results)}건, "
                f"발행회사 {len(companies_hit)}곳: {', '.join(companies_hit)} (코드 스캔)."
            )
        auto_widened = False
        # min_amount 필터 결과를 자동 확장이 무필터 80건으로 덮어쓴 버그 방지
        if explicit_limit is None and min_amount is None and min_ratio is None and len(results) >= requested_limit:
            widened = _search_filings(
                company=company_arg or None,
                sector=sector_arg,
                industry=industry_arg,
                doc_group=group,
                doc_subtype=subtype,
                filer=_as_str(tool_input, "filer") or None,
                market=_as_str(tool_input, "market") or None,
                date_from=date_from,
                date_to=date_to,
                base_year=base_year,
                base_month=base_month,
                is_correction=tool_input.get("is_correction"),
                keyword=tool_input.get("keyword"),
                order=_as_str(tool_input, "order") or "desc",
                limit=_AUTO_WIDEN_LIMIT,
            )
            if len(widened) > len(results):
                results = widened
                requested_limit = _AUTO_WIDEN_LIMIT
                auto_widened = True
        if not results:
            # 0건이면 어떤 필터가 결과를 죽였는지 코드가 진단해 준다.
            # "기간을 넓혀 보세요" 같은 일반 조언은 무시되지만(같은 실패 호출을
            # 6번 반복한 실제 사례), "기간을 빼면 3건 있음(최신 2026-03-16)"은
            # 다음 호출을 정확히 유도한다.
            # 회계기간이 특정된 정기보고서 0건: 일반 완화("base_year를 빼면")를
            # 제안하면 모델이 다른 연도 수치로 대체한다(V3-20: 2026 반기 질문에
            # 2025 반기 실적으로 답변) — 미수집 사실을 정확히 알리고 끝낸다.
            if group == "periodic" and base_year:
                try:
                    latest = _search_filings(company=company_arg or None,
                                             doc_group="periodic", limit=1)
                except Exception:  # noqa: BLE001
                    latest = []
                latest_nm = f"{latest[0]['report_nm']} (접수 {latest[0]['rcept_dt']})" if latest else "없음"
                return {
                    "note": (
                        f"base_year={base_year}"
                        + (f", base_month={base_month}" if base_month else "")
                        + " 에 해당하는 정기보고서는 코퍼스에 수집되어 있지 않습니다. "
                        f"이 회사의 최신 수집분: {latest_nm}. 다른 연도·분기 수치로 "
                        "대체하지 말고 해당 보고서가 미수집이라 답하세요."
                    )
                }
            diagnosis = _diagnose_empty(
                company=company_arg or None, sector=sector_arg,
                doc_group=group, doc_subtype=subtype,
                report_type=_as_str(tool_input, "report_type") or None,
                filer=_as_str(tool_input, "filer") or None,
                date_from=date_from, date_to=date_to,
                base_year=base_year,
                is_correction=tool_input.get("is_correction"),
                keyword=tool_input.get("keyword"),
            )
            if diagnosis:
                return {"note": "이 필터 조합은 0건입니다. " + diagnosis}

        if results:
            # doc_group 없이 검색해도 결과가 한 그룹뿐이면 그 그룹의 강화를
            # 적용한다 (keyword=유상증자 검색이 전부 major인데 증자비율이
            # 안 붙어 모델이 "정보 부족"으로 포기한 실제 사례).
            row_groups = {str(r.get("doc_id", "")).split("_")[0] for r in results}
            if group is None and len(row_groups) == 1:
                group = row_groups.pop()
            # 지분공시는 "보유비율이 어떻게 변했나"가 핵심인데, 그 값은 각 문서
            # 머리 요약에만 있다. 79건을 하나씩 읽는 건 불가능하므로(턴 한도)
            # 검색 단계에서 캐시된 본문을 훑어 비율·사유를 행에 실어 준다.
            # 거래소 계약공시도 마찬가지 — "규모 추이"를 물으면 계약금액이
            # 필요한데 60건을 하나씩 읽을 수는 없다. 문서가 작으므로(평균 859자)
            # 검색 단계에서 금액·계약명을 뽑아 행에 실어 준다.
            if group == "exchange" and len(results) <= _EXCHANGE_ENRICH_MAX:
                for r in results:
                    try:
                        body = get_parsed(r["doc_id"]).text
                    except Exception:  # noqa: BLE001
                        continue
                    fields = _contract_fields(body)
                    r.update(fields)
            if group == "major" and len(results) <= _MAJOR_ENRICH_MAX:
                for r in results:
                    try:
                        r.update(_major_fields(get_parsed(r["doc_id"]).text))
                    except Exception:  # noqa: BLE001
                        continue
            fin_values: list[tuple[dict[str, Any], dict[str, int]]] = []
            if group == "periodic" and len(results) <= 30:
                # 요약재무정보 3항목(연결, 당기 열)을 코드로 붙인다 — 모델이
                # 본문을 읽으며 이웃 행을 잘못 집는 것을 원천 차단
                for r in results:
                    fin, vals = _periodic_financials(r["doc_id"])
                    if not fin and r.get("is_correction"):
                        # 정기보고서 정정본은 고친 페이지만 담아 요약재무정보가
                        # 없는 경우가 많다 (신한지주·메리츠 실측) — 같은 기간의
                        # 원본 보고서에서 추출한다
                        try:
                            originals = _search_filings(
                                company=r.get("corp_name"), doc_group="periodic",
                                doc_subtype=r.get("doc_subtype"),
                                base_year=int(r["base_year"]) if r.get("base_year") else None,
                                base_month=int(r["base_month"]) if r.get("base_month") else None,
                                is_correction=False, limit=1,
                            )
                        except Exception:  # noqa: BLE001
                            originals = []
                        if originals:
                            fin, vals = _periodic_financials(originals[0]["doc_id"])
                            if fin:
                                fin = dict(fin)
                                fin["재무_출처"] = f"원본 {originals[0]['doc_id']} 에서 추출"
                    if fin:
                        r.update(fin)
                        r["재무_note"] = "매출액·영업이익·당기순이익은 요약재무정보 당기(연결) 값의 코드 추출"
                        fin_values.append((r, vals))
            if len(fin_values) >= 2 and len(results) == requested_limit:
                # limit에 잘린 집합의 합산은 조용한 오답이 된다 (limit=8로 정정본만
                # 잡혀 미래에셋이 빠진 실제 사례) — 계산하지 않고 재검색을 지시한다
                fin_values = []
                truncated_sum_note = (
                    " 결과가 limit에 잘려 재무_합계를 계산하지 않았습니다 — "
                    "limit을 빼고 다시 검색하면 전체 합산이 제공됩니다."
                )
            else:
                truncated_sum_note = ""
            if len(fin_values) >= 2:
                dedup: dict[tuple, tuple[dict[str, Any], dict[str, int]]] = {}
                for r, vals in fin_values:
                    key = (r.get("corp_name"), str(r.get("base_year")), str(r.get("base_month")))
                    if key not in dedup:  # 결과는 최신 접수순 — 첫 항목이 최신본
                        dedup[key] = (r, vals)
                totals: dict[str, int] = {}
                for _r, vals in dedup.values():
                    for k, v in vals.items():
                        if k in ("매출액", "영업이익", "당기순이익"):
                            totals[k] = totals.get(k, 0) + v
                covered = sorted({str(r.get("corp_name")) for r, _v in dedup.values()})
                all_names = sorted({str(r.get("corp_name") or "") for r in results} - {""})
                missing = [c for c in all_names if c not in covered]
                fin_summary = {
                    k: f"{v:,} ({'-' if v < 0 else ''}{korean_amount_short(abs(v))})"
                    for k, v in totals.items()
                }
                fin_summary["대상"] = (
                    f"{len(dedup)}개 보고서(회사·기간 중복 제거, 기업 {len(covered)}곳)"
                    + (f", 추출 실패: {', '.join(missing)}" if missing else "")
                )
            else:
                fin_summary = {}
            if group == "holding" and len(results) <= _HOLDING_ENRICH_MAX:
                for r in results:
                    try:
                        parsed = get_parsed(r["doc_id"])
                    except Exception:  # noqa: BLE001 - 못 읽는 문서는 건너뛴다
                        continue
                    head = parsed.text[: parsed.sections[0].offset] if parsed.sections else parsed.text[:1500]
                    hs = _holding_summary(head)
                    if hs.get("이번 보고서(주식수 | 비율)"):
                        r["보유비율_이번"] = hs["이번 보고서(주식수 | 비율)"]
                    if hs.get("직전 보고서(주식수 | 비율)"):
                        r["보유비율_직전"] = hs["직전 보고서(주식수 | 비율)"]
                    try:
                        _prev = float(str(r.get("보유비율_직전", "")).split("|")[-1])
                        _cur = float(str(r.get("보유비율_이번", "")).split("|")[-1])
                        _d = round(_cur - _prev, 2)
                        r["보유비율_변동"] = (
                            f"{_prev}% → {_cur}% ({'+' if _d > 0 else ''}{_d}%p, 코드 계산)"
                            if _d != 0 else f"{_prev}% → {_cur}% (변동 없음)"
                        )
                    except (ValueError, TypeError):
                        pass
                    if hs.get("보고사유"):
                        r["보고사유"] = hs["보고사유"][:120]
            _link_corrections_to_originals(results)
            for r in results:
                if r.get("file_format") == "pdf+html":
                    # 원문 XML이 없어 본문이 뷰어 껍데기(수백 자)뿐인 문서 — 모델이
                    # 다른 문서 내용으로 채워 요약한 실제 사례(V2-05)
                    r["원문_미제공"] = ("DART 원문 XML 미제공(pdf+html 대체수집). 본문 텍스트가 "
                                     "없어 수치·내용을 읽을 수 없음 — 확인 불가로 답할 것")
                d = str(r.get("rcept_dt") or "")
                if len(d) == 8 and d.isdigit():
                    # 모델이 8자리 날짜를 옮겨 적다 연도를 틀리므로 완성형을 준다
                    r["rcept_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
                # "(2026.03)" 분기보고서를 '2026년 3분기'로 옮긴 사례(V1-X-07 3차) —
                # 보고기간 라벨을 코드로 준다
                if r.get("doc_group") == "periodic" and r.get("base_year") and r.get("base_month"):
                    _bm = int(r["base_month"])
                    _lab = {3: "1분기", 6: "반기(상반기)", 9: "3분기", 12: "사업연도(연간)"}.get(_bm, f"{_bm}월 결산")
                    r["보고기간"] = f"{r['base_year']}년 {_lab} (기준일 {r['base_year']}.{_bm:02d})"
            # 금액이 뽑힌 계약공시라면 연도별 합계까지 코드가 계산해 준다.
            # 모델이 20여 건을 암산으로 더하다 총액이 구성 항목보다 작아지는
            # 오답이 실제로 나왔다 — 합산도 계산이므로 맡기지 않는다.
            stats_rows = results
            if wide_for_stats and group == "exchange" and len(wide_for_stats) <= 200:
                # 최대 계약·합계는 표시된 몇 건이 아니라 조건 전체에서 계산한다
                stats_rows = []
                for wr in wide_for_stats:
                    try:
                        wr2 = dict(wr)
                        wr2.update(_contract_fields(get_parsed(wr["doc_id"]).text))
                        stats_rows.append(wr2)
                    except Exception:  # noqa: BLE001
                        continue
            amount_summary = _amount_totals(stats_rows)
            major_field_names = ("신주수", "증자전주식총수", "증자비율", "시설자금",
                                 "타법인취득자금", "운영자금", "채무상환자금",
                                 "예정발행가", "사채총액", "이사회결의일")
            change_log = (
                _field_change_log(results, major_field_names)
                if group == "major" and any(any(r.get(f) for f in major_field_names) for r in results)
                else []
            )
            # 건수·기업 수·정정 수는 limit에 잘리지 않은 전체 집합으로 센다 —
            # 월별 접수 건수를 80건(자동 확장 limit)으로 답한 실제 사례(V3-13).
            full_rows = wide_for_stats if wide_for_stats else results
            corrections = sum(1 for r in full_rows if r.get("is_correction"))
            companies = sorted({str(r.get("corp_name") or "") for r in full_rows} - {""})
            summary = {
                "count": len(full_rows),
                **({"표시_건수": len(results)} if len(full_rows) != len(results) else {}),
                # is_correction 필터가 걸리면 정정/원본 수는 count와 같거나 0 —
                # 그 값으로 "정정 비율 100%"를 만든 실제 사례(J-02)가 있어 뺀다
                **({} if tool_input.get("is_correction") is not None else {
                    "correction_count": corrections,
                    "original_count": len(full_rows) - corrections,
                }),
                # 정정 비율을 암산해 순위를 뒤집은 실제 사례(J-02: 35.3%인
                # 두산로보틱스 대신 26.3%인 레인보우를 1위로) — 코드가 계산한다.
                # 결과가 limit에 잘렸으면 분모가 틀리므로 붙이지 않는다.
                # is_correction 필터가 걸린 검색의 비율은 자명하게 0/100%라
                # 무의미하다 (그 100%를 답으로 옮긴 실제 사례) — 필터 없을 때만.
                **({"correction_pct": round(corrections / len(full_rows) * 100, 1)}
                   if full_rows and (wide_for_stats or len(results) < requested_limit)
                   and tool_input.get("is_correction") is None else {}),
                "company_count": len(companies),
                "companies": companies,
                "note": (
                    f"총 {len(full_rows)}건 = 원본 {len(full_rows) - corrections}건 + "
                    f"[기재정정] {corrections}건, 기업 {len(companies)}곳"
                    + (f" (목록에는 {len(results)}건만 표시)" if len(full_rows) != len(results) else "")
                    + ". "
                    "건수·기업 수를 답할 때는 이 집계값을 그대로 사용하세요 "
                    "— 목록을 직접 세지 마세요."
                    + (" 정정이 여럿이면 접수번호가 큰 쪽이 더 나중의(최신) 정정입니다"
                       " — 같은 접수일이어도 마찬가지입니다." if corrections >= 2 else "")
                    + (" 정정 비율이 필요하면 is_correction 필터를 빼고 검색하세요 — "
                       "그때 correction_pct가 계산됩니다."
                       if tool_input.get("is_correction") is not None else "")
                    + (
                        f" (기본 20건에서 잘려 자동으로 {len(results)}건까지 넓혔습니다)"
                        if auto_widened
                        else ""
                    )
                    + (
                        f" ⚠ 목록은 limit={requested_limit}에서 잘렸습니다 — count·원본·정정 수는 "
                        "전체 기준이니 그대로 쓰고, 개별 건이 더 필요하면 조건을 좁히세요."
                        if len(results) >= requested_limit
                        else ""
                    )
                ),
            }
            if amount_summary:
                summary["amount_totals"] = amount_summary
                summary["note"] += (
                    " 연도별 금액 합계는 amount_totals 에 계산돼 있으니 직접 더하지 마세요."
                )
            if change_log:
                summary["field_change_log"] = change_log
                summary["note"] += (
                    " 정정 이력의 변경 내용은 field_change_log 에 시간순으로 계산돼 "
                    "있습니다 — 문서를 직접 비교하지 말고 이 목록을 그대로 사용하세요. "
                    "[원본 결정]은 새 이사회 결의, [기재정정]은 그 정정입니다."
                )
            notes = [n for n in (converted_note, sector_note) if n]
            if notes:
                summary["filter_note"] = " ".join(notes)
            if fin_summary:
                # 여러 보고서의 합산을 모델 암산에 맡기지 않는다 (H-03: 8개사
                # 합계가 매번 다르게 틀린 실제 사례) — 원화 정수로 코드 합산
                summary["재무_합계"] = fin_summary
                summary["note"] += (
                    " 재무_합계는 각 보고서에서 추출된 매출액·영업이익·당기순이익의 "
                    "코드 합산입니다(회사·기간 중복 제거) — 직접 더하지 마세요."
                )
            summary["note"] += truncated_sum_note + min_amount_note
            if min_amount_originals is not None:
                # count(정정 포함 70건)를 그대로 답한 사례(V4-07) — 원본 기준 건수를 키로 박는다
                summary["원본_기준_건수"] = min_amount_originals
                summary["정정_포함_건수"] = len(results)
            if wide_for_stats:
                summary["전체_매치_건수"] = len(wide_for_stats)
                summary["note"] += (
                    f" 목록은 {len(results)}건만 표시했지만 조건에 맞는 공시는 전체 "
                    f"{len(wide_for_stats)}건입니다. 건수·최대값·합계(count, amount_totals, 최대_건)는 "
                    "전체 기준으로 이미 계산돼 있으니 추가 검색 없이 그 값으로 답하세요."
                )
            if fiscal_year_total and fiscal_year_total > len(results):
                summary["회계연도_전체_건수"] = fiscal_year_total
                # 안내문만으로는 모델이 1건만 나열하는 일이 반복돼(M-01 3차 실행) 목록
                # 자체를 붙인다 — 한 회사의 한 회계연도는 많아야 정정 포함 10건 안팎이다.
                if fiscal_year_total <= 12:
                    def _fy_brief(r: dict[str, Any]) -> dict[str, Any]:
                        d = str(r.get("rcept_dt") or "")
                        return {"doc_id": r.get("doc_id"), "보고서명": r.get("report_nm"),
                                "접수일": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d,
                                "corp_name": r.get("corp_name")}
                    summary["회계연도_전체_목록"] = [
                        _fy_brief(r) for r in sorted(
                            fiscal_year_rows, key=lambda r: str(r.get("rcept_dt") or ""))
                    ]
                _narrow_desc = (f"base_month={base_month}" if base_month is not None
                                else f"doc_subtype={subtype}")
                summary["note"] += (
                    f" {_narrow_desc} 로 일부 보고서만 잡았습니다 — 같은 회계연도 정기공시는 "
                    f"분기·반기·사업보고서를 합쳐 {fiscal_year_total}건입니다."
                    + (" 회계연도 전체를 물었으면 회계연도_전체_목록을 그대로 나열하세요 (재검색 불필요)."
                       if "회계연도_전체_목록" in summary else " 회계연도 전체를 물었으면 base_month·doc_subtype을 빼세요.")
                )
            # 내림차순 목록의 마지막 행(가장 오래된 것)을 "가장 최근"이라 답한 실제
            # 사례(V4-09) — 최신·최초 공시를 코드로 박는다.
            if len(full_rows) >= 2:
                _by_date = sorted(full_rows, key=lambda r: (str(r.get("rcept_dt") or ""),
                                                            str(r.get("rcept_no") or "")))
                def _brief(r: dict[str, Any]) -> dict[str, Any]:
                    d = str(r.get("rcept_dt") or "")
                    return {"doc_id": r.get("doc_id"),
                            "접수일": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else r.get("rcept_date"),
                            "보고서명": r.get("report_nm"), "corp_name": r.get("corp_name")}
                summary["최신_공시"] = _brief(_by_date[-1])
                summary["최초_공시"] = _brief(_by_date[0])
                summary["note"] += " 가장 최근/가장 이른 공시는 최신_공시·최초_공시 값을 그대로 쓰세요."
            # 원본 1건 + 정정 n건이면 최종 유효본을 코드로 박는다 — desc 정렬 첫 행이
            # 최신인데도 원본(마지막 행)을 "가장 최근"이라 답한 사례(ADV-C-01).
            if (summary.get("original_count") == 1 and summary.get("correction_count", 0) >= 1
                    and len(results) <= 20):
                chain = sorted(results, key=lambda r: (str(r.get("rcept_dt")), str(r.get("rcept_no"))))
                originals = [r for r in chain if not r.get("is_correction")]
                cors = [r for r in chain if r.get("is_correction")]
                summary["정정_체인"] = {
                    "원본": {"doc_id": originals[0]["doc_id"], "접수일": originals[0].get("rcept_date")},
                    "정정본_시간순": [{"doc_id": c["doc_id"], "접수일": c.get("rcept_date")} for c in cors],
                    "정정_횟수": len(cors),
                    "최종_유효본": {"doc_id": chain[-1]["doc_id"], "접수일": chain[-1].get("rcept_date")},
                }
                summary["note"] += (
                    " 정정_체인의 최종_유효본이 현재 유효한 문서이고 정정_횟수가 정정 횟수입니다 "
                    "(코드 계산) — 순서를 직접 판단하지 마세요."
                )
            # 섹터/업종 범위 안에서 "한 번도 내지 않은 회사"를 물으면 집합차가
            # 필요하다 — 개별 행 검색에서도 매치 0건 기업을 코드로 붙인다(V2-09:
            # 3사 중 1사만 매치된 결과를 받고 나머지 2사를 끝내 명시하지 못함).
            if (sector_arg or industry_arg) and not company_arg:
                full = wide_for_stats if wide_for_stats is not None else (
                    results if len(results) < requested_limit else None)
                if full is not None:
                    try:
                        from app.data.manifest import load_corpus as _lc5
                        uni = _lc5().universe
                        if sector_arg:
                            uni = uni[uni["sector"].astype(str).str.contains(str(sector_arg), na=False)]
                        if industry_arg:
                            uni = uni[uni["industry"].astype(str).str.contains(str(industry_arg), na=False)]
                        scope = set(uni["corp_name"].astype(str))
                        matched = {str(r.get("corp_name")) for r in full}
                        absent = sorted(scope - matched)
                        summary["매치된_기업"] = sorted(scope & matched)
                        summary["매치없는_기업"] = absent
                        summary["note"] += (
                            f" 범위 기업 {len(scope)}곳 중 조건 매치 {len(scope & matched)}곳, "
                            f"매치 0건 {len(absent)}곳(매치없는_기업, 코드 계산) — 회사별로 "
                            "다시 검색하지 마세요."
                        )
                    except Exception:  # noqa: BLE001
                        pass
            summary["filings"] = results
            return summary
        if (sector_arg or industry_arg) and not company_arg and (subtype or group or tool_input.get("report_type")):
            try:
                from app.data.manifest import load_corpus as _lc6
                uni = _lc6().universe
                if sector_arg:
                    uni = uni[uni["sector"].astype(str).str.contains(str(sector_arg), na=False)]
                if industry_arg:
                    uni = uni[uni["industry"].astype(str).str.contains(str(industry_arg), na=False)]
                scope = sorted(uni["corp_name"].astype(str))
                if scope:
                    return {
                        "count": 0,
                        "범위_기업": scope,
                        "매치없는_기업": scope,
                        "note": (
                            f"범위 기업 {len(scope)}곳({', '.join(scope)}) 모두 이 조건의 공시가 0건입니다 "
                            "(코드 계산). '있는가/어디인가' 질문이면 '해당 없음'이 답이니 다른 유형·"
                            "다른 연도의 공시로 대체하지 마세요. 조건을 잘못 잡은 게 아닌지 "
                            "(회계연도 base_year vs 접수일 date_from/date_to, doc_subtype 표기)만 한 번 확인하세요."
                        ),
                    }
            except Exception:  # noqa: BLE001
                pass
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
            # "감자" 같은 2자 키워드는 trigram 본문검색이 불가하다 — 오류만 주면
            # 같은 호출을 반복하다 포기하므로(V3-04) 보고서명 검색으로 자동 전환
            rows = _search_filings(
                company=tool_input.get("company"), sector=tool_input.get("sector"),
                doc_group=tool_input.get("doc_group"), keyword=fts_query,
                date_from=_as_str(tool_input, "date_from") or None,
                date_to=_as_str(tool_input, "date_to") or None, limit=50,
            )
            return {
                "note": (
                    f"query '{fts_query}' 는 3자 미만이라 본문검색 대신 보고서명 "
                    f"검색(search_filings keyword)으로 전환했습니다 — {len(rows)}건."
                ),
                "results": rows,
            }
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
        section_title = (_as_str(tool_input, "section_title")
                         or _as_str(tool_input, "section_label")
                         or _as_str(tool_input, "section")
                         or _as_str(tool_input, "title"))
        _fname = tool_input.get("filename")
        attach_note = None
        # "감사보고서"를 섹션 제목으로 넣으면 본문의 요약재무정보 구간이 잡혀 감사인을
        # 못 찾는다(V2-04) — 첨부 파일 라벨과 맞으면 그 첨부를 통째로 읽게 돌린다.
        if not _fname and section_title and re.search(r"감사\s*보고서|감사인|감사\s*의견", section_title):
            try:
                from app.data.cache import list_available_files as _laf
                cands = [fi for fi in _laf(doc_id) if not fi.is_main and "감사보고서" in fi.label]
                if cands:
                    want_consol = "연결" in section_title
                    pick = next((fi for fi in cands if ("연결" in fi.label) == want_consol), cands[0])
                    _fname = pick.path.name
                    section_title = ""
                    attach_note = (f"섹션이 아니라 첨부 파일 '{pick.label}'({_fname})을 읽었습니다. "
                                   "다른 첨부는 list_sections의 available_files를 보세요.")
            except Exception:  # noqa: BLE001
                pass
        try:
            doc = get_parsed(doc_id, _fname)
        except (FilingNotFoundError, UnsupportedFileError) as e:
            return {"error": str(e)}
        if attach_note and not doc.sections:
            pass
        if attach_note and doc.sections and not section_title:
            section_title = doc.sections[0].title  # 첨부 문서의 첫 섹션부터

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
        fallback_note = None
        if not text:
            # 목차(ATOC)에 없는 내용도 본문에는 있을 수 있다 — 정정 분기보고서의
            # "타법인출자 현황(상세)" 표가 목차 없이 본문에만 있어, 정확한 제목으로
            # 세 번 요청하고도 매번 빈 결과를 받은 실제 사례. 제목 토큰을 본문에서
            # 찾아 그 주변을 돌려준다.
            # 한국어 명사는 두 글자가 많다(임원·직원·현황) — 1자 조사만 거른다
            tokens = [t for t in re.split(r"[\s./()\dⅠ-Ⅻ]+", section_title) if len(t) >= 2]
            for tok in sorted(tokens, key=len, reverse=True):
                i = doc.text.find(tok)
                if i < 0 and len(tok) >= 4:
                    # 공백 표기 차이("유상증자결정" vs 본문 "유상증자 결정") 흡수
                    m_ = re.search(r"\s*".join(map(re.escape, tok)), doc.text)
                    i = m_.start() if m_ else -1
                if i >= 0:
                    start = max(0, i - 200)
                    text = doc.text[start:]
                    fallback_note = (
                        f"'{section_title}' 은 목차에 없지만 본문에서 '{tok}' 위치를 찾아 "
                        "그 주변부터 반환합니다."
                    )
                    break
        if not text:
            return {
                "error": f"섹션 {section_title!r} 을(를) 찾지 못했습니다.",
                "available_sections": [sec.title for sec in doc.sections][:60],
                "hint": "위 목록에서 정확한 제목을 골라 다시 호출하세요.",
            }
        jump_note = None
        jump_kw = _as_str(tool_input, "keyword")
        if jump_kw:
            # ㈜/(주)/공백 표기 차이를 흡수해 위치를 찾는다
            variants = [jump_kw, jump_kw.replace("㈜", "(주)"), jump_kw.replace("(주)", "㈜"),
                        jump_kw.replace("㈜", "").replace("(주)", ""), jump_kw.replace(" ", "")]
            hits = [text.find(v) for v in dict.fromkeys(v for v in variants if v)]
            hits = [h for h in hits if h >= 0]
            pos = min(hits) if hits else -1
            if pos >= 0:
                if pos > 700:
                    # 표 머리(열 이름·단위 선언)가 잘리면 모델이 열을 오독한다
                    # (수량 510,810을 장부가액으로 답한 실제 사례) — 머리를 함께 준다
                    text = text[:400] + "\n…(중략)…\n" + text[pos - 300:]
                else:
                    text = text[max(0, pos - 300):]
                jump_note = f"'{jump_kw}' 위치로 이동해 그 주변부터 반환합니다 (표 머리 포함)."
            else:
                # keyword가 없어도 섹션 자체는 유효하다 — 빈손으로 돌려보내면
                # 모델이 같은 호출만 반복한다(질문 구절을 통째로 keyword에 넣은
                # 실제 사례). 전체를 주고 사실만 알린다.
                jump_note = (
                    f"'{jump_kw}' 문자열은 이 본문에 없습니다 — 섹션 전체를 반환합니다. "
                    "특정 회사·항목명을 keyword로 쓰면 그 위치로 이동합니다."
                )
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
        _meta = get_filing(doc_id) or {}
        if _meta.get("file_format") == "pdf+html":
            result["원문_미제공"] = (
                "이 문서는 DART 원문 XML이 없어 뷰어 껍데기만 파싱됐습니다(pdf+html 대체수집). "
                "위 텍스트에 실제 보고서 내용은 없습니다 — 수치·요약을 지어내지 말고 "
                "'원문 미제공으로 확인 불가'라고 답하세요."
            )
        if not doc.sections:
            # 거래소공시는 목차(ATOC)가 없는 정형 폼이다 — "몇 번째 섹션" 같은
            # 질문에 순번을 창작한 실제 사례(G-02) 대응
            result["문서_구조"] = (
                "이 문서는 목차(섹션 구분)가 없는 정형 폼입니다. '1. 판매ㆍ공급계약 구분' 같은 "
                "숫자는 서식 항목 번호이며 목차 순번이 아닙니다."
            )
        if fallback_note or jump_note:
            result["note"] = " ".join(n for n in (fallback_note, jump_note) if n)
        if jump_kw and "타법인출자" in (section_title + result.get("text", "")[:400]):
            parsed_row = _equity_row_parse(result.get("text", ""), jump_kw)
            if parsed_row:
                result["행_파싱"] = parsed_row
                result["행_파싱_note"] = (
                    "행_파싱은 코드가 서식의 고정 열 순서대로 라벨링한 값입니다 — "
                    "표에서 직접 열을 세지 말고 이 값을 쓰세요. 금액 단위는 unit_note 참고."
                )
        # 넓은 표가 잘린 채 반환되면 모델이 열을 잘못 짚기 쉽다 (수량 열을
        # 장부가액으로 답한 실제 사례) — 행 단위 재조회 방법을 그 자리에서 알린다
        if not jump_kw and result.get("truncated") and result.get("text", "").count("|") > 150:
            result["table_note"] = (
                "긴 표입니다. 특정 회사·항목의 값만 필요하면 keyword=그 이름 으로 다시 "
                "호출하세요 — 표 머리와 해당 행을 붙여 돌려주므로 열을 정확히 짚을 수 있습니다."
            )
        # 임원 현황 질문: 표가 거대해(미등기 포함 수백 행) 모델이 미등기 임원을
        # 섞거나 일부만 나열한 실제 사례(E-04) — 등기임원 행만 코드로 걸러 준다.
        if "임원" in section_title:
            roster = []
            # 섹션 제목 변형(상위 챕터 매칭 등)과 무관하게 붙도록 문서 전체를
            # 스캔한다 — 행 패턴(성별 + 사내/사외이사 + 10개 열)이 충분히 특이적
            for line in doc.text.split("\n"):
                f = [c.strip() for c in line.split("|")]
                if (len(f) >= 10 and f[1] in ("남", "여")
                        and f[4] in ("사내이사", "사외이사")):
                    roster.append(f"{f[0]} ({f[1]}, 출생년월 {f[2]}) — {f[3]}, {f[4]}, {f[5]}")
            if roster:
                result["등기임원_명단"] = roster
                result["등기임원_note"] = (
                    f"이 보고서의 등기임원(사내·사외이사)은 위 {len(roster)}명이 전원입니다 "
                    "(코드 추출) — 표를 직접 훑지 말고 이 목록을 쓰고, '일부'라고 하지 "
                    "마세요. 공시에는 생년월일이 아니라 출생년월까지만 기재됩니다."
                )
        # 축약 단위 표: "(단위: 백만원)" 표에서 쉼표 1개짜리 숫자(118,235)는
        # 자동환산 주석 대상이 아니라, 모델이 단위를 놓치고 "118,235원"처럼
        # 답할 수 있다. 어떤 단위 선언이 있는지 명시적으로 경고한다.
        page_text = result.get("text") or ""
        declared = sorted({
            m.group(1) for m in _UNIT_RE.finditer(page_text)
            if _UNIT_SCALES.get(m.group(1), 1) > 1
        })
        if declared:
            units = ", ".join(declared)
            result["unit_note"] = (
                f"이 본문 표에는 (단위: {units}) 선언이 있습니다 — 해당 표의 금액 "
                f"숫자는 {units} 단위이므로 답변에 단위를 반드시 붙이세요 "
                "(예: 백만원 표의 12,345 → 12,345백만원 ≈ 123억 4,500만원). "
                "수량(주)·비율(%) 열은 제외."
            )
        # 지분공시의 보고사유(인적분할, 장내매수 등)는 어떤 섹션에도 속하지 않는
        # 문서 머리 요약정보에만 있다. 섹션만 읽으면 "왜 변동했는지"를 놓치므로
        # 어떤 섹션을 읽든 요약을 함께 실어준다.
        if attach_note:
            result["note_attachment"] = attach_note
        _rtext = result.get("text", "") or ""
        if attach_note or "감사보고서" in _rtext[:400] or "감사의견" in _rtext[:3000]:
            audit: dict[str, str] = {}
            m_aud = re.search(r"([가-힣A-Za-z]+회계법인)", _rtext)
            if m_aud:
                audit["감사인"] = m_aud.group(1)
            m_op = re.search(r"(적정|한정|부적정|의견\s*거절)", _rtext[:6000])
            if m_op:
                audit["감사의견"] = m_op.group(1).replace(" ", "")
            if audit:
                result["감사_요약"] = audit
                result["note_audit"] = "감사인·감사의견은 감사_요약(코드 추출)을 그대로 쓰세요."
        filing_meta = get_filing(doc_id)
        if filing_meta and filing_meta.get("doc_group") == "holding":
            head = doc.text[: doc.sections[0].offset] if doc.sections else doc.text[:1500]
            summary = _holding_summary(head)
            if summary:
                result["holding_summary"] = summary
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
