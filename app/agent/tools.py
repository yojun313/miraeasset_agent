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
)


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
        ("keyword", ("keyword",), "keyword를 빼면"),
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
    """계약금액이 뽑힌 행들의 연도별·전체 합계를 계산한다."""
    by_year: dict[str, int] = {}
    counts: dict[str, int] = {}
    total = 0
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
    if not by_year:
        return {}
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
    ("증자전주식총수", re.compile(r"증자전 발행주식총수 \(주\) \| 보통주식 \(주\) \| ([\d,]+)"), False),
    ("시설자금", re.compile(r"시설자금 \(원\) \| ([\d,]+)"), True),
    ("타법인취득자금", re.compile(r"타법인 증권취득자금 \(원\) \| ([\d,]+)"), True),
    ("운영자금", re.compile(r"운영자금 \(원\) \| ([\d,]+)"), True),
    ("채무상환자금", re.compile(r"채무상환자금 \(원\) \| ([\d,]+)"), True),
    ("예정발행가", re.compile(r"예정발행가 \| 보통주식 \(원\) \| ([\d,]+)"), False),
    ("사채총액", re.compile(r"사채의 권면\(전자등록\)총액 \(원\) \| ([\d,]+)"), True),
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


def dispatch(name: str, tool_input: dict[str, Any]) -> Any:
    if name == "get_company_info":
        query = _as_str(tool_input, "query")
        if not query:
            return {"error": "query가 비어 있습니다. 조회할 기업명이나 6자리 종목코드를 넣으세요."}
        matches = resolve_company(query)
        if not matches:
            sector_hit = resolve_sector(query)
            if sector_hit:
                sector_hit["note"] = (
                    f"'{query}' 를 {sector_hit['kind']} '{sector_hit['matched']}' 로 해석해 "
                    f"소속 기업 {sector_hit['count']}곳을 반환합니다. 이후 도구에는 각 "
                    "corp_name을 쓰거나 sector 필터를 사용하세요."
                )
                return sector_hit
        return matches or {
            "error": (
                f"{query!r} 와 일치하는 기업이 없습니다. 제공된 70개사 유니버스에 "
                "없는 기업일 수 있습니다 (그럴 경우 답변에서 범위 밖임을 알리세요)."
            )
        }

    if name == "search_filings":
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
            or (_as_str(tool_input, "keyword")
                and not any(k in _as_str(tool_input, "keyword") for k in _periodic_kw))
        )
        if (group in ("major", "exchange", "holding") or (group is None and event_signal)) and (
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
        if group_by:
            try:
                # 비율 순위를 정확히 주려면 limit 이전의 넓은 집합이 필요하다.
                # 모델이 limit=3 을 걸면 '건수 상위 3곳'만 보고 그 안에서 비율을
                # 매기게 되어 실제 비율 순위와 어긋난다.
                wide = _aggregate_filings(
                    group_by,
                    company=company_arg or None,
                    sector=sector_arg,
                    industry=tool_input.get("industry"),
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
            groups = wide[: _as_int(tool_input.get("limit")) or 30]
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
                    industry=tool_input.get("industry"),
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

            ranked = sorted(
                (g for g in wide if g["count"] >= ratio_floor),
                key=lambda g: -g["correction_pct"],
            )[:5]
            result: dict[str, Any] = {
                "group_by": group_by,
                "group_count": len(wide),
                "total_count": sum(g["count"] for g in wide),
                "total_correction_count": sum(g.get("correction_count", 0) for g in wide),
                "groups": groups,
                "top_by_correction_pct": [
                    {k: g[k] for k in ("group_key", "count", "correction_count", "correction_pct")}
                    for g in ranked
                ],
                "ratio_min_sample": ratio_floor,
                "note": (
                    "조건 전체 건수는 total_count, 그룹(기업) 수는 group_count 입니다 — "
                    "groups의 건수를 직접 합산하지 마세요. "
                    f"groups는 건수 순, top_by_correction_pct는 정정 비율 순입니다"
                    f"(표본 {ratio_floor}건 이상만). "
                    "비율을 묻는 질문이면 top_by_correction_pct 를, 건수를 묻는 질문이면 "
                    "groups 를 쓰세요. 값을 직접 세거나 계산하지 마세요."
                ),
            }
            if amount_summary:
                result["amount_totals"] = amount_summary
                result["note"] += " 연도별 금액 합계는 amount_totals 에 있습니다."
            if ratio_summary:
                result["ratio_trend"] = ratio_summary
                result["note"] += " 보유비율 추세는 ratio_trend 에 있습니다."
            try:
                bounds = dict(
                    company=company_arg or None, sector=sector_arg,
                    industry=tool_input.get("industry"),
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

        results = _search_filings(
            company=company_arg or None,
            sector=sector_arg,
            industry=tool_input.get("industry"),
            doc_group=tool_input.get("doc_group"),
            doc_subtype=tool_input.get("doc_subtype"),
            filer=_as_str(tool_input, "filer") or None,
            market=_as_str(tool_input, "market") or None,
            date_from=date_from,
            date_to=date_to,
            base_year=base_year,
            base_month=base_month,
            is_correction=tool_input.get("is_correction"),
            keyword=tool_input.get("keyword"),
            order=_as_str(tool_input, "order") or "desc",
            limit=requested_limit,
        )
        auto_widened = False
        if explicit_limit is None and len(results) >= requested_limit:
            widened = _search_filings(
                company=company_arg or None,
                sector=sector_arg,
                industry=tool_input.get("industry"),
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
                    if hs.get("보고사유"):
                        r["보고사유"] = hs["보고사유"][:120]
            _link_corrections_to_originals(results)
            for r in results:
                d = str(r.get("rcept_dt") or "")
                if len(d) == 8 and d.isdigit():
                    # 모델이 8자리 날짜를 옮겨 적다 연도를 틀리므로 완성형을 준다
                    r["rcept_date"] = f"{d[:4]}-{d[4:6]}-{d[6:]}"
            # 금액이 뽑힌 계약공시라면 연도별 합계까지 코드가 계산해 준다.
            # 모델이 20여 건을 암산으로 더하다 총액이 구성 항목보다 작아지는
            # 오답이 실제로 나왔다 — 합산도 계산이므로 맡기지 않는다.
            amount_summary = _amount_totals(results)
            major_field_names = ("신주수", "증자전주식총수", "증자비율", "시설자금",
                                 "타법인취득자금", "운영자금", "채무상환자금",
                                 "예정발행가", "사채총액", "이사회결의일")
            change_log = (
                _field_change_log(results, major_field_names)
                if group == "major" and any(any(r.get(f) for f in major_field_names) for r in results)
                else []
            )
            corrections = sum(1 for r in results if r.get("is_correction"))
            companies = sorted({str(r.get("corp_name") or "") for r in results} - {""})
            summary = {
                "count": len(results),
                "correction_count": corrections,
                "original_count": len(results) - corrections,
                "company_count": len(companies),
                "companies": companies,
                "note": (
                    f"총 {len(results)}건 = 원본 {len(results) - corrections}건 + "
                    f"[기재정정] {corrections}건, 기업 {len(companies)}곳. "
                    "건수·기업 수를 답할 때는 이 집계값을 그대로 사용하세요 "
                    "— 목록을 직접 세지 마세요."
                    + (" 정정이 여럿이면 접수번호가 큰 쪽이 더 나중의(최신) 정정입니다"
                       " — 같은 접수일이어도 마찬가지입니다." if corrections >= 2 else "")
                    + (
                        f" (기본 20건에서 잘려 자동으로 {len(results)}건까지 넓혔습니다)"
                        if auto_widened
                        else ""
                    )
                    + (
                        f" ⚠ 결과가 limit={requested_limit}에서 잘렸습니다. 실제로는 더 많으니 "
                        "limit을 늘리거나 group_by로 집계하세요."
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
            summary["filings"] = results
            return summary
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
        # 지분공시의 보고사유(인적분할, 장내매수 등)는 어떤 섹션에도 속하지 않는
        # 문서 머리 요약정보에만 있다. 섹션만 읽으면 "왜 변동했는지"를 놓치므로
        # 어떤 섹션을 읽든 요약을 함께 실어준다.
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
