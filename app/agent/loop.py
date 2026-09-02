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
import re

from app.agent.tools import TOOLS, dispatch, openai_tool_specs
from app.config import CLOVA_ANSWER_OUTPUT_TOKENS, MAX_AGENT_TURNS

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

# HCX-007의 function calling은 도구 개수가 늘수록 불안정해서, 도구 결과가 이력에
# 있는 요청에서 400 "Unsupported function"(40009)을 반환한다. 통제 실험(교차 실행)
# 결과 도구 3개는 5/5 성공, 5개는 0/5 실패였다. 모델 결함이라 우리가 없앨 수는
# 없으므로, 실패하면 도구를 줄여 다시 시도한다 — 답을 못 내는 것보다 낫다.
_CORE_TOOLS = ("get_company_info", "search_filings", "read_section")
_MID_TOOLS = _CORE_TOOLS + ("list_sections",)
_TOOL_FALLBACKS: tuple[tuple[str, ...] | None, ...] = (None, _MID_TOOLS, _CORE_TOOLS)

# 강제 마무리 시점은 모델이 "외부 사이트에서 확인하세요"로 도피하기 가장 쉬운
# 지점이므로, 시스템 프롬프트의 금지 규칙을 여기서 한 번 더 상기시킨다.
_FORCED_ANSWER_PROMPT = (
    "도구 호출 한도(또는 시간 한도)에 도달했습니다. 추가 도구 호출 없이, 지금까지 "
    "수집된 정보만으로 최종 답변을 작성하세요. 수집된 정보로 확인되지 않는 내용은 "
    "반드시 '공시에서 확인되지 않음'이라고 명시하세요. "
    "외부 사이트·뉴스·증권 정보 서비스(네이버 금융, DART 홈페이지 등)를 참고하라는 "
    "안내는 절대 하지 마세요. 확인이 안 되면 그 사실만 담담히 밝히면 됩니다."
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
        if result.get("repeat_warning"):
            # 이전 결과를 그대로 되돌려주는 응답이 "1건 결과"로 요약돼 모델이
            # 새 결과로 착각하고 같은 호출을 5번 반복한 실제 사례 — 명확히 표기
            return "반복 호출 차단 — 같은 조건은 같은 결과입니다. 조건을 바꾸세요"
        if result.get("error"):
            return f"오류: {str(result['error'])[:200]}"
        if isinstance(result.get("text"), str):
            note = " (일부 잘림)" if result.get("truncated") else ""
            return f"본문 {len(result['text']):,}자{note}"
        if isinstance(result.get("filings"), list):
            base = f"{result.get('count', len(result['filings']))}건 결과"
            if result.get("correction_count"):
                base += f" (정정 {result['correction_count']}건 포함)"
            return base
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


def _linearize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """도구 이력을 평문 메시지로 바꾼 사본을 반환한다.

    HCX-007은 assistant.tool_calls / role="tool" 구조가 대화 이력에 들어가면
    이후 function calling이 깨져 400 "Unsupported function"(40009)을 낸다.
    통제 실험(교차 실행, 길이 동일): 네이티브 구조 0/4 vs 평문화 3/4.
    정보는 그대로 두고 표현 형식만 바꾼다. 내부 history는 손대지 않는다
    (세션 저장·추적용으로 원본 구조를 유지해야 하므로)."""
    out: list[dict[str, Any]] = []
    names: dict[str, str] = {}  # tool_call_id → 도구 이름

    for msg in history:
        role = msg.get("role")

        if role == "assistant" and msg.get("tool_calls"):
            lines: list[str] = []
            for tc in msg["tool_calls"] or []:
                fn = tc.get("function") or {} if isinstance(tc, dict) else {}
                name = fn.get("name") or "unknown"
                if isinstance(tc, dict) and tc.get("id"):
                    names[tc["id"]] = name
                lines.append(f"[도구 호출] {name}({fn.get('arguments') or '{}'})")
            text = _content_to_text(msg.get("content")).strip()
            out.append({
                "role": "assistant",
                "content": (text + "\n" if text else "") + "\n".join(lines),
            })
            continue

        if role == "tool":
            name = names.get(msg.get("tool_call_id", ""), "도구")
            line = f"[도구 결과] {name} → {msg.get('content')}"
            # 한 턴에 여러 도구를 부르면 결과 메시지가 연달아 나온다. user 역할이
            # 연속되지 않도록 하나로 합친다.
            if out and out[-1]["role"] == "user" and out[-1].get("_tool_result"):
                out[-1]["content"] += "\n\n" + line
            else:
                out.append({"role": "user", "content": line, "_tool_result": True})
            continue

        out.append(msg)

    # 내부 표시용 키 제거
    return [{k: v for k, v in m.items() if k != "_tool_result"} for m in out]


# "…검색하겠습니다"처럼 계속하겠다는 의사만 밝히고 도구를 부르지 않은 응답.
# 이걸 최종 답변으로 내보내면 반쪽짜리 답이 나간다 (실제 사례: 삼성전자 수치만
# 확인하고 "SK하이닉스는 추가로 검색하겠습니다"가 그대로 최종 답변이 됨).
_UNFINISHED_RE = re.compile(
    r"(검색|확인|조회|찾아|진행|시도|살펴|읽어|이어|계속)[가-힣\s]{0,6}"
    r"(하겠습니다|보겠습니다|예정입니다|좋겠습니다)[.\s]*$"
)


def _looks_unfinished(content: str) -> bool:
    tail = content.strip()[-80:]
    return bool(_UNFINISHED_RE.search(tail))


# 도구의 빈 결과 안내문("다른 키워드나 필터를 사용해 보시기 바랍니다")을 그대로
# 최종 답변으로 내놓은 실제 사례 — 답이 아니라 에코다.
_TOOL_ECHO_RE = re.compile(
    r"(다른|새로운)\s*(키워드|필터|조건).{0,14}?(사용|시도|바꿔)|다시 시도하세요|조건을 넓혀"
)


def _looks_like_tool_echo(content: str) -> bool:
    return len(content.strip()) < 200 and bool(_TOOL_ECHO_RE.search(content))


# \b 는 doc_id 접두어(major_2025...)의 밑줄에 막힌다 — 숫자 경계로 판단
_RCEPT_NO_RE = re.compile(r"(?<!\d)20\d{12}(?!\d)")


def _fabricated_citations(
    answer: str,
    tool_calls_log: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> list[str]:
    """답변이 인용한 접수번호 중 도구 결과 어디에도 없는 것들.

    빈 컨텍스트에서 모델이 접수번호를 통째로 지어낸 실제 사례(V2-03:
    '20230000123456 ... 예시로 실제 문서 ID와는 다를 수 있습니다') 대응.
    질문에 원래 들어 있던 번호는 날조가 아니므로 user 메시지도 근거로 본다."""
    cited = set(_RCEPT_NO_RE.findall(answer))
    if not cited:
        return []
    seen_text = json.dumps(tool_calls_log, ensure_ascii=False) + " ".join(
        str(m.get("content") or "") for m in history if m.get("role") == "user"
    )
    return sorted(cited - set(_RCEPT_NO_RE.findall(seen_text)))


_TEXT_CALL_RE = re.compile(r"\[도구\s*호출\]\s*([A-Za-z_]\w*)\s*\((\{.*\})\)")
_KNOWN_TOOLS = {t["name"] for t in TOOLS}


def _parse_text_tool_calls(content: str) -> list[dict[str, Any]]:
    """본문 텍스트에 적힌 '[도구 호출] name({...})' 을 실제 호출로 변환한다.

    도구 이력을 평문으로 보여주는 우회책(40009 대응)의 부작용으로, 모델이
    function calling 대신 그 텍스트 형식을 흉내 내 도구를 부르는 일이 생겼다
    (그 텍스트가 그대로 최종 답변으로 반환된 실제 사례). 형식이 무엇이든
    의도가 명확한 호출은 받아서 실행한다."""
    calls: list[dict[str, Any]] = []
    for line in content.split("\n"):
        m = _TEXT_CALL_RE.search(line)
        if not m:
            continue
        name, args_raw = m.group(1), m.group(2)
        if name not in _KNOWN_TOOLS:
            continue
        try:
            args = json.loads(args_raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(args, dict):
            continue
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:8]}",
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
        })
    return calls


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


def _subset(tool_specs: list[dict[str, Any]], names: tuple[str, ...] | None) -> list[dict[str, Any]]:
    if names is None:
        return tool_specs
    return [t for t in tool_specs if t.get("function", {}).get("name") in names]


def _call_model(
    messages: list[dict[str, Any]],
    tool_specs: list[dict[str, Any]],
    deadline: float | None,
    on_meta: Any,
    emit: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    """도구를 단계적으로 줄여가며 모델을 호출한다. 40009(모델이 없는 함수를
    호출하려 함)일 때만 축소 재시도하고, 다른 오류는 그대로 올린다."""
    last: clova.ClovaAPIError | None = None
    for level, names in enumerate(_TOOL_FALLBACKS):
        tools = _subset(tool_specs, names)
        try:
            return clova.chat_completion(
                messages, tools=tools, deadline=deadline, on_meta=on_meta
            )
        except clova.ClovaAPIError as e:
            if not e.is_unsupported_function or level == len(_TOOL_FALLBACKS) - 1:
                raise
            last = e
            nxt = _TOOL_FALLBACKS[level + 1]
            logger.warning("40009 — 도구를 %d개로 줄여 재시도", len(nxt or ()))
            emit({
                "type": "note", "level": "warn",
                "text": f"모델이 잘못된 도구 호출을 시도해 거부됨 — 도구 {len(nxt or ())}개로 축소 재시도",
            })
    raise last if last else RuntimeError("unreachable")


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
    unfinished_nudges = 0
    citation_nudges = 0
    call_cache: dict[str, Any] = {}  # 동일 호출 반복 차단용 (name+args → 결과)
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
            msg = _call_model(
                [{"role": "system", "content": SYSTEM_PROMPT}]
                + _linearize_history(_compact_history(history)),
                tool_specs,
                deadline,
                on_meta,
                emit,
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
            text_calls = _parse_text_tool_calls(content_text)
            if text_calls:
                trace.append(
                    f"[턴 {turn}] 텍스트 형식 도구 호출 {len(text_calls)}건 감지 — 실제 호출로 변환해 실행"
                )
                emit({"type": "note", "level": "warn",
                      "text": f"모델이 도구를 텍스트로 호출 — {len(text_calls)}건을 변환해 실행합니다"})
                raw_tool_calls = text_calls
                # 흉내 텍스트를 메모로 남기면 다음 턴에 또 흉내 낸다 — 비운다
                content_text = ""
            elif "[도구 호출]" in content_text:
                # 마커는 있는데 파싱이 안 되는 경우 — 답변으로 내보내면 안 된다
                trace.append(f"[턴 {turn}] 깨진 텍스트 도구 호출 — 형식 교정 요청")
                history.append({"role": "assistant", "content": content_text})
                history.append({
                    "role": "user",
                    "content": (
                        "직전 응답의 도구 호출은 실행되지 않았습니다. 답변 본문에 "
                        "'[도구 호출]'을 쓰지 말고, function calling 기능으로 다시 "
                        "호출하거나 지금까지의 정보로 최종 답변을 작성하세요."
                    ),
                })
                continue

        if not raw_tool_calls:
            history.append({"role": "assistant", "content": content_text})
            if content_text.strip():
                if _looks_like_tool_echo(content_text) and unfinished_nudges < 2:
                    unfinished_nudges += 1
                    trace.append(f"[턴 {turn}] 도구 안내문 에코 감지 — 실제 답변 재요청")
                    emit({"type": "note", "level": "warn",
                          "text": "도구 안내문을 답변으로 반환 — 실제 답변을 재요청합니다"})
                    history.append({
                        "role": "user",
                        "content": (
                            "그건 답변이 아니라 도구 안내문입니다. 질문에 대해 지금까지 "
                            "수집한 정보로 답하거나, 확인할 수 없으면 '확인할 수 없다'고 "
                            "명시적으로 답하세요."
                        ),
                    })
                    continue
                if _looks_unfinished(content_text) and unfinished_nudges < 2:
                    unfinished_nudges += 1
                    trace.append(f"[턴 {turn}] 미완성 응답(계속 의사만 표명) — 실행 재촉")
                    emit({"type": "note", "level": "warn",
                          "text": "모델이 '검색하겠습니다'라고만 답함 — 실제 실행을 재촉합니다"})
                    history.append({
                        "role": "user",
                        "content": (
                            "말로만 하지 말고 지금 실행하세요: 필요한 도구를 function calling으로 "
                            "실제 호출하거나, 이미 수집한 정보만으로 최종 답변을 완성하세요."
                        ),
                    })
                    continue
                fabricated = _fabricated_citations(content_text, tool_calls_log, history)
                if fabricated and citation_nudges < 1:
                    citation_nudges += 1
                    shown = ", ".join(fabricated[:3])
                    trace.append(f"[턴 {turn}] 근거 없는 접수번호 인용 감지({shown}) — 재작성 요구")
                    emit({"type": "note", "level": "warn",
                          "text": f"도구 결과에 없는 접수번호 인용 감지 — 재작성을 요구합니다"})
                    history.append({
                        "role": "user",
                        "content": (
                            f"답변에 인용한 접수번호({shown})는 도구 결과에 없는 번호입니다. "
                            "접수번호나 문서 정보를 절대 지어내지 마세요. 필요하면 도구를 "
                            "호출해 실제 공시를 확인하고, 확인하지 못한 내용은 '확인할 수 "
                            "없음'으로 답하세요."
                        ),
                    })
                    continue
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
            call_key = f"{name}|{json.dumps(args, ensure_ascii=False, sort_keys=True)}"
            if parse_error:
                result: Any = {"error": f"{parse_error} — 인자를 올바른 JSON 객체로 다시 호출하세요"}
            elif not name:
                result = {"error": "도구 이름이 비어 있습니다"}
            elif call_key in call_cache:
                # 같은 조건의 호출을 반복하면 같은 결과가 나올 뿐이다 — 실행하지
                # 않고 결과를 되돌려주며 조건 변경을 강하게 요구한다 (동일 실패
                # 호출을 6번 반복하며 턴을 전부 태운 실제 사례).
                result = {
                    "repeat_warning": (
                        "이미 똑같은 조건으로 호출했고 결과는 같습니다. 조건을 반드시 "
                        "바꾸거나(필터 제거·기간 변경), 지금까지의 정보로 답변을 완성하세요."
                    ),
                    "previous_result": call_cache[call_key],
                }
            else:
                result = _run_tool(name, args)
                call_cache[call_key] = result
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

        # 턴 전체가 반복 차단이면 경고만으로는 못 벗어난다 — 같은 8건 배치를
        # 세 번 반복하다 예산을 태운 실제 사례(H-03). 강하게 개입한다.
        all_repeats = pending_results and all(
            isinstance(res, dict) and res.get("repeat_warning") for _, res in pending_results
        )
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
        if all_repeats:
            trace.append(f"[턴 {turn}] 턴 전체가 반복 호출 — 종합 답변을 강제 요구")
            emit({"type": "note", "level": "warn",
                  "text": "같은 호출만 반복 중 — 수집된 정보로 답변을 요구합니다"})
            history.append({
                "role": "user",
                "content": (
                    "직전 턴의 호출은 전부 이전과 동일해 아무 진전이 없습니다. 더 이상 "
                    "같은 호출을 반복하지 말고, 지금까지 받은 결과만으로 질문에 답하세요. "
                    "일부만 확인됐다면 어디까지 확인했는지 밝히세요."
                ),
            })

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
            [{"role": "system", "content": SYSTEM_PROMPT}]
            + _linearize_history(_compact_history(history))
            + [closing],
            tools=None,
            deadline=deadline,
            on_meta=on_meta,
            max_output_tokens=CLOVA_ANSWER_OUTPUT_TOKENS,
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
