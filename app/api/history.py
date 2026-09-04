from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from typing import Any

from app.config import ANSWER_LOG_PATH

logger = logging.getLogger(__name__)

LOG_LOCK = threading.Lock()

_TOOL_CALL_RE = re.compile(r"도구 호출:")


def _line_id(raw_line: str) -> str:
    return hashlib.sha1(raw_line.strip().encode("utf-8")).hexdigest()[:12]


def _iter_records() -> list[tuple[str, str, dict[str, Any]]]:
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
    return _rewrite(lambda rid: rid != record_id) > 0


def delete_many(record_ids: list[str]) -> int:
    targets = {str(r) for r in record_ids if r}
    if not targets:
        return 0
    return _rewrite(lambda rid: rid not in targets)


def delete_all() -> int:
    return _rewrite(lambda rid: False)
