"""서버측 질의 히스토리.

대시보드의 최근 질의 목록이 브라우저 localStorage에 있으면 기기·브라우저·
접속 주소마다 따로 놀아서 다른 컴퓨터에서는 기록이 보이지 않는다. 서버가
모든 질의를 logs/answers.jsonl 에 남기므로 그걸 읽어 제공한다 — 어디서
접속하든 같은 목록이 보인다.

삭제는 **실제 삭제**다: 해당 줄을 로그 파일에서 물리적으로 제거한다(원자적
재작성). 따라서 id는 줄 번호가 아니라 **줄 내용의 해시**를 쓴다 — 삭제로
줄 번호가 밀려도, 다른 기기의 오래된 목록에서 삭제해도 엉뚱한 기록이
지워지지 않는다.

주의: answers.jsonl 은 평가 기간(09.07~09.20) 주최측 호출의 증빙이기도 하다.
그 기간에는 대시보드에서 삭제하지 말 것 — 지우면 복구할 수 없다.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any

from app.config import ANSWER_LOG_PATH

logger = logging.getLogger(__name__)

# 로그 append(routes._log_answer)와 삭제 재작성이 겹치면 줄이 유실될 수 있어
# 하나의 락을 양쪽이 공유한다.
LOG_LOCK = threading.Lock()

_TOOL_CALL_RE = re.compile(r"도구 호출:")


def _line_id(raw_line: str) -> str:
    return hashlib.sha1(raw_line.strip().encode("utf-8")).hexdigest()[:12]


def _iter_records() -> list[tuple[str, str, dict[str, Any]]]:
    """(id, 원본 줄, 레코드) 목록. 깨진 줄·빈 질문은 목록에서 제외한다."""
    out: list[tuple[str, str, dict[str, Any]]] = []
    try:
        if not ANSWER_LOG_PATH.exists():
            return out
        with ANSWER_LOG_PATH.open(encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    rec = json.loads(stripped)
                except ValueError:
                    continue  # 기록 도중 끊긴 마지막 줄 등
                if not isinstance(rec, dict) or not str(rec.get("question", "")).strip():
                    continue
                out.append((_line_id(stripped), stripped, rec))
    except OSError:
        logger.warning("히스토리 로그를 읽지 못했습니다", exc_info=True)
    return out


def _summarize(record_id: str, rec: dict[str, Any]) -> dict[str, Any]:
    answer = str(rec.get("answer", ""))
    trace = str(rec.get("think_trace", ""))
    return {
        "id": record_id,
        "ts": rec.get("ts"),
        "question_id": rec.get("question_id") or "Q-?",
        "question": rec.get("question", ""),
        "answer_preview": " ".join(answer.split())[:120],
        "ok": bool(rec.get("ok", True)),
        "seconds": rec.get("duration_s"),
        "tools": len(_TOOL_CALL_RE.findall(trace)),
    }


def list_history(limit: int = 20) -> list[dict[str, Any]]:
    records = _iter_records()
    return [_summarize(rid, rec) for rid, _line, rec in reversed(records[-limit:])]


def get_record(record_id: str) -> dict[str, Any] | None:
    for rid, _line, rec in _iter_records():
        if rid == record_id:
            return {
                "id": rid,
                "ts": rec.get("ts"),
                "question_id": rec.get("question_id") or "Q-?",
                "question": rec.get("question", ""),
                "answer": rec.get("answer", ""),
                "think_trace": rec.get("think_trace", ""),
                "retrieved_context": rec.get("retrieved_context", ""),
                "ok": bool(rec.get("ok", True)),
                "seconds": rec.get("duration_s"),
            }
    return None


def _rewrite(keep: "callable[[str], bool]") -> int:
    """keep(줄 id)가 False인 줄을 제거하고 원자적으로 교체. 제거 수 반환."""
    with LOG_LOCK:
        try:
            if not ANSWER_LOG_PATH.exists():
                return 0
            lines = ANSWER_LOG_PATH.read_text(encoding="utf-8").splitlines()
            kept = [l for l in lines if not l.strip() or keep(_line_id(l.strip()))]
            removed = len(lines) - len(kept)
            if removed == 0:
                return 0
            tmp = ANSWER_LOG_PATH.with_suffix(".tmp")
            tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
            tmp.replace(ANSWER_LOG_PATH)
            return removed
        except OSError:
            logger.exception("히스토리 삭제 실패")
            return 0


def delete(record_id: str) -> bool:
    """해당 기록을 로그에서 영구 삭제한다."""
    return _rewrite(lambda rid: rid != record_id) > 0


def delete_many(record_ids: list[str]) -> int:
    """여러 기록을 한 번의 재작성으로 삭제한다 — 개별 DELETE를 N번 호출하면
    파일을 N번 다시 쓰게 되고 그 사이 새 기록이 끼면 경합이 난다."""
    targets = {str(r) for r in record_ids if r}
    if not targets:
        return 0
    return _rewrite(lambda rid: rid not in targets)


def delete_all() -> int:
    """질문이 있는 기록 전부를 영구 삭제한다 (깨진 줄·빈 질문 노이즈 포함 정리)."""
    return _rewrite(lambda rid: False)
