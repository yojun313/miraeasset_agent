#!/usr/bin/env python3
"""평가 질문을 실제 API 경로로 실행하고 결과를 요약한다.

네트워크 서버를 띄우지 않고 FastAPI TestClient로 /runs 를 호출하므로,
대시보드가 읽는 logs/answers.jsonl 에 동일하게 기록된다.

    uv run python scripts/run_eval.py H-07 "질문 내용"
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def run(question_id: str, question: str, timeout: float = 260.0) -> dict:
    with TestClient(app) as client:
        client.post("/runs", json={"message": question, "session_id": question_id})
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            runs = client.get("/runs/active").json()
            mine = [r for r in runs if r["question_id"] == question_id]
            if mine and mine[-1]["status"] != "running":
                return mine[-1]
            time.sleep(1.0)
    return {"status": "timeout", "result": None, "events": []}


def main() -> None:
    question_id, question = sys.argv[1], sys.argv[2]
    started = time.monotonic()
    run_state = run(question_id, question)
    result = run_state.get("result") or {}
    trace = result.get("think_trace", "")

    print(f"═══ {question_id}  ({time.monotonic() - started:.0f}s, {run_state['status']})")
    print(f"Q: {question}\n")
    for line in trace.splitlines():
        print("  ", line[:130])
    print("\n--- 답변 ---")
    print(result.get("answer", "(없음)")[:1400])
    tools = len(re.findall(r"도구 호출:", trace))
    print(f"\n[도구 {tools}회 · 근거 {len(result.get('retrieved_context', '')):,}자]")


if __name__ == "__main__":
    main()
