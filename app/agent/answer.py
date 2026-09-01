"""Single-question answer pipeline for the evaluation endpoint (GET /answer).

Wraps the agent loop with input sanitation, a wall-clock budget,
retrieved-context assembly from the tool-call log, and a think-trace string.

Contract: answer_question() returns a well-formed AnswerOutcome for every
expected failure mode (LLM down, budget exhausted, empty question). Only a
genuine bug escapes as an exception — and the API route has a final
catch-all so the evaluator still receives valid JSON.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from app.agent import clova
from app.agent.loop import run_agent
from app.config import ANSWER_CONTEXT_MAX_CHARS, ANSWER_TIME_BUDGET
from app.data.manifest import get_filing

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 4_000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 외부 정보원 안내는 시스템 프롬프트로 금지했지만, 강제 마무리 경로처럼
# 모델이 궁지에 몰리면 여전히 새어 나온다. 마지막 방어선으로 문장 단위로 걷어낸다.
# 오탐을 막기 위해 "외부 출처"와 "권유 동사"가 같은 문장에 있을 때만 제거한다.
_EXTERNAL_SOURCE = re.compile(
    r"네이버\s*(금융|증권)|다음\s*금융|구글|뉴스\s*기사|증권사?\s*(리포트|정보)"
    r"|금융\s*(정보\s*)?(플랫폼|사이트|서비스)|DART\s*(홈페이지|웹\s*사이트|사이트|전자공시|웹)"
    r"|전자공시\s*시스템|공식\s*홈페이지|IR\s*자료"
)
_REFERRAL_VERB = re.compile(
    r"참고|참조|확인(해\s*(보시|주시|보세)|할\s*수\s*있|하실\s*수|이\s*가능)"
    r"|조회(해\s*(보시|주시)|할\s*수\s*있|하실\s*수)|방문|검색해\s*(보시|주시)"
    r"|이용(해\s*(보시|주시)|하시)|권(장|해)"
)


def strip_external_referrals(answer: str) -> str:
    """답변에서 '외부 사이트에서 확인하세요' 류 문장을 제거한다."""
    if not answer:
        return answer
    kept: list[str] = []
    for line in answer.split("\n"):
        # 한 줄 안에서도 문장 단위로 판단 (문장부호 뒤에서 분리)
        pieces = re.split(r"(?<=[.!?。])\s+", line)
        surviving = [
            p for p in pieces
            if not (_EXTERNAL_SOURCE.search(p) and _REFERRAL_VERB.search(p))
        ]
        if len(surviving) != len(pieces) and not "".join(surviving).strip():
            continue  # 줄 전체가 안내문이었으면 줄째로 버린다
        kept.append(" ".join(surviving).rstrip() if surviving else "")
    cleaned = "\n".join(kept)
    # 제거 결과로 생긴 꼬리 빈 줄 정리
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

# --- 검색 힌트 라우터 -----------------------------------------------------
#
# HCX-007은 시스템 프롬프트의 검색 규칙을 자주 무시한다. 실제 사례: "3월 20일
# 결정한 유상증자" 질문에 엉뚱한 doc_subtype(투자판단관련주요경영사항)과
# date_to 없는 date_from을 걸어 12월 공시를 읽고 "확인 불가"로 답했다.
# 질문 키워드에서 결정론적으로 올바른 검색 경로를 뽑아 힌트로 주입한다.
_TOPIC_ROUTES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("유상증자", "무상증자", "제3자배정", "주주배정"),
     "search_filings(doc_group='major', keyword='유상증자') — 유상증자는 주요사항보고서입니다"),
    (("전환사채", "CB"),
     "search_filings(doc_group='major', keyword='전환사채')"),
    (("교환사채", "EB"),
     "search_filings(doc_group='major', keyword='교환사채')"),
    (("신주인수권부사채", "BW"),
     "search_filings(doc_group='major', keyword='신주인수권')"),
    (("자기주식", "자사주"),
     "search_filings(doc_group='major', keyword='자기주식')"),
    (("합병",), "search_filings(doc_group='major', keyword='합병')"),
    (("분할",), "search_filings(doc_group='major', keyword='분할')"),
    (("해지",),
     "search_filings(doc_group='exchange', doc_subtype='단일판매공급계약해지') — 해지 20건뿐이니 체결 공시와 대조하세요"),
    (("공급계약", "수주", "계약"),
     "search_filings(doc_group='exchange', doc_subtype='단일판매공급계약체결')"),
    (("시설투자", "설비투자"),
     "search_filings(doc_group='exchange', doc_subtype='신규시설투자등')"),
    (("대량보유", "5%", "지분 변동", "지분변동", "보유비율", "지분"),
     "지분공시는 발행회사 기준으로 저장됩니다. 'A가 B 지분을 보고' 질문은 "
     "search_filings(doc_group='holding', company=B(발행회사), filer=A(보고자)) 로 검색하세요. "
     "보고자는 유니버스 밖 주체(지주사·국민연금 등)일 수 있으며 filer로만 찾을 수 있습니다."),
    (("사업보고서", "반기보고서", "분기보고서", "매출", "영업이익", "순이익", "실적", "재무"),
     "search_filings(doc_group='periodic', base_year=<질문의 회계연도>) — 접수일 필터 금지. "
     "매출액·영업이익·순이익 같은 핵심 수치는 read_section('1. 요약재무정보') 에 "
     "당기/전기가 함께 정리돼 있어 비교 질문에 가장 효율적입니다."),
)

_DATE_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
_PERIOD_RE = re.compile(r"(\d{4})\s*년\s*(상반기|하반기|([1-4])\s*분기)")
_PERIOD_RANGES = {"상반기": ("0101", "0630"), "하반기": ("0701", "1231"),
                  "1": ("0101", "0331"), "2": ("0401", "0630"),
                  "3": ("0701", "0930"), "4": ("1001", "1231")}


def build_search_hint(question: str) -> str | None:
    """질문에서 뽑은 결정론적 검색 힌트. 해당 없으면 None."""
    hints: list[str] = []
    for keywords, route in _TOPIC_ROUTES:
        if any(k in question for k in keywords):
            hints.append(route)
            if len(hints) >= 2:
                break

    if any(k in question for k in ("가장 이른", "최초", "처음으로", "가장 오래된", "첫 ")):
        hints.append("오래된 순으로 보려면 search_filings(order='asc') 를 사용하세요.")
    if any(k in question for k in ("코스닥", "KOSDAQ")):
        hints.append("search_filings(market='KOSDAQ') 로 시장을 필터링할 수 있습니다.")
    elif any(k in question for k in ("코스피", "KOSPI")):
        hints.append("search_filings(market='KOSPI') 로 시장을 필터링할 수 있습니다.")

    pm = _PERIOD_RE.search(question)
    if pm:
        year = pm.group(1)
        key = pm.group(3) or pm.group(2)
        lo, hi = _PERIOD_RANGES[key]
        hints.append(
            f"'{pm.group(0)}'는 수시공시(주요사항·거래소·지분)라면 접수일 "
            f"date_from={year}{lo}, date_to={year}{hi} 로 지정하세요 "
            "(base_month는 정기공시 전용이므로 쓰지 마세요)."
        )

    m = _DATE_RE.search(question)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        try:
            base = datetime.date(y, mo, d)
            until = base + datetime.timedelta(days=7)
            hints.append(
                f"질문의 날짜 {base:%Y-%m-%d}는 결정(이사회)일입니다. 공시 접수는 그날부터 "
                f"수일 내이므로 date_from={base:%Y%m%d}, date_to={until:%Y%m%d} 로 "
                "양쪽을 모두 지정하세요 (date_to를 빼면 몇 달 뒤 공시가 잡힙니다)."
            )
        except ValueError:
            pass

    if not hints:
        return None
    return "[검색 힌트 — 시스템 자동 생성]\n" + "\n".join(f"- {h}" for h in hints)


FALLBACK_ANSWER = (
    "죄송합니다. 일시적인 시스템 문제로 이 질의를 처리하지 못했습니다. "
    "현재로서는 제공된 공시 데이터에서 확인할 수 없습니다."
)

EMPTY_QUESTION_ANSWER = (
    "질문이 비어 있습니다. 조회할 기업명과 궁금한 내용을 함께 질문해 주세요. "
    "예: \"삼성전자의 2025년 사업보고서 기준 주요 사업 내용을 알려줘\""
)


@dataclass
class AnswerOutcome:
    answer: str
    retrieved_context: str
    think_trace: str
    ok: bool
    error: str | None = None
    duration_s: float = 0.0


def sanitize_question(question: str) -> str:
    q = _CONTROL_CHARS.sub(" ", question or "").strip()
    if len(q) > _MAX_QUESTION_CHARS:
        q = q[:_MAX_QUESTION_CHARS]
    return q


def _fmt_date(raw: object) -> str:
    """20231205 → 2023-12-05. 모델이 8자리 날짜를 직접 변환하다 연도를 틀리는
    일이 있어(2025→2024로 옮겨 적음) 사람이 읽는 형식을 미리 만들어 준다."""
    s = str(raw or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _doc_header(doc_id: str) -> str:
    try:
        filing = get_filing(doc_id)
    except Exception:  # noqa: BLE001 - a header lookup must never break assembly
        logger.exception("get_filing failed for %s", doc_id)
        filing = None
    if not filing:
        return f"[{doc_id}]"
    return (
        f"[{doc_id}] {filing.get('corp_name', '')} — {filing.get('report_nm', '')} "
        f"(접수일 {_fmt_date(filing.get('rcept_dt', '?'))})"
    )


def build_retrieved_context(
    tool_calls: list[dict], max_chars: int = ANSWER_CONTEXT_MAX_CHARS
) -> str:
    """Assembles the evidence the agent actually consulted, most substantial
    first: read_section bodies, then fulltext snippets, then metadata hits."""
    bodies: list[str] = []
    snippets: list[str] = []
    filings: list[str] = []
    seen_bodies: set[tuple] = set()
    seen_snippets: set[str] = set()
    seen_docs: set[str] = set()

    for call in tool_calls:
        tool = call.get("tool")
        result = call.get("result")
        inp = call.get("input") or {}

        if tool == "read_section" and isinstance(result, dict) and isinstance(result.get("text"), str):
            doc_id = str(result.get("doc_id") or inp.get("doc_id") or "?")
            # 거래소공시는 섹션 제목이 달라도 같은 전문이 돌아온다. 제목이 아니라
            # 본문 내용으로 중복을 걸러야 같은 텍스트가 근거에 3번 실리지 않는다.
            key = (doc_id, inp.get("filename"), hash(result["text"]))
            if key in seen_bodies:
                continue
            seen_bodies.add(key)
            section = str(inp.get("section_title") or "")
            bodies.append(f"### {_doc_header(doc_id)} / 섹션: {section}\n{result['text']}")

        elif tool == "search_fulltext" and isinstance(result, list):
            for row in result:
                if not (isinstance(row, dict) and row.get("snippet") and row.get("doc_id")):
                    continue
                line = f"- {_doc_header(str(row['doc_id']))}: {row['snippet']}"
                if line not in seen_snippets:
                    seen_snippets.add(line)
                    snippets.append(line)

        elif tool == "search_filings":
            rows = result.get("filings") if isinstance(result, dict) else result
            if isinstance(result, dict) and result.get("note"):
                line = f"- (집계) {result['note']}"
                if line not in seen_snippets:
                    seen_snippets.add(line)
                    filings.append(line)
            for row in rows if isinstance(rows, list) else []:
                if not (isinstance(row, dict) and row.get("doc_id")):
                    continue
                doc_id = str(row["doc_id"])
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
                filings.append(
                    f"- [{doc_id}] {row.get('corp_name', '')} {row.get('report_nm', '')} "
                    f"(접수일 {_fmt_date(row.get('rcept_dt', '?'))})"
                )

    parts: list[str] = []
    if bodies:
        parts.append("## 열람한 공시 본문\n" + "\n\n".join(bodies))
    if snippets:
        parts.append("## 키워드 검색 결과 발췌\n" + "\n".join(snippets[:40]))
    if filings:
        parts.append("## 검색된 공시 목록\n" + "\n".join(filings[:60]))

    context = "\n\n".join(parts)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...(길이 제한으로 잘림)"
    return context


def answer_question(question: str, on_event: Callable[[dict], None] | None = None) -> AnswerOutcome:
    """질문 하나를 처리한다. `on_event`를 주면 진행 상황(도구 호출, 토큰 사용량
    등)을 실시간으로 받아볼 수 있다 — 평가용 /answer는 쓰지 않고, 대시보드
    스트리밍이 사용한다."""
    start = time.monotonic()
    q = sanitize_question(question)
    if not q:
        return AnswerOutcome(
            answer=EMPTY_QUESTION_ANSWER,
            retrieved_context="",
            think_trace="입력 검증: 빈 질문",
            ok=True,
        )

    deadline = start + ANSWER_TIME_BUDGET
    hint = build_search_hint(q)
    user_content = q if hint is None else f"{q}\n\n{hint}"
    try:
        result = run_agent(
            [{"role": "user", "content": user_content}], deadline=deadline, on_event=on_event
        )
    except clova.ClovaError as e:
        # ClovaConfigError(키 미설정)를 포함, LLM을 아예 못 쓰는 상황의 폴백.
        logger.error("answer pipeline LLM failure: %s", e)
        return AnswerOutcome(
            answer=FALLBACK_ANSWER,
            retrieved_context="",
            think_trace=f"LLM 호출 실패: {type(e).__name__}: {e}",
            ok=False,
            error=str(e),
            duration_s=time.monotonic() - start,
        )

    answer = strip_external_referrals(result.answer)
    if not answer.strip():  # 필터가 전부 걷어낸 극단적 경우
        answer = result.answer
    return AnswerOutcome(
        answer=answer,
        retrieved_context=build_retrieved_context(result.tool_calls),
        think_trace="\n".join(result.trace),
        ok=True,
        duration_s=time.monotonic() - start,
    )
