from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.agent.loop import run_agent
from app.api.schemas import ChatRequest, ChatResponse, Citation, CompanyInfo, FilingInfo
from app.data.manifest import get_filing, load_corpus, resolve_company, search_filings

router = APIRouter()

# v1: in-memory session store (session_id -> Anthropic message history).
# Lost on restart; move to Redis/DB before this needs to survive deploys.
_sessions: dict[str, list[dict[str, Any]]] = {}


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
        answer, updated_history, tool_calls = run_agent(history)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    _sessions[session_id] = updated_history
    return ChatResponse(
        session_id=session_id,
        answer=answer,
        citations=_extract_citations(tool_calls),
        tool_calls=tool_calls,
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
        is_correction=is_correction,
        keyword=keyword,
        limit=limit,
    )
