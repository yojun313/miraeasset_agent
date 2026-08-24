"""Anthropic tool-use agentic loop over the DART filing tools."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from app.agent.system_prompt import SYSTEM_PROMPT
from app.agent.tools import TOOLS, dispatch
from app.config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, MAX_AGENT_TURNS

_client: anthropic.Anthropic | None = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (see .env.example)")
        _client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    return _client


def _run_tool(name: str, tool_input: dict[str, Any]) -> Any:
    try:
        return dispatch(name, tool_input)
    except Exception as e:  # noqa: BLE001 - surface as a tool error, don't crash the loop
        return {"error": f"{type(e).__name__}: {e}"}


def run_agent(
    messages: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]], list[dict[str, Any]]]:
    """Runs the tool-use loop to completion for the given message history
    (which already includes the new user turn). Returns (answer_text,
    updated_messages, tool_calls) where updated_messages is the full history
    to persist for the next turn, and tool_calls records every tool
    invocation made while answering (for citation display)."""
    client = _get_client()
    history = list(messages)
    tool_calls: list[dict[str, Any]] = []

    for _ in range(MAX_AGENT_TURNS):
        response = client.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=history,
        )

        history.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            answer = "".join(block.text for block in response.content if block.type == "text")
            return answer, history, tool_calls

        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            result = _run_tool(block.name, block.input)
            tool_calls.append({"tool": block.name, "input": block.input, "result": result})
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result, ensure_ascii=False, default=str),
                }
            )
        history.append({"role": "user", "content": tool_results})

    fallback = (
        "죄송합니다 - 도구 호출 한도 내에 답변을 완성하지 못했습니다. "
        "질문을 더 구체적으로 나누어 다시 시도해 주세요."
    )
    return fallback, history, tool_calls
