from __future__ import annotations

import asyncio
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

_sessions: dict[str, list[dict[str, Any]]] = {}

_ANSWER_CACHE_MAX = 500
_answer_cache: OrderedDict[tuple[str, str], AnswerResponse] = OrderedDict()
_answer_cache_lock = threading.Lock()



def _log_answer(record: dict[str, Any]) -> None:
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
def history_list(limit: int = Query(default=20, ge=1, le=1000)) -> list[dict[str, Any]]:
    return history_store.list_history(limit)


@router.get("/history/{item_id}", include_in_schema=False)
def history_detail(item_id: str) -> dict[str, Any]:
    rec = history_store.get_record(item_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="해당 기록이 없습니다")
    return rec


@router.delete("/history/{item_id}", include_in_schema=False)
def history_delete(item_id: str) -> dict[str, Any]:
    deleted = history_store.delete(item_id)
    if deleted:
        broadcaster.publish({"type": "history_changed"})
    return {"deleted": deleted}


@router.post("/history/delete", include_in_schema=False)
def history_delete_many(payload: dict[str, Any]) -> dict[str, Any]:
    ids = payload.get("ids") or []
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids는 배열이어야 합니다")
    removed = history_store.delete_many([str(i) for i in ids])
    _publish({"type": "history_changed"})
    return {"deleted": removed}


@router.delete("/history", include_in_schema=False)
def history_clear() -> dict[str, Any]:
    n = history_store.delete_all()
    if n:
        broadcaster.publish({"type": "history_changed"})
    return {"deleted": n}


@router.get("/usage", include_in_schema=False)
def usage_summary() -> dict[str, Any]:
    return usage_tracker.summary()


def _publish(event: dict[str, Any]) -> None:
    broadcaster.publish(event)


def _start_run(question_id: str, question: str) -> dict[str, Any]:
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
        except Exception as e:  
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
    question = (req.message or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question이 비어 있습니다")
    return _start_run(req.session_id or "Q-001", question)


@router.get("/runs/active", include_in_schema=False)
def active_runs() -> list[dict[str, Any]]:
    return registry.snapshot()


@router.get("/events", include_in_schema=False)
async def events_stream() -> StreamingResponse:
    async def generate():
        q = broadcaster.subscribe()
        if q is None:
            yield _sse({"type": "error", "text": "동시 접속 한도를 초과했습니다"})
            return
        try:
            yield _sse({"type": "hello", "subscribers": broadcaster.subscriber_count})
            idle = 0.0
            while True:
                try:
                    yield _sse(q.get_nowait())
                    idle = 0.0
                except queue.Empty:
                    await asyncio.sleep(0.25)
                    idle += 0.25
                    if idle >= 15.0:
                        yield ": keepalive\n\n"  
                        idle = 0.0
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
async def answer_stream(
    question_id: str = Query(default=""),
    question: str = Query(default=""),
) -> StreamingResponse:
    started_at = datetime.now(timezone.utc).isoformat()

    async def generate():
        events: queue.Queue = queue.Queue(maxsize=1000)
        box: dict[str, Any] = {}
        DONE = object()

        def on_event(ev: dict[str, Any]) -> None:
            try:
                events.put_nowait(ev)
            except queue.Full:
                pass

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
        idle = 0.0
        while True:
            try:
                ev = events.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.25)
                idle += 0.25
                if idle >= 10.0:
                    yield ": keepalive\n\n"  # 프록시가 유휴 연결을 끊지 않도록
                    idle = 0.0
                continue
            idle = 0.0
            if ev is DONE:
                break
            yield _sse(ev)

        outcome = box.get("outcome")
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
