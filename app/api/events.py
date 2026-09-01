"""서버 전역 이벤트 브로드캐스터 + 실행 중 질의 레지스트리.

대시보드를 여러 컴퓨터·탭에서 열어도 같은 화면을 보려면, 실행 상태가
브라우저가 아니라 서버에 있어야 한다. 질의를 시작한 클라이언트만 SSE를 받던
구조에서, 모든 클라이언트가 하나의 스트림(/events)을 구독하는 구조로 바꾼다.

  POST /runs        → 서버가 백그라운드로 실행 시작
  GET  /events      → 모든 브라우저가 구독 (도구 호출·토큰·완료·삭제 알림)
  GET  /runs/active → 늦게 접속한 브라우저가 진행 중인 실행을 따라잡기 위한 재생

평가용 GET /answer 는 이 구조와 무관하게 기존대로 동작한다.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_MAX_SUBSCRIBERS = 64
_MAX_QUEUE = 500
_RUN_EVENT_CAP = 400  # 재생용으로 보관하는 실행당 이벤트 수
_KEEP_FINISHED = 5  # 완료된 실행을 잠시 남겨 늦은 구독자도 결과를 받게 함
_FINISHED_TTL_S = 120.0


class Broadcaster:
    """구독자 큐에 이벤트를 뿌린다. 느린 구독자가 전체를 막지 않도록
    큐가 차면 그 구독자의 이벤트만 버린다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []

    def subscribe(self) -> queue.Queue | None:
        q: queue.Queue = queue.Queue(maxsize=_MAX_QUEUE)
        with self._lock:
            if len(self._subscribers) >= _MAX_SUBSCRIBERS:
                logger.warning("구독자 한도 초과 — 새 연결을 거부합니다")
                return None
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(event)
            except queue.Full:
                pass  # 화면 갱신이 밀리는 것보다 답변 생성이 우선

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)


broadcaster = Broadcaster()


class RunRegistry:
    """실행 중/최근 완료된 질의 상태. 늦게 접속한 브라우저가 재생할 수 있도록
    이벤트를 실행별로 모아둔다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def create(self, question_id: str, question: str) -> dict[str, Any]:
        run = {
            "run_id": uuid.uuid4().hex[:12],
            "question_id": question_id,
            "question": question,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "started_mono": time.monotonic(),
            "status": "running",
            "events": [],
            "result": None,
        }
        with self._lock:
            self._runs[run["run_id"]] = run
            self._prune()
        return run

    def add_event(self, run_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            if len(run["events"]) < _RUN_EVENT_CAP:
                run["events"].append(event)

    def finish(self, run_id: str, result: dict[str, Any], status: str = "done") -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                return
            run["status"] = status
            run["result"] = result
            run["finished_mono"] = time.monotonic()
            self._prune()

    def _prune(self) -> None:
        """오래된 완료 실행을 정리한다 (호출자가 락을 쥔 상태여야 함)."""
        now = time.monotonic()
        finished = [
            rid for rid, r in self._runs.items()
            if r["status"] != "running"
            and now - r.get("finished_mono", now) > _FINISHED_TTL_S
        ]
        for rid in finished:
            del self._runs[rid]
        done_ids = [rid for rid, r in self._runs.items() if r["status"] != "running"]
        for rid in done_ids[:-_KEEP_FINISHED] if len(done_ids) > _KEEP_FINISHED else []:
            del self._runs[rid]

    def snapshot(self) -> list[dict[str, Any]]:
        """진행 중 + 최근 완료된 실행. 새로 접속한 브라우저가 이걸로 따라잡는다."""
        with self._lock:
            self._prune()
            return [
                {
                    "run_id": r["run_id"],
                    "question_id": r["question_id"],
                    "question": r["question"],
                    "started_at": r["started_at"],
                    "status": r["status"],
                    "elapsed": round(
                        r.get("finished_mono", time.monotonic()) - r["started_mono"], 1
                    ),
                    "events": list(r["events"]),
                    "result": r["result"],
                }
                for r in self._runs.values()
            ]

    def has_running(self) -> bool:
        with self._lock:
            return any(r["status"] == "running" for r in self._runs.values())


registry = RunRegistry()
