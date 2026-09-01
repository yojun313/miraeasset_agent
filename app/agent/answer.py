"""Single-question answer pipeline for the evaluation endpoint (GET /answer).

Wraps the agent loop with input sanitation, a wall-clock budget,
retrieved-context assembly from the tool-call log, and a think-trace string.

Contract: answer_question() returns a well-formed AnswerOutcome for every
expected failure mode (LLM down, budget exhausted, empty question). Only a
genuine bug escapes as an exception — and the API route has a final
catch-all so the evaluator still receives valid JSON.
"""

from __future__ import annotations

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
        f"(접수일 {filing.get('rcept_dt', '?')})"
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
            key = (doc_id, inp.get("section_title"), inp.get("filename"))
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

        elif tool == "search_filings" and isinstance(result, list):
            for row in result:
                if not (isinstance(row, dict) and row.get("doc_id")):
                    continue
                doc_id = str(row["doc_id"])
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
                filings.append(
                    f"- [{doc_id}] {row.get('corp_name', '')} {row.get('report_nm', '')} "
                    f"(접수일 {row.get('rcept_dt', '?')})"
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
    try:
        result = run_agent([{"role": "user", "content": q}], deadline=deadline, on_event=on_event)
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

    return AnswerOutcome(
        answer=result.answer,
        retrieved_context=build_retrieved_context(result.tool_calls),
        think_trace="\n".join(result.trace),
        ok=True,
        duration_s=time.monotonic() - start,
    )
