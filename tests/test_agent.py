"""Agent-loop and /answer endpoint tests with a mocked CLOVA client.

No network, no API key needed: clova.chat_completion is monkeypatched.
Tool dispatch runs for real against the local corpus (like test_parser.py).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app.agent import clova
from app.agent.answer import build_retrieved_context
from app.agent.loop import run_agent
from app.agent.tools import TOOLS, openai_tool_specs
from app.config import MAX_AGENT_TURNS


def _assistant(content: str | None = None, tool_calls: list | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        msg["tool_calls"] = tool_calls
    return msg


def _tool_call(name: str, arguments: Any, call_id: str = "call_1") -> dict[str, Any]:
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}


def _mock_sequence(monkeypatch, responses: list[Any]):
    """chat_completion returns/raises each item in order; records every call."""
    calls: list[dict[str, Any]] = []

    def fake(messages, *, tools=None, deadline=None, **kwargs):
        calls.append({"messages": messages, "tools": tools})
        i = min(len(calls) - 1, len(responses) - 1)
        item = responses[i]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(clova, "chat_completion", fake)
    return calls


# --- tool spec conversion -------------------------------------------------


def test_openai_tool_specs_shape():
    specs = openai_tool_specs()
    assert len(specs) == len(TOOLS)
    for spec in specs:
        assert spec["type"] == "function"
        fn = spec["function"]
        assert fn["name"] and fn["description"]
        assert fn["parameters"]["type"] == "object"


# --- run_agent ------------------------------------------------------------


def test_direct_answer_no_tools(monkeypatch):
    _mock_sequence(monkeypatch, [_assistant("최종 답변입니다.")])
    result = run_agent([{"role": "user", "content": "안녕"}])
    assert result.answer == "최종 답변입니다."
    assert result.completed is True
    assert result.tool_calls == []
    assert result.history[-1] == {"role": "assistant", "content": "최종 답변입니다."}


def test_tool_call_flow(monkeypatch):
    calls = _mock_sequence(
        monkeypatch,
        [
            _assistant(None, [_tool_call("get_company_info", {"query": "삼성전자"})]),
            _assistant("삼성전자는 반도체·전자부품 섹터입니다."),
        ],
    )
    result = run_agent([{"role": "user", "content": "삼성전자 섹터는?"}])
    assert result.completed is True
    assert "반도체" in result.answer
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["tool"] == "get_company_info"
    # 실제 dispatch가 돌아서 유니버스 데이터가 나왔는지
    assert result.tool_calls[0]["result"][0]["corp_name"] == "삼성전자"
    # 2번째 요청에는 assistant(tool_calls) + tool 결과 메시지가 순서대로 들어가야 한다
    second_call_messages = calls[1]["messages"]
    roles = [m["role"] for m in second_call_messages]
    assert roles[-2:] == ["assistant", "tool"]
    assert second_call_messages[-1]["tool_call_id"] == "call_1"
    assert any("도구 호출: get_company_info" in line for line in result.trace)


def test_invalid_json_arguments_fed_back(monkeypatch):
    _mock_sequence(
        monkeypatch,
        [
            _assistant(None, [_tool_call("get_company_info", "{broken json")]),
            _assistant("답변"),
        ],
    )
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.answer == "답변"
    assert "error" in result.tool_calls[0]["result"]
    assert "JSON" in result.tool_calls[0]["result"]["error"]


def test_unknown_tool_name(monkeypatch):
    _mock_sequence(
        monkeypatch,
        [
            _assistant(None, [_tool_call("no_such_tool", {})]),
            _assistant("답변"),
        ],
    )
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.answer == "답변"
    assert "error" in result.tool_calls[0]["result"]


def test_camelcase_toolcalls_and_object_arguments(monkeypatch):
    """네이티브 v3 스타일(toolCalls camelCase, arguments가 dict)도 수용해야 한다."""
    _mock_sequence(
        monkeypatch,
        [
            {
                "role": "assistant",
                "content": None,
                "toolCalls": [
                    {
                        "id": "call_native",
                        "type": "function",
                        "function": {"name": "get_company_info", "arguments": {"query": "삼성전자"}},
                    }
                ],
            },
            _assistant("답변"),
        ],
    )
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.answer == "답변"
    assert result.tool_calls[0]["result"][0]["corp_name"] == "삼성전자"


def test_empty_response_then_answer(monkeypatch):
    _mock_sequence(monkeypatch, [_assistant(""), _assistant("답변")])
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.answer == "답변"


def test_turn_budget_forces_final_answer(monkeypatch):
    def fake(messages, *, tools=None, deadline=None, **kwargs):
        if tools is None:  # 강제 마무리 호출 (no-tools)
            return _assistant("수집된 정보 기반 요약 답변")
        return _assistant(None, [_tool_call("get_company_info", {"query": "삼성전자"})])

    monkeypatch.setattr(clova, "chat_completion", fake)
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.completed is False
    assert result.answer == "수집된 정보 기반 요약 답변"
    assert len(result.tool_calls) == MAX_AGENT_TURNS


def test_api_error_midloop_degrades_to_fallback(monkeypatch):
    _mock_sequence(monkeypatch, [clova.ClovaAPIError("HTTP 500")])
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.completed is False
    assert result.answer  # 정적 폴백이라도 비어있으면 안 된다


def test_config_error_propagates(monkeypatch):
    _mock_sequence(monkeypatch, [clova.ClovaConfigError("no key")])
    with pytest.raises(clova.ClovaConfigError):
        run_agent([{"role": "user", "content": "q"}])


# --- retrieved_context assembly ------------------------------------------


def test_build_retrieved_context_sections_and_dedup():
    tool_calls = [
        {
            "tool": "search_filings",
            "input": {},
            "result": [
                {"doc_id": "d1", "corp_name": "삼성전자", "report_nm": "사업보고서", "rcept_dt": "20250311"},
                {"doc_id": "d1", "corp_name": "삼성전자", "report_nm": "사업보고서", "rcept_dt": "20250311"},
            ],
        },
        {
            "tool": "read_section",
            "input": {"doc_id": "d1", "section_title": "사업의 내용"},
            "result": {"doc_id": "d1", "text": "본문 텍스트"},
        },
        {  # 같은 섹션 중복 읽기 → 한 번만 포함
            "tool": "read_section",
            "input": {"doc_id": "d1", "section_title": "사업의 내용"},
            "result": {"doc_id": "d1", "text": "본문 텍스트"},
        },
        {"tool": "read_section", "input": {"doc_id": "dX"}, "result": {"error": "not found"}},
    ]
    ctx = build_retrieved_context(tool_calls)
    assert ctx.count("본문 텍스트") == 1
    assert "## 열람한 공시 본문" in ctx
    assert "## 검색된 공시 목록" in ctx
    assert ctx.count("[d1]") >= 1


def test_build_retrieved_context_truncates():
    tool_calls = [
        {
            "tool": "read_section",
            "input": {"doc_id": "d1", "section_title": "s"},
            "result": {"doc_id": "d1", "text": "가" * 5000},
        }
    ]
    ctx = build_retrieved_context(tool_calls, max_chars=1000)
    assert len(ctx) <= 1000 + 50
    assert "잘림" in ctx


def test_build_retrieved_context_empty():
    assert build_retrieved_context([]) == ""


# --- GET /answer endpoint -------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        yield c


def test_answer_returns_200_on_llm_failure(monkeypatch, client):
    _mock_sequence(monkeypatch, [clova.ClovaAPIError("down")])
    resp = client.get("/answer", params={"question_id": "Q-1", "question": "삼성전자 매출은?"})
    assert resp.status_code == 200
    data = resp.json()
    assert set(data) == {"question_id", "question", "retrieved_context", "think_trace", "answer"}
    assert data["question_id"] == "Q-1"
    assert data["answer"]


def test_answer_returns_200_on_config_error(monkeypatch, client):
    _mock_sequence(monkeypatch, [clova.ClovaConfigError("no key")])
    resp = client.get("/answer", params={"question_id": "Q-2", "question": "질문"})
    assert resp.status_code == 200
    assert resp.json()["answer"]


def test_answer_empty_question_is_200(client):
    resp = client.get("/answer")
    assert resp.status_code == 200
    data = resp.json()
    assert "질문이 비어" in data["answer"]


def test_answer_success_is_cached(monkeypatch, client):
    calls = _mock_sequence(monkeypatch, [_assistant("답변입니다")])
    params = {"question_id": "Q-cache", "question": "캐시 테스트 질문"}
    r1 = client.get("/answer", params=params)
    n_after_first = len(calls)
    r2 = client.get("/answer", params=params)
    assert r1.json() == r2.json()
    assert len(calls) == n_after_first  # 두 번째 호출은 LLM을 다시 부르지 않음


def test_answer_failure_not_cached(monkeypatch, client):
    calls = _mock_sequence(
        monkeypatch, [clova.ClovaConfigError("no key"), clova.ClovaConfigError("no key")]
    )
    params = {"question_id": "Q-nocache", "question": "실패 캐시 테스트"}
    client.get("/answer", params=params)
    n_after_first = len(calls)
    client.get("/answer", params=params)
    assert len(calls) > n_after_first  # 실패는 캐시되지 않고 재시도됨


# --- rate limiting / token budget ----------------------------------------


def test_parse_seconds_formats():
    assert clova._parse_seconds("49s") == 49
    assert clova._parse_seconds("1m30s") == 90
    assert clova._parse_seconds("12") == 12
    assert clova._parse_seconds(None) is None
    assert clova._parse_seconds("garbage") is None


def test_reasoning_effort_always_sent(monkeypatch):
    """tool 이력이 있으면 tools 미전송이어도 reasoning을 꺼야 400이 안 난다."""
    sent = {}

    class FakeResp:
        status_code = 200
        headers = {}
        text = ""

        @staticmethod
        def json():
            return {"choices": [{"message": {"role": "assistant", "content": "ok"}}]}

    def fake_post(url, headers=None, json=None):
        sent.update(json)
        return FakeResp()

    monkeypatch.setattr(clova, "CLOVA_API_KEY", "test-key")
    monkeypatch.setattr(clova, "_http", lambda: type("C", (), {"post": staticmethod(fake_post)})())
    monkeypatch.setattr(clova._governor, "before_request", lambda deadline: None)

    clova.chat_completion([{"role": "user", "content": "hi"}], tools=None)
    assert sent["reasoning_effort"] == "none"
    assert "tools" not in sent

    clova.chat_completion([{"role": "user", "content": "hi"}], tools=[{"type": "function"}])
    assert sent["reasoning_effort"] == "none"
    assert sent["tools"]


def test_compact_history_shrinks_only_old_tool_results():
    from app.agent.loop import _STALE_TOOL_CHARS, _compact_history

    a, b, c = "가" * 5000, "나" * 5000, "다" * 5000
    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": a},  # 오래됨 → 축약
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
        {"role": "tool", "tool_call_id": "2", "content": b},  # 최근 → 유지
        {"role": "assistant", "content": None, "tool_calls": [{"id": "3"}]},
        {"role": "tool", "tool_call_id": "3", "content": c},  # 최근 → 유지
    ]
    out = _compact_history(history)
    assert len(out[2]["content"]) <= _STALE_TOOL_CHARS + 40
    assert len(out[4]["content"]) == 5000
    assert len(out[6]["content"]) == 5000
    assert history[2]["content"] == a  # 원본 불변


def test_compact_history_dedups_repeated_results():
    """같은 섹션을 여러 번 읽어도 본문은 한 번만 전송된다."""
    from app.agent.loop import _compact_history

    same = "동일한 본문 " * 200
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "1"}]},
        {"role": "tool", "tool_call_id": "1", "content": same},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "2"}]},
        {"role": "tool", "tool_call_id": "2", "content": same},
    ]
    out = _compact_history(history)
    assert out[1]["content"] == same  # 첫 등장은 원문 유지
    assert "중복 생략" in out[3]["content"]
    assert len(out[3]["content"]) < 100
    assert history[3]["content"] == same  # 원본 불변


def test_elide_middle_keeps_both_ends():
    from app.agent.loop import _elide_middle

    text = "시작" + "중" * 5000 + "끝부분합계"
    out = _elide_middle(text, 600)
    assert out.startswith("시작")
    assert out.endswith("끝부분합계")  # 합계가 뒤에 오는 공시 표를 잃지 않는다
    assert len(out) < 700
    assert _elide_middle("짧은 글", 600) == "짧은 글"


def test_read_section_paging():
    """긴 섹션은 offset으로 이어 읽을 수 있어야 한다."""
    from app.agent.tools import _MAX_RESULT_CHARS, _page

    text = "가" * (_MAX_RESULT_CHARS * 2 + 500)
    first = _page(text, 0)
    assert first["truncated"] is True
    assert first["next_offset"] == _MAX_RESULT_CHARS
    assert first["total_chars"] == len(text)

    second = _page(text, first["next_offset"])
    assert second["offset"] == _MAX_RESULT_CHARS
    assert second["next_offset"] == _MAX_RESULT_CHARS * 2

    last = _page(text, second["next_offset"])
    assert "truncated" not in last
    assert len(last["text"]) == 500

    assert _page("짧은 본문", 0) == {"text": "짧은 본문"}


def test_turn_result_budget_caps_many_parallel_calls(monkeypatch):
    """한 턴에 도구를 많이 부르면 결과 총량이 예산 안으로 들어와야 한다."""
    from app.agent.loop import _TURN_RESULT_BUDGET

    many = [_tool_call("get_company_info", {"query": "삼성전자"}, "c%d" % i) for i in range(20)]
    _mock_sequence(monkeypatch, [_assistant(None, many), _assistant("답변")])
    result = run_agent([{"role": "user", "content": "q"}])
    assert result.answer == "답변"
    assert len(result.tool_calls) == 20


# --- 회계연도 vs 접수일 (실제 오답 사례 회귀 테스트) ----------------------


def test_base_year_finds_correct_fiscal_year_report():
    """'2025년 실적' 질의는 FY2025 보고서(2026-03 접수)를 찾아야 한다.

    접수일 필터를 쓰면 2025년에 접수된 FY2024 보고서가 잡혀 한 해 전
    실적을 답하게 된다 — 실제로 발생했던 오답이다."""
    from app.data.manifest import search_filings

    fy2025 = search_filings(
        company="삼성화재", doc_group="periodic", doc_subtype="annual", base_year=2025
    )
    assert fy2025, "FY2025 사업보고서를 찾지 못했습니다"
    assert all(r["base_year"] == 2025 for r in fy2025)
    assert any(r["doc_id"] == "periodic_20260312001399" for r in fy2025)
    # 회계연도 보고서는 이듬해에 접수된다
    assert all(r["rcept_dt"].startswith("2026") for r in fy2025)

    # 접수일 필터는 다른(한 해 전) 보고서를 반환한다 — 그래서 구분이 필요하다
    by_rcept = search_filings(
        company="삼성화재", doc_group="periodic", doc_subtype="annual",
        date_from="20250101", date_to="20251231",
    )
    assert by_rcept and all(r["base_year"] == 2024 for r in by_rcept)


def test_search_filings_returns_base_year():
    from app.data.manifest import search_filings

    rows = search_filings(company="삼성전자", doc_group="periodic", limit=5)
    assert rows
    for r in rows:
        assert "base_year" in r and isinstance(r["base_year"], int)
        assert "base_month" in r


def test_tool_accepts_base_year_as_string():
    """LLM이 숫자를 문자열로 채워 넣어도 동작해야 한다."""
    from app.agent.tools import dispatch

    rows = dispatch("search_filings", {
        "company": "삼성화재", "doc_group": "periodic",
        "doc_subtype": "annual", "base_year": "2025",
    })
    assert isinstance(rows, list) and rows
    assert all(r["base_year"] == 2025 for r in rows)
