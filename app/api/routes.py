from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.agent import clova
from app.agent.answer import FALLBACK_ANSWER, answer_question
from app.agent.loop import run_agent
from app.api.schemas import (
    AnswerResponse,
    ChatRequest,
    ChatResponse,
    Citation,
    CompanyInfo,
    FilingInfo,
)
from app.config import ANSWER_LOG_PATH
from app.data.manifest import get_filing, load_corpus, resolve_company, search_filings

logger = logging.getLogger(__name__)

router = APIRouter()

# v1: in-memory session store (session_id -> OpenAI-format message history).
# Lost on restart; move to Redis/DB before this needs to survive deploys.
_sessions: dict[str, list[dict[str, Any]]] = {}

# 평가자가 타임아웃 등으로 같은 질문을 재호출할 수 있으므로 성공 응답은 캐시한다.
# 실패(폴백) 응답은 캐시하지 않는다 — 재시도가 새 기회를 얻어야 하므로.
_ANSWER_CACHE_MAX = 500
_answer_cache: OrderedDict[tuple[str, str], AnswerResponse] = OrderedDict()
_answer_cache_lock = threading.Lock()
_answer_log_lock = threading.Lock()


def _log_answer(record: dict[str, Any]) -> None:
    """Best-effort JSONL audit log; must never break the response."""
    try:
        ANSWER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _answer_log_lock, ANSWER_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        logger.exception("failed to write answer log")


@router.get("/answer", response_model=AnswerResponse)
def answer(
    question_id: str = Query(default=""),
    question: str = Query(default=""),
) -> AnswerResponse:
    """평가용 엔드포인트. 어떤 내부 오류가 나도 200 + 유효한 JSON을 반환한다."""
    cache_key = (question_id, question)
    with _answer_cache_lock:
        cached = _answer_cache.get(cache_key)
    if cached is not None:
        return cached

    started_at = datetime.now(timezone.utc).isoformat()
    start = time.monotonic()
    try:
        outcome = answer_question(question)
        response = AnswerResponse(
            question_id=question_id,
            question=question,
            retrieved_context=outcome.retrieved_context,
            think_trace=outcome.think_trace,
            answer=outcome.answer,
        )
        ok, error = outcome.ok, outcome.error
    except Exception as e:  # noqa: BLE001 - the evaluator must always get valid JSON
        logger.exception("answer pipeline crashed for question_id=%r", question_id)
        response = AnswerResponse(
            question_id=question_id,
            question=question,
            retrieved_context="",
            think_trace=f"internal error: {type(e).__name__}",
            answer=FALLBACK_ANSWER,
        )
        ok, error = False, f"{type(e).__name__}: {e}"

    _log_answer(
        {
            "ts": started_at,
            "question_id": question_id,
            "question": question,
            "answer": response.answer,
            "think_trace": response.think_trace,
            "context_chars": len(response.retrieved_context),
            "ok": ok,
            "error": error,
            "duration_s": round(time.monotonic() - start, 2),
        }
    )

    if ok:
        with _answer_cache_lock:
            _answer_cache[cache_key] = response
            while len(_answer_cache) > _ANSWER_CACHE_MAX:
                _answer_cache.popitem(last=False)
    return response


def _sse(event: dict[str, Any]) -> str:
    try:
        payload = json.dumps(event, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = json.dumps({"type": "note", "level": "warn", "text": "이벤트 직렬화 실패"})
    return f"data: {payload}\n\n"


@router.get("/answer/stream", include_in_schema=False)
def answer_stream(
    question_id: str = Query(default=""),
    question: str = Query(default=""),
) -> StreamingResponse:
    """대시보드용 SSE 스트림. 에이전트가 일하는 과정(도구 호출·결과·토큰
    사용량)을 발생하는 대로 흘려보낸다. 평가용 엔드포인트는 /answer 이며
    이 경로는 그 동작에 영향을 주지 않는다."""

    def generate():
        events: queue.Queue = queue.Queue(maxsize=1000)
        box: dict[str, Any] = {}
        DONE = object()

        def on_event(ev: dict[str, Any]) -> None:
            try:
                events.put_nowait(ev)
            except queue.Full:
                pass  # UI 이벤트가 밀리면 버린다 — 답변 생성이 우선

        def work() -> None:
            started = time.monotonic()
            try:
                box["outcome"] = answer_question(question, on_event=on_event)
            except Exception as e:  # noqa: BLE001
                logger.exception("stream pipeline crashed")
                box["error"] = f"{type(e).__name__}: {e}"
            finally:
                box["elapsed"] = time.monotonic() - started
                events.put(DONE)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()

        yield _sse({"type": "start", "question_id": question_id, "question": question})
        while True:
            try:
                ev = events.get(timeout=1.0)
            except queue.Empty:
                yield ": keepalive\n\n"  # 프록시가 유휴 연결을 끊지 않도록
                continue
            if ev is DONE:
                break
            yield _sse(ev)

        outcome = box.get("outcome")
        if outcome is not None:
            yield _sse(
                {
                    "type": "done",
                    "question_id": question_id,
                    "question": question,
                    "answer": outcome.answer,
                    "retrieved_context": outcome.retrieved_context,
                    "think_trace": outcome.think_trace,
                    "ok": outcome.ok,
                    "elapsed": round(box.get("elapsed", 0.0), 1),
                }
            )
        else:
            yield _sse(
                {
                    "type": "done",
                    "question_id": question_id,
                    "question": question,
                    "answer": FALLBACK_ANSWER,
                    "retrieved_context": "",
                    "think_trace": box.get("error", "unknown error"),
                    "ok": False,
                    "elapsed": round(box.get("elapsed", 0.0), 1),
                }
            )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


def _extract_citations(tool_calls: list[dict[str, Any]]) -> list[Citation]:
    doc_ids: set[str] = set()
    for call in tool_calls:
        result = call.get("result")
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict) and item.get("doc_id"):
                doc_ids.add(item["doc_id"])

    citations = []
    for doc_id in sorted(doc_ids):
        filing = get_filing(doc_id)
        if filing is None:
            continue
        citations.append(
            Citation(
                doc_id=doc_id,
                report_nm=filing.get("report_nm"),
                corp_name=filing.get("corp_name"),
                rcept_dt=filing.get("rcept_dt"),
            )
        )
    return citations


@router.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    session_id = req.session_id or str(uuid.uuid4())
    history = _sessions.get(session_id, [])
    history = history + [{"role": "user", "content": req.message}]

    try:
        result = run_agent(history)
    except clova.ClovaConfigError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    _sessions[session_id] = result.history
    return ChatResponse(
        session_id=session_id,
        answer=result.answer,
        citations=_extract_citations(result.tool_calls),
        tool_calls=result.tool_calls,
    )


@router.get("/companies", response_model=list[CompanyInfo])
def list_companies(query: str | None = Query(default=None)) -> list[dict[str, Any]]:
    if query:
        return resolve_company(query)
    corpus = load_corpus()
    return corpus.universe.to_dict(orient="records")


@router.get("/filings", response_model=list[FilingInfo])
def list_filings(
    company: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    doc_group: str | None = None,
    doc_subtype: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    base_year: int | None = None,
    base_month: int | None = None,
    is_correction: bool | None = None,
    keyword: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return search_filings(
        company=company,
        sector=sector,
        industry=industry,
        doc_group=doc_group,
        doc_subtype=doc_subtype,
        date_from=date_from,
        date_to=date_to,
        base_year=base_year,
        base_month=base_month,
        is_correction=is_correction,
        keyword=keyword,
        limit=limit,
    )
