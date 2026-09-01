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
    # 요청은 평문화되어 나간다 (HCX-007이 네이티브 tool 이력에서 깨지므로).
    # 내부 history에는 표준 구조가 그대로 남아 있어야 한다.
    second_call_messages = calls[1]["messages"]
    roles = [m["role"] for m in second_call_messages]
    assert roles[-2:] == ["assistant", "user"]
    assert "[도구 호출] get_company_info" in second_call_messages[-2]["content"]
    assert "[도구 결과] get_company_info" in second_call_messages[-1]["content"]
    assert all("tool_calls" not in m for m in second_call_messages)
    assert result.history[1]["tool_calls"][0]["id"] == "call_1"
    assert result.history[2]["role"] == "tool"
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
    monkeypatch.setattr(clova._governor, "before_request", lambda deadline, need_tokens=0: None)

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


# --- calculate 도구 (단위 환산 오답 회귀 테스트) --------------------------


def test_korean_amount_conversion():
    """227,500,000,000원을 '2조 275억원'으로 답한 실제 오답의 회귀 테스트."""
    from app.agent.calc import korean_amount

    assert korean_amount(227_500_000_000) == "2,275억"      # 정답
    assert korean_amount(2_027_500_000_000) == "2조 275억"   # 오답이 나왔던 값
    assert korean_amount(114_800_000_000) == "1,148억"
    assert korean_amount(4_004_923_296_000) == "4조 49억 2,329만 6,000"
    assert korean_amount(0) == "0"
    assert korean_amount(-1_234_567).startswith("-")


def test_calculate_tool_accepts_filing_format():
    """공시 금액 표기(쉼표 포함)를 그대로 넣을 수 있어야 한다."""
    from app.agent.tools import dispatch

    r = dispatch("calculate", {"expression": "227,500,000,000"})
    assert r["result"] == 227_500_000_000
    assert r["korean_amount"] == "2,275억"
    assert r["formatted"] == "227,500,000,000"


def test_calculate_arithmetic():
    from app.agent.calc import calculate

    assert round(calculate("(300870903-258935494)/258935494*100")["result"], 2) == 16.20
    assert calculate("227500000000 - 114800000000")["result"] == 112_700_000_000


def test_calculate_rejects_unsafe_input():
    """LLM이 채우는 값이므로 코드 실행 경로가 없어야 한다."""
    from app.agent.calc import CalcError, calculate

    for bad in ["__import__('os').system('ls')", "open('/etc/passwd')", "1/0",
                "2**999", "", "1+", "x + 1"]:
        with pytest.raises(CalcError):
            calculate(bad)


def test_calculate_tool_returns_error_not_exception():
    from app.agent.tools import dispatch

    r = dispatch("calculate", {"expression": "1/0"})
    assert "error" in r


# --- 도구 인자 검증 / 외부 안내 필터 (E-07 실패 회귀) ---------------------


def test_invalid_doc_subtype_for_group_returns_hint():
    """major에 exchange 전용 subtype을 붙이면 조용한 0건 대신 안내를 준다."""
    from app.agent.tools import dispatch

    r = dispatch("search_filings", {
        "company": "한화에어로스페이스", "doc_group": "major", "doc_subtype": "신규시설투자등",
    })
    assert "error" in r
    assert "doc_subtype" in r["error"]
    assert "생략" in r["error"]

    r2 = dispatch("search_filings", {"doc_group": "periodic", "doc_subtype": "대량보유상황보고서"})
    assert "error" in r2 and "annual" in r2["error"]


def test_valid_doc_subtype_still_works():
    from app.agent.tools import dispatch

    rows = dispatch("search_filings", {
        "company": "삼성화재", "doc_group": "periodic", "doc_subtype": "annual", "base_year": 2025,
    })
    assert isinstance(rows, list) and rows


def test_strip_external_referrals():
    """'네이버 금융에서 확인하세요' 류 안내를 걷어낸다 (실제 발생한 가드레일 누수)."""
    from app.agent.answer import strip_external_referrals as f

    out = f("확인되지 않습니다.\n\n추가 정보가 필요하면 네이버 금융 등 금융 플랫폼에서 확인해 보시기 바랍니다.")
    assert "네이버" not in out
    assert "확인되지 않습니다" in out

    out2 = f("계약금액은 227,500,000,000원입니다. 자세한 내용은 DART 홈페이지를 참고하시기 바랍니다.")
    assert "DART 홈페이지" not in out2
    assert "227,500,000,000" in out2


def test_strip_external_referrals_keeps_normal_answers():
    """정상 답변과 인용은 건드리지 않아야 한다."""
    from app.agent.answer import strip_external_referrals as f

    normal = "삼성전자의 매출은 300조원입니다.\n근거 공시: (사업보고서, 2025-03-11, periodic_20250311001085)"
    assert f(normal) == normal
    assert f("공시에서 확인되지 않습니다.") == "공시에서 확인되지 않습니다."
    assert f("") == ""


def test_calculate_not_advertised_as_tool():
    """산술 도구를 노출하면 HCX-007의 function calling이 40009로 깨진다.
    (도구 4개 중 calculate 포함 시 성공률 0/4, 제외 시 4/4 — 실측)
    환산은 read_section 결과에 미리 실어 보낸다."""
    from app.agent.tools import TOOLS

    assert "calculate" not in [t["name"] for t in TOOLS]


def test_read_section_annotates_amounts_inline():
    """환산은 별도 목록이 아니라 숫자 옆에 붙어야 한다 — 목록 방식은 재무제표처럼
    숫자가 많은 섹션에서 정작 필요한 값이 누락됐다."""
    from app.agent.tools import dispatch

    r = dispatch("read_section", {"doc_id": "exchange_20230602800079", "section_title": "계약"})
    assert "227,500,000,000(2,275억)" in r["text"]
    assert r.get("note_amount")


def test_annotation_covers_dense_financial_section():
    """재무제표 섹션의 모든 금액에 환산이 붙어야 한다 (앞 80개만 붙던 버그)."""
    from app.agent.tools import dispatch

    r = dispatch("read_section", {
        "doc_id": "periodic_20260312001399", "section_title": "2-2. 연결 포괄손익계산서",
    })
    assert "2,020,287,132,255(약 2조 202억)" in r["text"]


def test_annotate_amounts_skips_small():
    from app.agent.tools import annotate_amounts

    out, n = annotate_amounts("금액 1,234원 · 5,000,000원 · 227,500,000,000원")
    assert n == 1  # 1억 미만은 건드리지 않는다
    assert "1,234원" in out and "5,000,000원" in out
    assert "227,500,000,000(2,275억)" in out


def test_korean_amount_short():
    from app.agent.calc import korean_amount_short as k

    assert k(227_500_000_000) == "2,275억"
    assert k(2_020_287_132_255) == "약 2조 202억"
    assert k(5_944_700_000_000) == "5조 9,447억"


def test_linearize_history_converts_tool_structure():
    """HCX-007은 네이티브 도구 이력에서 function calling이 깨진다
    (실측: 네이티브 0/4 vs 평문화 3/4). 형식만 바꾸고 정보는 보존한다."""
    from app.agent.loop import _linearize_history

    history = [
        {"role": "user", "content": "질문"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "c1", "type": "function",
             "function": {"name": "get_company_info", "arguments": '{"query":"삼성"}'}},
            {"id": "c2", "type": "function",
             "function": {"name": "search_filings", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "content": "결과1"},
        {"role": "tool", "tool_call_id": "c2", "content": "결과2"},
    ]
    out = _linearize_history(history)

    assert [m["role"] for m in out] == ["user", "assistant", "user"]  # tool 결과는 하나로 병합
    assert all("tool_calls" not in m for m in out)
    assert all(m["role"] != "tool" for m in out)
    # 도구 이름·인자·결과가 모두 남아 있어야 한다
    assert "get_company_info" in out[1]["content"] and '{"query":"삼성"}' in out[1]["content"]
    assert "결과1" in out[2]["content"] and "결과2" in out[2]["content"]
    assert "search_filings" in out[2]["content"]
    # 원본은 그대로
    assert history[1]["tool_calls"][0]["id"] == "c1"
    assert history[2]["role"] == "tool"


# --- 토큰 추정 / TPM 선제 대기 -------------------------------------------


def test_estimate_tokens_is_conservative():
    """공식 계산기 실측 대비 과대추정이어야 한다 (과소추정은 429를 부른다)."""
    from app.agent.clova import estimate_tokens

    msgs = [{"role": "user", "content": "삼성중공업이 2023년에 체결한 계약금액은 얼마인가?"}]
    est = estimate_tokens(msgs)
    # 실측 기준 한글 문장은 문자수/1.37 근처가 상한
    assert est >= len(msgs[0]["content"]) / 1.4
    assert estimate_tokens(msgs, [{"type": "function"}]) > est  # tools도 포함해야 한다
    assert estimate_tokens([]) >= 1


def test_governor_waits_when_budget_short(monkeypatch):
    """예상 소요가 잔여 토큰을 넘으면 창 리셋까지 기다린다."""
    import time as _time

    from app.agent.clova import _RateLimitGovernor

    gov = _RateLimitGovernor()
    gov._remaining_tokens = 1_000
    gov._window_ends = _time.monotonic() + 30
    waited = []
    monkeypatch.setattr(gov, "_wait", lambda s, d, r: waited.append((round(s), r)))

    gov.before_request(None, need_tokens=50_000)   # 잔여보다 큼 → 대기
    assert waited and "잔여" in waited[0][1]

    waited.clear()
    gov.before_request(None, need_tokens=100)      # 잔여 안쪽 → 대기 없음
    assert not waited


def test_governor_observe_records_remaining():
    from app.agent.clova import _RateLimitGovernor

    gov = _RateLimitGovernor()
    gov.observe({
        "x-ratelimit-remaining-tokens": "12345",
        "x-ratelimit-reset-tokens": "42s",
        "x-ratelimit-remaining-requests": "50",
    })
    assert gov._remaining_tokens == 12345
    assert gov._window_ends > 0


# --- LLM이 채운 인자의 결손 처리 (KeyError 회귀) --------------------------


def test_dispatch_never_raises_on_missing_args():
    """도구 인자는 LLM이 채우므로 누락·None·타입 오류가 항상 온다.
    예외로 터지면 턴만 낭비하므로 안내 메시지를 반환해야 한다."""
    from app.agent.tools import dispatch

    cases = [
        ("read_section", {"doc_id": "periodic_20260312001399"}),   # section_title 누락
        ("read_section", {}),                                       # doc_id 누락
        ("read_section", {"doc_id": None, "section_title": None}),
        ("get_company_info", {}),
        ("get_company_info", {"query": None}),
        ("search_fulltext", {}),
        ("search_fulltext", {"query": "가"}),                        # 3자 미만
        ("list_sections", {}),
        ("list_sections", {"doc_id": None}),
        ("search_filings", {}),
    ]
    for name, args in cases:
        result = dispatch(name, args)  # 예외가 나면 테스트 실패
        assert isinstance(result, (dict, list)), (name, args, result)


def test_read_section_without_title_lists_sections():
    """섹션 있는 문서에 제목이 비면 첫 섹션을 주지 말고 목차를 안내한다."""
    from app.agent.tools import dispatch

    r = dispatch("read_section", {"doc_id": "periodic_20260312001399"})
    assert "error" in r
    assert r["available_sections"]
    assert "text" not in r


def test_read_section_without_title_ok_for_sectionless_doc():
    """거래소공시는 섹션이 없으므로 제목이 비어도 전문을 준다."""
    from app.agent.tools import dispatch

    r = dispatch("read_section", {"doc_id": "exchange_20230602800079"})
    assert r.get("text") and "227,500,000,000" in r["text"]


def test_wrong_section_title_returns_available_list():
    from app.agent.tools import dispatch

    r = dispatch("read_section", {
        "doc_id": "periodic_20260312001399", "section_title": "존재하지않는섹션",
    })
    assert "error" in r and r["available_sections"]


# --- 단위(백만원) 반영 환산 (10배 오답 회귀) ------------------------------


def test_annotation_respects_declared_unit():
    """재무제표는 표마다 단위를 선언한다. 이걸 무시하면 32,725,961(백만원)을
    모델이 스스로 환산하다 '327조'로 10배 틀린다 (실제 발생한 오답)."""
    from app.agent.tools import dispatch

    r = dispatch("read_section", {
        "doc_id": "periodic_20250311001085", "section_title": "1. 요약재무정보",
    })
    assert "32,725,961(약 32조 7,259억)" in r["text"]
    assert "300,870,903(약 300조 8,709억)" in r["text"]
    assert "327조" not in r["text"]


def test_annotate_amounts_unit_scales():
    from app.agent.tools import annotate_amounts

    won, _ = annotate_amounts("(단위 : 원)\n금액 | 227,500,000,000")
    assert "227,500,000,000(2,275억)" in won

    million, _ = annotate_amounts("(단위 : 백만원)\n영업이익 | 32,725,961")
    assert "32,725,961(약 32조 7,259억)" in million

    thousand, _ = annotate_amounts("(단위 : 천원)\n금액 | 1,234,567")
    assert "1,234,567(약 12억 3,456만)" in thousand


def test_annotate_amounts_switches_unit_midway():
    """한 섹션에 원/백만원이 섞여 나와도 각 표의 단위를 따라야 한다."""
    from app.agent.tools import annotate_amounts

    text = "(단위 : 백만원)\nA | 32,725,961\n(단위 : 원)\nB | 227,500,000,000"
    out, _ = annotate_amounts(text)
    assert "32,725,961(약 32조 7,259억)" in out
    assert "227,500,000,000(2,275억)" in out


# --- 검색 힌트 라우터 (E-07 오검색 회귀) ----------------------------------


def test_search_hint_routes_topics():
    """유상증자 질문에 엉뚱한 doc_subtype을 골라 '확인 불가'로 답한 실제 사례.
    질문 키워드에서 결정론적으로 올바른 경로를 힌트로 주입한다."""
    from app.agent.answer import build_search_hint as hint

    h = hint("한화에어로스페이스가 2025년 3월 20일 결정한 유상증자에서 발행하는 신주는?")
    assert h and "doc_group='major'" in h and "유상증자" in h
    assert "date_from=20250320" in h and "date_to=20250327" in h  # 날짜 상한 필수

    assert "base_year" in hint("삼성전자의 2024년 영업이익은?")
    assert "단일판매공급계약해지" in hint("삼성중공업 계약 중 해지된 것이 있는가?")
    assert "holding" in hint("삼성물산의 삼성바이오로직스 보유비율 변동은?")
    assert hint("삼성전자는 어떤 회사야?") is None  # 해당 없으면 주입하지 않는다


def test_search_hint_is_injected_into_agent_messages(monkeypatch):
    from app.agent import clova
    from app.agent.answer import answer_question

    captured = {}

    def fake(messages, **kw):
        captured["messages"] = messages
        return {"role": "assistant", "content": "답"}

    monkeypatch.setattr(clova, "chat_completion", fake)
    answer_question("한화에어로스페이스의 유상증자 신주 수는?")
    user_msg = [m for m in captured["messages"] if m["role"] == "user"][0]
    assert "검색 힌트" in user_msg["content"]
    assert "doc_group='major'" in user_msg["content"]
