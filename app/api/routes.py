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
from app.agent import usage as usage_tracker
from app.api import history as history_store
from app.api.events import broadcaster, registry
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



def _log_answer(record: dict[str, Any]) -> None:
    """Best-effort JSONL audit log; must never break the response."""
    try:
        ANSWER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with history_store.LOG_LOCK, ANSWER_LOG_PATH.open("a", encoding="utf-8") as f:
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
            "retrieved_context": response.retrieved_context,
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


@router.get("/history", include_in_schema=False)
def history_list(limit: int = Query(default=20, ge=1, le=100)) -> list[dict[str, Any]]:
    """서버에 기록된 질의 히스토리 — 어느 기기에서 접속해도 같은 목록."""
    return history_store.list_history(limit)


@router.get("/history/{item_id}", include_in_schema=False)
def history_detail(item_id: str) -> dict[str, Any]:
    rec = history_store.get_record(item_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="해당 기록이 없습니다")
    return rec


@router.delete("/history/{item_id}", include_in_schema=False)
def history_delete(item_id: str) -> dict[str, Any]:
    """로그 파일에서 해당 기록을 영구 삭제한다."""
    deleted = history_store.delete(item_id)
    if deleted:
        broadcaster.publish({"type": "history_changed"})
    return {"deleted": deleted}


@router.delete("/history", include_in_schema=False)
def history_clear() -> dict[str, Any]:
    """모든 질의 기록을 영구 삭제한다."""
    n = history_store.delete_all()
    if n:
        broadcaster.publish({"type": "history_changed"})
    return {"deleted": n}


@router.get("/usage", include_in_schema=False)
def usage_summary() -> dict[str, Any]:
    """이 서버가 호출한 CLOVA API 사용량 누적치.

    CLOVA API 키로는 요금·크레딧 잔액을 조회할 수 없어(NCP 콘솔 또는 별도
    자격증명이 필요한 Cost and Usage API가 필요) 응답의 usage 필드를 직접
    합산한 값이다."""
    return usage_tracker.summary()


def _publish(event: dict[str, Any]) -> None:
    """모든 대시보드 클라이언트에 이벤트를 뿌린다."""
    broadcaster.publish(event)


def _start_run(question_id: str, question: str) -> dict[str, Any]:
    """질의를 서버 백그라운드에서 실행한다. 진행 상황은 /events 로 방송되고
    실행 상태는 레지스트리에 남아, 나중에 접속한 브라우저도 따라잡을 수 있다."""
    run = registry.create(question_id, question)
    run_id = run["run_id"]

    def on_event(ev: dict[str, Any]) -> None:
        payload = {"type": "run_event", "run_id": run_id, "event": ev}
        registry.add_event(run_id, ev)
        _publish(payload)

    def work() -> None:
        started = time.monotonic()
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            outcome = answer_question(question, on_event=on_event)
            result = {
                "question_id": question_id,
                "question": question,
                "answer": outcome.answer,
                "retrieved_context": outcome.retrieved_context,
                "think_trace": outcome.think_trace,
                "ok": outcome.ok,
            }
            error = outcome.error
        except Exception as e:  # noqa: BLE001 - 실행 스레드가 조용히 죽으면 안 된다
            logger.exception("run %s crashed", run_id)
            result = {
                "question_id": question_id,
                "question": question,
                "answer": FALLBACK_ANSWER,
                "retrieved_context": "",
                "think_trace": f"internal error: {type(e).__name__}",
                "ok": False,
            }
            error = f"{type(e).__name__}: {e}"

        elapsed = round(time.monotonic() - started, 1)
        result["elapsed"] = elapsed
        registry.finish(run_id, result)
        _log_answer({
            "ts": started_at,
            "endpoint": "dashboard",
            "question_id": question_id,
            "question": question,
            "answer": result["answer"],
            "think_trace": result["think_trace"],
            "retrieved_context": result["retrieved_context"],
            "context_chars": len(result["retrieved_context"]),
            "ok": result["ok"],
            "error": error,
            "duration_s": elapsed,
        })
        # 완료 알림 → 모든 브라우저가 결과를 그리고 히스토리를 갱신한다
        _publish({"type": "run_done", "run_id": run_id, "result": result})
        _publish({"type": "history_changed"})

    threading.Thread(target=work, daemon=True).start()
    _publish({
        "type": "run_started",
        "run_id": run_id,
        "question_id": question_id,
        "question": question,
        "started_at": run["started_at"],
    })
    return {"run_id": run_id}


@router.post("/runs", include_in_schema=False)
def start_run(req: ChatRequest) -> dict[str, Any]:
    """대시보드 질의 시작. 진행 상황은 /events 스트림으로 전달된다."""
    question = (req.message or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question이 비어 있습니다")
    return _start_run(req.session_id or "Q-001", question)


@router.get("/runs/active", include_in_schema=False)
def active_runs() -> list[dict[str, Any]]:
    """진행 중·최근 완료된 실행. 새로 접속한 브라우저가 현재 상태를 따라잡는다."""
    return registry.snapshot()


@router.get("/events", include_in_schema=False)
def events_stream() -> StreamingResponse:
    """모든 대시보드가 구독하는 공용 이벤트 스트림."""

    def generate():
        q = broadcaster.subscribe()
        if q is None:
            yield _sse({"type": "error", "text": "동시 접속 한도를 초과했습니다"})
            return
        try:
            yield _sse({"type": "hello", "subscribers": broadcaster.subscriber_count})
            while True:
                try:
                    yield _sse(q.get(timeout=15.0))
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            broadcaster.unsubscribe(q)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


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

    started_at = datetime.now(timezone.utc).isoformat()

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
        # 대시보드 실행도 평가용 /answer 와 동일하게 감사 로그에 남긴다
        _log_answer(
            {
                "ts": started_at,
                "endpoint": "stream",
                "question_id": question_id,
                "question": question,
                "answer": outcome.answer if outcome else FALLBACK_ANSWER,
                "think_trace": outcome.think_trace if outcome else box.get("error", ""),
                "retrieved_context": outcome.retrieved_context if outcome else "",
                "context_chars": len(outcome.retrieved_context) if outcome else 0,
                "ok": bool(outcome and outcome.ok),
                "error": box.get("error"),
                "duration_s": round(box.get("elapsed", 0.0), 2),
            }
        )
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
        if isinstance(result, dict) and isinstance(result.get("filings"), list):
            items = result["filings"]
        elif isinstance(result, list):
            items = result
        else:
            items = [result]
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
    filer: str | None = None,
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
        filer=filer,
        date_from=date_from,
        date_to=date_to,
        base_year=base_year,
        base_month=base_month,
        is_correction=is_correction,
        keyword=keyword,
        limit=limit,
    )
