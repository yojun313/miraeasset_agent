from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = Field(default=None, description="omit to start a new session")


class Citation(BaseModel):
    doc_id: str
    report_nm: str | None = None
    corp_name: str | None = None
    rcept_dt: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    answer: str
    citations: list[Citation]
    tool_calls: list[dict[str, Any]]


class AnswerResponse(BaseModel):
    """평가용 GET /answer 응답 — 주최측 요구 스키마 그대로."""

    question_id: str
    question: str
    retrieved_context: str
    think_trace: str
    answer: str


class CompanyInfo(BaseModel):
    corp_code: str
    stock_code: str
    corp_name: str
    listed_name: str
    sector: str
    industry: str
    market_cap: int | None = None


class FilingInfo(BaseModel):
    doc_id: str
    corp_name: str
    listed_name: str
    sector: str
    doc_group: str
    doc_subtype: str | None = None  # major 공시는 세부 유형이 없다
    report_nm: str
    rcept_no: str
    rcept_dt: str
    base_year: int | None = None  # 정기공시가 다루는 회계연도 (접수일과 다름)
    base_month: int | None = None
    is_correction: bool
    file_path: str
    file_format: str
    n_files: int
