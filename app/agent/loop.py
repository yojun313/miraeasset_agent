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
_TURN_RESULT_BUDGET = 24_000  # 한 턴 도구 결과 총량
_FRESH_TOOL_TURNS = 2  # 최근 몇 턴의 도구 결과를 원문 그대로 유지할지
_STALE_TOOL_CHARS = 800  # 그보다 오래된 결과는 이 길이로 압축
_DEDUP_MIN_CHARS = 200  # 이 길이 넘는 동일 결과는 재전송하지 않고 참조로 대체
_FORCED_ANSWER_GRACE_S = 40.0

_CORE_TOOLS = ("get_company_info", "search_filings", "read_section")
_MID_TOOLS = _CORE_TOOLS + ("list_sections",)
_TOOL_FALLBACKS: tuple[tuple[str, ...] | None, ...] = (None, _MID_TOOLS, _CORE_TOOLS)

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
    tool_calls: list[dict[str, Any]]
    trace: list[str] = field(default_factory=list)
    completed: bool = True


def _run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    try:
        return dispatch(name, tool_input)
    except Exception as e:
        logger.exception("tool %s raised", name)
        return {"error": f"{type(e).__name__}: {e}"}


def _content_to_text(content: Any) -> str:
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
    if len(text) <= budget:
        return text
    head = int(budget * 0.6)
    tail = budget - head
    dropped = len(text) - head - tail
    return f"{text[:head]}\n...({dropped:,}자 생략)...\n{text[-tail:]}"


def _compact_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    boundaries = [i for i, m in enumerate(history) if m.get("role") == "assistant"]
    cutoff = (
        boundaries[-_FRESH_TOOL_TURNS] if len(boundaries) > _FRESH_TOOL_TURNS else -1
    )

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
                msg["content"] = (
                    f"(앞선 {first}번째 도구 결과와 동일한 내용입니다 — 중복 생략)"
                )
                compacted.append(msg)
                continue
            seen[content] = len(seen) + 1

        if i < cutoff and len(content) > _STALE_TOOL_CHARS:
            msg = dict(msg)
            msg["content"] = _elide_middle(content, _STALE_TOOL_CHARS)
        compacted.append(msg)
    return compacted


def _linearize_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
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
            out.append(
                {
                    "role": "assistant",
                    "content": (text + "\n" if text else "") + "\n".join(lines),
                }
            )
            continue

        if role == "tool":
            name = names.get(msg.get("tool_call_id", ""), "도구")
            line = f"[도구 결과] {name} → {msg.get('content')}"
            if out and out[-1]["role"] == "user" and out[-1].get("_tool_result"):
                out[-1]["content"] += "\n\n" + line
            else:
                out.append({"role": "user", "content": line, "_tool_result": True})
            continue

        out.append(msg)

    # 내부 표시용 키 제거
    return [{k: v for k, v in m.items() if k != "_tool_result"} for m in out]


_UNFINISHED_RE = re.compile(
    r"(검색|확인|조회|찾아|진행|시도|살펴|읽어|이어|계속|정리|파악|수집)[가-힣\s]{0,8}"
    r"(하겠습니다|보겠습니다|예정입니다|좋겠습니다|할 수 있을 것입니다|드리겠습니다)[.\s]*"
    r"(?:\n|$|잠시만)"
    r"|잠시만\s*기다려"
)


def _looks_unfinished(content: str) -> bool:
    tail = content.strip()[-120:]
    return bool(_UNFINISHED_RE.search(tail))


_TOOL_ECHO_RE = re.compile(
    r"(다른|새로운)\s*(키워드|필터|조건).{0,14}?(사용|시도|바꿔)|다시 시도하세요|조건을 넓혀"
)


def _looks_like_tool_echo(content: str) -> bool:
    return len(content.strip()) < 200 and bool(_TOOL_ECHO_RE.search(content))


_RCEPT_NO_RE = re.compile(r"(?<!\d)20\d{12}(?!\d)")


def _fabricated_citations(
    answer: str,
    tool_calls_log: list[dict[str, Any]],
    history: list[dict[str, Any]],
) -> list[str]:
    cited = set(_RCEPT_NO_RE.findall(answer))
    if not cited:
        return []
    seen_text = json.dumps(tool_calls_log, ensure_ascii=False) + " ".join(
        str(m.get("content") or "") for m in history if m.get("role") == "user"
    )
    return sorted(cited - set(_RCEPT_NO_RE.findall(seen_text)))


_CITATION_RE = re.compile(
    r"(?:major|periodic|exchange|holding)_\d{14}|(?<!\d)20\d{12}(?!\d)"
)


def _read_doc_ids(tool_calls_log: list[dict[str, Any]]) -> set[str]:
    """실제로 본문을 읽었거나 목록에 나온 doc_id 집합 (search 행 + read/list 인자)."""
    ids: set[str] = set()
    for c in tool_calls_log:
        args = c.get("input") or {}
        if isinstance(args, dict) and args.get("doc_id"):
            ids.add(str(args["doc_id"]))
        res = c.get("result")
        if isinstance(res, dict):
            if res.get("doc_id"):  # read_section 결과
                ids.add(str(res["doc_id"]))
            for row in res.get("filings") or res.get("results") or []:
                if isinstance(row, dict) and row.get("doc_id"):
                    ids.add(str(row["doc_id"]))
    return ids


def _lacks_citations(answer: str, tool_calls_log: list[dict[str, Any]]) -> bool:
    text = answer.strip()
    if _CITATION_RE.search(text):
        return False
    has_number = bool(re.search(r"\d{2,}", text))
    if len(text) < 300 and not (has_number and len(text) >= 120):
        return False  # 짧은 답·거절은 인용이 없어도 자연스럽다
    return bool(_read_doc_ids(tool_calls_log))


_REFUSAL_RE = re.compile(
    r"(?:제공|드릴|답변|말씀드릴)\s*수\s*없|투자\s*(?:의견|판단|권유|조언)|전망(?:을|이나|은)?\s*(?:제공|예측|드리)"
    r"|개인정보|공개\s*대상이\s*아니"
)
_GIVEUP_RE = re.compile(r"확인할 수 없|확인되지 않|확인이 어렵|찾을 수 없|제공할 수 없")
_NEGATIVE_Q_RE = re.compile(r"않은|않는|없는|안\s*한|제외|아닌|하지\s*않")


def _gave_up_with_unread_results(
    answer: str,
    tool_calls_log: list[dict[str, Any]],
    question: str = "",
) -> list[str]:
    if not _GIVEUP_RE.search(answer):
        return []
    if question and _NEGATIVE_Q_RE.search(question):
        return []
    listed: list[str] = []
    read: set[str] = set()
    for c in tool_calls_log:
        args = c.get("input") or {}
        if c.get("tool") in ("read_section", "list_sections") and isinstance(
            args, dict
        ):
            read.add(str(args.get("doc_id")))
        res = c.get("result")
        if isinstance(res, dict) and not res.get("error"):
            for row in res.get("filings") or []:
                if isinstance(row, dict) and row.get("doc_id"):
                    listed.append(str(row["doc_id"]))
    cited = {m.group(0) for m in _CITATION_RE.finditer(answer)}
    if cited and any(any(c in r or r in c for r in read) for c in cited):
        return []
    unread = [d for d in dict.fromkeys(listed) if d not in read]
    return unread if listed and not read else []


_TEXT_CALL_RE = re.compile(r"\[도구\s*호출\]\s*([A-Za-z_]\w*)\s*\((\{.*\})\)")
_KNOWN_TOOLS = {t["name"] for t in TOOLS}


def _parse_text_tool_calls(content: str) -> list[dict[str, Any]]:
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
        calls.append(
            {
                "id": f"call_{uuid.uuid4().hex[:8]}",
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return calls


def _parse_tool_call(tc: Any) -> tuple[str, str, dict[str, Any], str | None]:
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
    return (
        call_id,
        name,
        {},
        f"인자 타입을 해석할 수 없습니다: {type(args_raw).__name__}",
    )


def _subset(
    tool_specs: list[dict[str, Any]], names: tuple[str, ...] | None
) -> list[dict[str, Any]]:
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
    last: clova.ClovaAPIError | None = None
    for level, names in enumerate(_TOOL_FALLBACKS):
        tools = _subset(tool_specs, names)
        try:
            return clova.chat_completion(
                messages, tools=tools, deadline=deadline, on_meta=on_meta
            )
        except clova.ClovaAPIError as e:
            if not e.is_unsupported_function:
                raise
            if level == len(_TOOL_FALLBACKS) - 1:
                names = ", ".join(
                    t.get("function", {}).get("name", "") for t in tool_specs
                )
                retry = [dict(m) for m in messages]
                for m in retry:
                    if (
                        m.get("role") == "user"
                        and isinstance(m.get("content"), str)
                        and "[검색 힌트]" in m["content"]
                    ):
                        m["content"] = m["content"].split("\n\n[검색 힌트]")[0]
                retry.append(
                    {
                        "role": "user",
                        "content": (
                            f"사용할 수 있는 도구는 {names} 뿐입니다. 이 이름 그대로만 "
                            "function calling으로 호출하세요. 다른 이름의 함수는 존재하지 않습니다."
                        ),
                    }
                )
                logger.warning("40009 — 힌트 제거 + 도구명 상기 후 최종 재시도")
                emit(
                    {
                        "type": "note",
                        "level": "warn",
                        "text": "도구 축소로도 거부됨 — 힌트를 빼고 유효 도구명을 상기시켜 재시도",
                    }
                )
                try:
                    return clova.chat_completion(
                        retry, tools=tool_specs, deadline=deadline, on_meta=on_meta
                    )
                except clova.ClovaAPIError as e2:
                    raise e2
            last = e
            nxt = _TOOL_FALLBACKS[level + 1]
            logger.warning("40009 — 도구를 %d개로 줄여 재시도", len(nxt or ()))
            emit(
                {
                    "type": "note",
                    "level": "warn",
                    "text": f"모델이 잘못된 도구 호출을 시도해 거부됨 — 도구 {len(nxt or ())}개로 축소 재시도",
                }
            )
    raise last if last else RuntimeError("unreachable")


def _make_emitter(on_event: Callable[[dict[str, Any]], None] | None):
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
    emit = _make_emitter(on_event)
    history = list(messages)
    tool_calls_log: list[dict[str, Any]] = []
    trace: list[str] = []
    unfinished_nudges = 0
    citation_nudges = 0
    fabrication_nudges = 0
    giveup_nudges = 0
    notool_nudges = 0
    repeat_turns = 0
    call_cache: dict[str, Any] = {}  # 동일 호출 반복 차단용 (name+args → 결과)
    tool_specs = openai_tool_specs()
    usage_total = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
    }

    def on_meta(meta: dict[str, Any]) -> None:
        usage = meta.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                usage_total[key] += int(usage.get(key) or 0)
            except (TypeError, ValueError):
                pass
        usage_total["calls"] += 1
        emit(
            {
                "type": "usage",
                "usage": usage,
                "cumulative": dict(usage_total),
                "rate_limit": meta.get("rate_limit") or {},
            }
        )

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
            trace.append(
                f"[턴 {turn}] 시간 예산 소진 — 수집된 정보로 답변을 마무리합니다"
            )
            emit(
                {
                    "type": "note",
                    "level": "warn",
                    "text": "시간 예산 소진 — 수집된 정보로 마무리합니다",
                }
            )
            return _finish_without_tools(
                history, tool_calls_log, trace, deadline, emit, on_meta
            )
        except clova.ClovaError as e:
            trace.append(
                f"[턴 {turn}] LLM 호출 실패({type(e).__name__}) — 수집된 정보로 마무리 시도"
            )
            logger.warning("chat_completion failed mid-loop: %s", e)
            emit(
                {
                    "type": "note",
                    "level": "error",
                    "text": f"LLM 호출 실패({type(e).__name__}) — 마무리 시도",
                }
            )
            return _finish_without_tools(
                history, tool_calls_log, trace, deadline, emit, on_meta
            )

        reasoning = _content_to_text(
            msg.get("reasoning_content")
            or msg.get("thinking_content")
            or msg.get("thinkingContent")
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
                emit(
                    {
                        "type": "note",
                        "level": "warn",
                        "text": f"모델이 도구를 텍스트로 호출 — {len(text_calls)}건을 변환해 실행합니다",
                    }
                )
                raw_tool_calls = text_calls
                content_text = ""
            elif "[도구 호출]" in content_text:
                trace.append(f"[턴 {turn}] 깨진 텍스트 도구 호출 — 형식 교정 요청")
                history.append({"role": "assistant", "content": content_text})
                history.append(
                    {
                        "role": "user",
                        "content": (
                            "직전 응답의 도구 호출은 실행되지 않았습니다. 답변 본문에 "
                            "'[도구 호출]'을 쓰지 말고, function calling 기능으로 다시 "
                            "호출하거나 지금까지의 정보로 최종 답변을 작성하세요."
                        ),
                    }
                )
                continue

        if not raw_tool_calls:
            history.append({"role": "assistant", "content": content_text})
            if content_text.strip():
                if _looks_like_tool_echo(content_text) and unfinished_nudges < 2:
                    unfinished_nudges += 1
                    trace.append(
                        f"[턴 {turn}] 도구 안내문 에코 감지 — 실제 답변 재요청"
                    )
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": "도구 안내문을 답변으로 반환 — 실제 답변을 재요청합니다",
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "그건 답변이 아니라 도구 안내문입니다. 질문에 대해 지금까지 "
                                "수집한 정보로 답하거나, 확인할 수 없으면 '확인할 수 없다'고 "
                                "명시적으로 답하세요."
                            ),
                        }
                    )
                    continue
                if _looks_unfinished(content_text):
                    if unfinished_nudges >= 1:
                        trace.append(
                            f"[턴 {turn}] 재촉에도 실행 없음 — 수집분으로 강제 마무리"
                        )
                        emit(
                            {
                                "type": "note",
                                "level": "warn",
                                "text": "재촉에도 실행이 없어 수집된 정보로 마무리합니다",
                            }
                        )
                        return _finish_without_tools(
                            history, tool_calls_log, trace, deadline, emit, on_meta
                        )
                    unfinished_nudges += 1
                    trace.append(
                        f"[턴 {turn}] 미완성 응답(계속 의사만 표명) — 실행 재촉"
                    )
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": "모델이 '검색하겠습니다'라고만 답함 — 실제 실행을 재촉합니다",
                        }
                    )
                    collected = "; ".join(
                        f"{c['tool']} → {_summarize_result(c['result'])[:80]}"
                        for c in tool_calls_log[-3:]
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "말로만 하지 말고 지금 실행하세요. 이미 확보한 결과: "
                                f"{collected or '없음'}. 이 정보로 답할 수 있으면 추가 검색 없이 "
                                "지금 최종 답변을 쓰고, 부족하면 도구를 function calling으로 "
                                "실제 호출하세요. 답변에는 진행 상황·사과·이전 응답 언급을 쓰지 "
                                "말고 질문에 대한 사실과 근거만 담으세요."
                            ),
                        }
                    )
                    continue
                if (
                    not tool_calls_log
                    and len(content_text) > 450
                    and notool_nudges < 1
                    and not _CITATION_RE.search(content_text)
                    and not _REFUSAL_RE.search(content_text)
                ):
                    notool_nudges += 1
                    trace.append(
                        f"[턴 {turn}] 도구 호출 없이 장문 서술 — 근거 확인 요구"
                    )
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": "도구를 호출하지 않고 내용을 서술함 — 공시 확인을 요구합니다",
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "도구를 한 번도 호출하지 않았는데 공시 내용을 서술했습니다. 공시에 "
                                "적힌 내용은 반드시 search_filings·read_section 으로 확인한 뒤 "
                                "(보고서명, 접수일, doc_id)와 함께 인용하세요. 확인 없이 아는 내용을 "
                                "쓰면 안 됩니다. 도구 없이 답해도 되는 거절·안내라면 그 부분만 "
                                "짧게 다시 쓰세요."
                            ),
                        }
                    )
                    continue
                unread = _gave_up_with_unread_results(
                    content_text, tool_calls_log, str(history[0].get("content") or "")
                )
                if unread and giveup_nudges < 1:
                    giveup_nudges += 1
                    shown = ", ".join(unread[:4])
                    trace.append(
                        f"[턴 {turn}] 검색 결과를 읽지 않고 확인 불가 — 열람 요구({shown})"
                    )
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": "검색된 문서를 읽지 않고 '확인 불가'로 답함 — 열람을 요구합니다",
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                f"검색으로 {len(unread)}건의 관련 공시({shown})를 받았는데 본문을 읽지 "
                                "않고 확인 불가라고 답했습니다. 질문과 관련 있어 보이는 문서를 "
                                "read_section으로 실제로 확인한 뒤 답하세요. 읽고도 없으면 그때 "
                                "확인 불가라고 하세요."
                            ),
                        }
                    )
                    continue
                if (
                    not _fabricated_citations(content_text, tool_calls_log, history)
                    and _lacks_citations(content_text, tool_calls_log)
                    and citation_nudges < 1
                ):
                    citation_nudges += 1
                    trace.append(f"[턴 {turn}] 근거 공시 표기 누락 — 인용 추가 요구")
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": "답변에 근거 공시 표기가 없음 — 인용 추가를 요구합니다",
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                "답변 내용은 유지하되, 근거 공시 표기가 없습니다. 도구 결과에 "
                                "실제로 나온 doc_id만 (보고서명, 접수일, doc_id) 형식으로 붙여 "
                                "다시 작성하세요. 결과에 없는 번호를 지어내면 안 됩니다."
                            ),
                        }
                    )
                    continue
                fabricated = _fabricated_citations(
                    content_text, tool_calls_log, history
                )
                if fabricated and fabrication_nudges < 1:
                    fabrication_nudges += 1
                    shown = ", ".join(fabricated[:3])
                    trace.append(
                        f"[턴 {turn}] 근거 없는 접수번호 인용 감지({shown}) — 재작성 요구"
                    )
                    emit(
                        {
                            "type": "note",
                            "level": "warn",
                            "text": f"도구 결과에 없는 접수번호 인용 감지 — 재작성을 요구합니다",
                        }
                    )
                    history.append(
                        {
                            "role": "user",
                            "content": (
                                f"답변에 인용한 접수번호({shown})는 도구 결과에 없는 번호입니다. "
                                "접수번호나 문서 정보를 절대 지어내지 마세요. 필요하면 도구를 "
                                "호출해 실제 공시를 확인하고, 확인하지 못한 내용은 '확인할 수 "
                                "없음'으로 답하세요."
                            ),
                        }
                    )
                    continue
                trace.append(f"[턴 {turn}] 최종 답변 생성")
                emit({"type": "answer", "text": content_text, "turn": turn})
                return AgentResult(content_text, history, tool_calls_log, trace, True)
            trace.append(f"[턴 {turn}] 빈 응답 수신 — 답변 재요청")
            emit(
                {
                    "type": "note",
                    "level": "warn",
                    "text": "빈 응답 수신 — 답변을 다시 요청합니다",
                }
            )
            history.append(
                {
                    "role": "user",
                    "content": "답변이 비어 있습니다. 위 질문에 대한 최종 답변을 작성해 주세요.",
                }
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
            emit(
                {
                    "type": "tool_call",
                    "turn": turn,
                    "tool": name or "unknown",
                    "input": args,
                }
            )
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
                result: Any = {
                    "error": f"{parse_error} — 인자를 올바른 JSON 객체로 다시 호출하세요"
                }
            elif not name:
                result = {"error": "도구 이름이 비어 있습니다"}
            elif call_key in call_cache:
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
            tool_calls_log.append(
                {"tool": name or "unknown", "input": args, "result": result}
            )
            emit(
                {
                    "type": "tool_result",
                    "turn": turn,
                    "tool": name or "unknown",
                    "input": args,
                    "summary": _summarize_result(result),
                    "ok": not (isinstance(result, dict) and result.get("error")),
                    "doc_ids": _result_doc_ids(result),
                }
            )
            trace.append(
                f"[턴 {turn}] 도구 호출: {name or '(이름 없음)'}"
                f"({json.dumps(args, ensure_ascii=False)[:300]}) → {_summarize_result(result)}"
            )
            pending_results.append((call_id, result))

        turn_all_repeat = bool(pending_results) and all(
            isinstance(res, dict) and res.get("repeat_warning")
            for _, res in pending_results
        )
        repeat_turns = repeat_turns + 1 if turn_all_repeat else 0
        all_repeats = turn_all_repeat and (
            len(pending_results) >= 2 or repeat_turns >= 2
        )
        history.append(
            {
                "role": "assistant",
                "content": content_text or None,
                "tool_calls": sanitized_calls,
            }
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
            emit(
                {
                    "type": "note",
                    "level": "warn",
                    "text": "같은 호출만 반복 중 — 수집된 정보로 답변을 요구합니다",
                }
            )
            read_docs = [
                str(c.get("input", {}).get("doc_id"))
                for c in tool_calls_log
                if c.get("tool") == "read_section"
                and isinstance(c.get("result"), dict)
                and c["result"].get("text")
            ]
            collected = "; ".join(
                f"{c['tool']} → {_summarize_result(c['result'])[:80]}"
                for c in tool_calls_log[-4:]
            )
            history.append(
                {
                    "role": "user",
                    "content": (
                        "직전 턴의 호출은 전부 이전과 동일해 아무 진전이 없습니다. 같은 호출을 "
                        "반복하지 말고, 조건을 바꾼 새로운 호출을 하거나(다른 필터·도구), 지금까지 "
                        f"받은 결과로 답을 완성하세요. 이미 확보한 결과: {collected or '없음'}."
                        + (
                            f" 이미 본문을 읽은 문서({', '.join(read_docs[:3])})의 내용이 질문의 "
                            "답일 가능성이 높으니 그 내용을 근거로 답하세요."
                            if read_docs
                            else ""
                        )
                        + " 답변에는 검색 과정 서술이 아니라 질문에 대한 사실과 근거만 담으세요."
                    ),
                }
            )

    trace.append(
        f"도구 호출 한도({MAX_AGENT_TURNS}턴) 도달 — 수집된 정보로 답변을 마무리합니다"
    )
    emit(
        {
            "type": "note",
            "level": "warn",
            "text": f"도구 호출 한도({MAX_AGENT_TURNS}턴) 도달 — 마무리합니다",
        }
    )
    return _finish_without_tools(
        history, tool_calls_log, trace, deadline, emit, on_meta
    )


def _finish_without_tools(
    history: list[dict[str, Any]],
    tool_calls_log: list[dict[str, Any]],
    trace: list[str],
    deadline: float | None,
    emit: Callable[[dict[str, Any]], None] | None = None,
    on_meta: Any = None,
) -> AgentResult:
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
