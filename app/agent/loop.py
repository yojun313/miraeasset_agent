"""HyperCLOVA X function-calling agent loop over the DART filing tools.

Speaks the OpenAI-compatible message format via app.agent.clova: an
assistant message may carry `tool_calls`, and each one is answered with a
`role="tool"` message before the next model call.

The loop is defensive by design — a malformed tool call, a failing tool, an
empty model response, or an exhausted time/turn budget must all degrade into
the best possible answer, never an exception to the caller. The only
exception that escapes run_agent is ClovaConfigError (a permanent local
misconfiguration the API layer should surface loudly).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from app.agent import clova
from app.agent.system_prompt import SYSTEM_PROMPT
from app.agent.tools import dispatch, openai_tool_specs
from app.config import MAX_AGENT_TURNS

logger = logging.getLogger(__name__)

_TOOL_RESULT_MAX_CHARS = 12_000
# 한 턴에 모델이 도구를 20번 넘게 호출하는 일이 실제로 있다. 그 결과가 전부
# 이력에 쌓이면 매 턴 재전송되며 분당 토큰 한도(60,000)를 순식간에 넘긴다.
_TURN_RESULT_BUDGET = 24_000  # 한 턴 도구 결과 총량
_FRESH_TOOL_TURNS = 2  # 최근 몇 턴의 도구 결과를 원문 그대로 유지할지
_STALE_TOOL_CHARS = 800  # 그보다 오래된 결과는 이 길이로 압축
_DEDUP_MIN_CHARS = 200  # 이 길이 넘는 동일 결과는 재전송하지 않고 참조로 대체
# 시간 예산이 소진돼도 마무리 합성 호출 1회는 이만큼의 유예를 갖는다 —
# 수집한 근거를 버리고 빈손으로 폴백하는 것보다 조금 늦은 답이 낫다.
_FORCED_ANSWER_GRACE_S = 40.0

_FORCED_ANSWER_PROMPT = (
    "도구 호출 한도(또는 시간 한도)에 도달했습니다. 추가 도구 호출 없이, 지금까지 "
    "수집된 정보만으로 최종 답변을 작성하세요. 수집된 정보로 확인되지 않는 내용은 "
    "반드시 '공시에서 확인되지 않음'이라고 명시하세요."
)

_STATIC_FALLBACK = (
    "죄송합니다. 시간·도구 호출 한도 내에 답변을 완성하지 못했습니다. "
    "질문을 더 구체적으로 나누어 다시 시도해 주세요."
)


@dataclass
class AgentResult:
    answer: str
    history: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]  # [{tool, input, result}] — 인용/컨텍스트 조립용
    trace: list[str] = field(default_factory=list)  # think_trace 라인들
    completed: bool = True  # False = 턴/시간 예산 소진으로 강제 마무리됨


def _run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    try:
        return dispatch(name, tool_input)
    except Exception as e:  # noqa: BLE001 - surface as a tool error, don't crash the loop
        logger.exception("tool %s raised", name)
        return {"error": f"{type(e).__name__}: {e}"}


def _content_to_text(content: Any) -> str:
    """OpenAI-compat content is normally a string; tolerate None and typed-part lists."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "".join(parts)
    return str(content)


def _summarize_result(result: Any) -> str:
    if isinstance(result, list):
        return f"{len(result)}건 결과"
    if isinstance(result, dict):
        if result.get("error"):
            return f"오류: {str(result['error'])[:200]}"
        if isinstance(result.get("text"), str):
            note = " (일부 잘림)" if result.get("truncated") else ""
            return f"본문 {len(result['text']):,}자{note}"
        if isinstance(result.get("sections"), list):
            return f"섹션 {len(result['sections'])}개"
        if result.get("note"):
            return str(result["note"])[:200]
        return "1건 결과"
    return str(result)[:200]


def _result_doc_ids(result: Any) -> list[str]:
    """도구 결과에서 발견한 공시 doc_id (UI가 "무엇을 찾았는지" 보여주는 용도)."""
    items = result if isinstance(result, list) else [result]
    out: list[str] = []
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("doc_id"), str):
            if item["doc_id"] not in out:
                out.append(item["doc_id"])
    return out[:12]


def _serialize_tool_result(result: Any, max_chars: int = _TOOL_RESULT_MAX_CHARS) -> str:
    try:
        text = json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(result)
    return _elide_middle(text, max_chars)


def _elide_middle(text: str, budget: int) -> str:
    """가운데를 잘라내고 앞뒤를 남긴다.

    앞에서만 자르면 잘린 뒤쪽이 통째로 사라지는데, 공시 본문은 합계·총액·
    결론이 끝에 오는 경우가 많아 정확히 필요한 부분을 잃는다."""
    if len(text) <= budget:
        return text
    head = int(budget * 0.6)
    tail = budget - head
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...({dropped:,}자 생략)...\n{text[-tail:]}"


def _compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """이전 턴의 도구 결과를 줄인 사본을 반환한다.

    이력은 매 턴 통째로 재전송되므로, 지난 턴의 대용량 본문까지 원문으로
    실어 보내면 분당 토큰 한도를 넘긴다. 두 가지를 적용한다:
      1) 오래된 결과는 가운데를 생략해 앞뒤만 남긴다.
      2) 앞서 똑같이 나왔던 결과는 중복 전송하지 않고 참조로 대체한다
         (모델이 같은 섹션을 여러 번 읽는 일이 실제로 잦다).
    원본 history는 건드리지 않는다 — 세션 저장/추적용으로 온전해야 한다."""
    boundaries = [i for i, m in enumerate(history) if m.get("role") == "assistant"]
    cutoff = boundaries[-_FRESH_TOOL_TURNS] if len(boundaries) > _FRESH_TOOL_TURNS else -1

    compacted: list[dict[str, Any]] = []
    seen: dict[str, int] = {}  # 내용 → 처음 등장한 순번
    for i, msg in enumerate(history):
        content = msg.get("content")
        if msg.get("role") != "tool" or not isinstance(content, str):
            compacted.append(msg)
            continue

        if len(content) > _DEDUP_MIN_CHARS:
            first = seen.get(content)
            if first is not None:
                msg = dict(msg)
                msg["content"] = f"(앞선 {first}번째 도구 결과와 동일한 내용입니다 — 중복 생략)"
                compacted.append(msg)
                continue
            seen[content] = len(seen) + 1

        if i < cutoff and len(content) > _STALE_TOOL_CHARS:
            msg = dict(msg)
            msg["content"] = _elide_middle(content, _STALE_TOOL_CHARS)
        compacted.append(msg)
    return compacted


def _parse_tool_call(tc: Any) -> tuple[str, str, dict[str, Any], str | None]:
    """Returns (call_id, name, args, parse_error). Never raises."""
    if not isinstance(tc, dict):
        tc = {}
    call_id = tc.get("id") or f"call_{uuid.uuid4().hex[:8]}"
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
    name = fn.get("name") if isinstance(fn.get("name"), str) else ""
    args_raw = fn.get("arguments")

    if isinstance(args_raw, dict):  # 일부 구현은 이미 파싱된 객체를 준다
        return call_id, name, args_raw, None
    if args_raw is None or (isinstance(args_raw, str) and not args_raw.strip()):
        return call_id, name, {}, None
    if isinstance(args_raw, str):
        try:
            parsed = json.loads(args_raw)
        except json.JSONDecodeError as e:
            return call_id, name, {}, f"인자가 올바른 JSON이 아닙니다: {e}"
        if not isinstance(parsed, dict):
            return call_id, name, {}, "인자는 JSON 객체여야 합니다"
        return call_id, name, parsed, None
    return call_id, name, {}, f"인자 타입을 해석할 수 없습니다: {type(args_raw).__name__}"


def _make_emitter(on_event: Callable[[dict[str, Any]], None] | None):
    """이벤트 콜백을 감싼다. 관측 실패가 답변 생성을 막아선 안 되므로
    콜백에서 나는 예외는 로그만 남기고 삼킨다."""
    if on_event is None:
        return lambda _event: None

    def emit(event: dict[str, Any]) -> None:
        try:
            on_event(event)
        except Exception:  # noqa: BLE001
            logger.exception("on_event callback failed")

    return emit


def run_agent(
    messages: list[dict[str, Any]],
    deadline: float | None = None,
    on_event: Callable[[dict[str, Any]], None] | None = None,
) -> AgentResult:
    """Runs the function-calling loop to completion for the given message
    history (which already includes the new user turn). `deadline` is a
    time.monotonic() value bounding the whole loop.

    `on_event` receives progress events (turn/tool_call/tool_result/usage/
    answer) as they happen, so a UI can show the agent working live. It is
    best-effort: a raising callback never breaks the loop."""
    emit = _make_emitter(on_event)
    history = list(messages)
    tool_calls_log: list[dict[str, Any]] = []
    trace: list[str] = []
    tool_specs = openai_tool_specs()
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "calls": 0}

    def on_meta(meta: dict[str, Any]) -> None:
        usage = meta.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                usage_total[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        usage_total["calls"] += 1
        emit({"type": "usage", "usage": usage, "cumulative": dict(usage_total),
              "rate_limit": meta.get("rate_limit") or {}})

    for turn in range(1, MAX_AGENT_TURNS + 1):
        emit({"type": "turn", "turn": turn, "max_turns": MAX_AGENT_TURNS})
        try:
            msg = clova.chat_completion(
                [{"role": "system", "content": SYSTEM_PROMPT}] + _compact_history(history),
                tools=tool_specs,
                deadline=deadline,
                on_meta=on_meta,
            )
        except clova.ClovaConfigError:
            raise  # 영구적 설정 오류는 숨기지 않는다
        except clova.DeadlineExceeded:
            trace.append(f"[턴 {turn}] 시간 예산 소진 — 수집된 정보로 답변을 마무리합니다")
            emit({"type": "note", "level": "warn", "text": "시간 예산 소진 — 수집된 정보로 마무리합니다"})
            return _finish_without_tools(history, tool_calls_log, trace, deadline, emit, on_meta)
        except clova.ClovaError as e:
            trace.append(f"[턴 {turn}] LLM 호출 실패({type(e).__name__}) — 수집된 정보로 마무리 시도")
            logger.warning("chat_completion failed mid-loop: %s", e)
            emit({"type": "note", "level": "error", "text": f"LLM 호출 실패({type(e).__name__}) — 마무리 시도"})
            return _finish_without_tools(history, tool_calls_log, trace, deadline, emit, on_meta)

        # OpenAI 호환 계층은 snake_case(tool_calls)를 쓰지만, 네이티브 v3
        # 필드명(toolCalls)이 흘러들어올 가능성에 대비해 읽기만 양쪽 허용.
        reasoning = _content_to_text(
            msg.get("reasoning_content") or msg.get("thinking_content") or msg.get("thinkingContent")
        ).strip()
        if reasoning:
            trace.append(f"[턴 {turn}] 모델 추론: {reasoning[:1500]}")
            emit({"type": "reasoning", "turn": turn, "text": reasoning[:1500]})

        content_text = _content_to_text(msg.get("content"))
        raw_tool_calls = msg.get("tool_calls") or msg.get("toolCalls")
        if not isinstance(raw_tool_calls, list):
            raw_tool_calls = []

        if not raw_tool_calls:
            history.append({"role": "assistant", "content": content_text})
            if content_text.strip():
                trace.append(f"[턴 {turn}] 최종 답변 생성")
                emit({"type": "answer", "text": content_text, "turn": turn})
                return AgentResult(content_text, history, tool_calls_log, trace, True)
            # 내용도 도구 호출도 없는 빈 응답 — 한 번 재촉하고 다음 턴으로
            trace.append(f"[턴 {turn}] 빈 응답 수신 — 답변 재요청")
            emit({"type": "note", "level": "warn", "text": "빈 응답 수신 — 답변을 다시 요청합니다"})
            history.append(
                {"role": "user", "content": "답변이 비어 있습니다. 위 질문에 대한 최종 답변을 작성해 주세요."}
            )
            continue

        if content_text.strip():
            trace.append(f"[턴 {turn}] 모델 메모: {content_text.strip()[:500]}")
            emit({"type": "note", "level": "info", "text": content_text.strip()[:500]})

        emit({"type": "tool_batch", "turn": turn, "count": len(raw_tool_calls)})
        sanitized_calls: list[dict[str, Any]] = []
        pending_results: list[tuple[str, Any]] = []
        for tc in raw_tool_calls:
            call_id, name, args, parse_error = _parse_tool_call(tc)
            emit({"type": "tool_call", "turn": turn, "tool": name or "unknown", "input": args})
            sanitized_calls.append(
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name or "unknown",
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            )
            if parse_error:
                result: Any = {"error": f"{parse_error} — 인자를 올바른 JSON 객체로 다시 호출하세요"}
            elif not name:
                result = {"error": "도구 이름이 비어 있습니다"}
            else:
                result = _run_tool(name, args)
            tool_calls_log.append({"tool": name or "unknown", "input": args, "result": result})
            emit({
                "type": "tool_result",
                "turn": turn,
                "tool": name or "unknown",
                "input": args,
                "summary": _summarize_result(result),
                "ok": not (isinstance(result, dict) and result.get("error")),
                "doc_ids": _result_doc_ids(result),
            })
            trace.append(
                f"[턴 {turn}] 도구 호출: {name or '(이름 없음)'}"
                f"({json.dumps(args, ensure_ascii=False)[:300]}) → {_summarize_result(result)}"
            )
            pending_results.append((call_id, result))

        # 어시스턴트 메시지(tool_calls 포함)를 먼저 넣고, 각 호출에 대한
        # tool 결과 메시지를 빠짐없이 넣어야 다음 요청이 형식 오류가 안 난다.
        history.append(
            {"role": "assistant", "content": content_text or None, "tool_calls": sanitized_calls}
        )
        per_call_cap = max(1_200, _TURN_RESULT_BUDGET // max(1, len(pending_results)))
        per_call_cap = min(per_call_cap, _TOOL_RESULT_MAX_CHARS)
        for call_id, result in pending_results:
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _serialize_tool_result(result, per_call_cap),
                }
            )

    trace.append(f"도구 호출 한도({MAX_AGENT_TURNS}턴) 도달 — 수집된 정보로 답변을 마무리합니다")
    emit({"type": "note", "level": "warn", "text": f"도구 호출 한도({MAX_AGENT_TURNS}턴) 도달 — 마무리합니다"})
    return _finish_without_tools(history, tool_calls_log, trace, deadline, emit, on_meta)


def _finish_without_tools(
    history: list[dict[str, Any]],
    tool_calls_log: list[dict[str, Any]],
    trace: list[str],
    deadline: float | None,
    emit: Callable[[dict[str, Any]], None] | None = None,
    on_meta: Any = None,
) -> AgentResult:
    """One last no-tools call to turn whatever was gathered into an answer;
    a static apology if even that fails."""
    closing = {"role": "user", "content": _FORCED_ANSWER_PROMPT}
    if deadline is not None:
        deadline = max(deadline, time.monotonic() + _FORCED_ANSWER_GRACE_S)

    emit = emit or (lambda _event: None)
    answer = ""
    try:
        msg = clova.chat_completion(
            [{"role": "system", "content": SYSTEM_PROMPT}] + _compact_history(history) + [closing],
            tools=None,
            deadline=deadline,
            on_meta=on_meta,
        )
        answer = _content_to_text(msg.get("content")).strip()
    except clova.ClovaConfigError:
        raise
    except clova.ClovaError as e:
        logger.warning("forced-answer call failed: %s", e)

    if answer:
        trace.append("수집된 정보 기반 최종 답변 생성")
    else:
        answer = _STATIC_FALLBACK
        trace.append("최종 답변 생성 실패 — 안내 문구로 대체")
    emit({"type": "answer", "text": answer, "forced": True})

    new_history = history + [closing, {"role": "assistant", "content": answer}]
    return AgentResult(answer, new_history, tool_calls_log, trace, False)
