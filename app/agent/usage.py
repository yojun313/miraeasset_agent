"""CLOVA API 사용량 누적 집계.

CLOVA Studio API 키로는 요금·크레딧 잔액을 조회할 수 없다(콘솔에서만 확인
가능하고, NCP Cost and Usage API는 별도의 Access/Secret Key를 요구한다).
대신 모든 응답에 실려 오는 usage 필드를 직접 합산해 기록한다 — 이 서버가
쓴 양은 이 방식이 가장 정확하다.

일자별로 묶어 JSON 한 파일에 보관한다. 집계 실패가 답변을 막아서는 안 되므로
모든 경로에서 예외를 삼킨다.
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from typing import Any

from app.config import USAGE_LOG_PATH

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_KEEP_DAYS = 60


def _empty_day() -> dict[str, Any]:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def _load() -> dict[str, Any]:
    try:
        if USAGE_LOG_PATH.exists():
            data = json.loads(USAGE_LOG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("days"), dict):
                return data
    except (OSError, ValueError):
        logger.warning("사용량 파일을 읽지 못했습니다 — 새로 시작합니다", exc_info=True)
    return {"days": {}, "models": {}}


def _save(data: dict[str, Any]) -> None:
    try:
        USAGE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_LOG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(USAGE_LOG_PATH)  # 원자적 교체 — 중간에 죽어도 파일이 깨지지 않는다
    except OSError:
        logger.warning("사용량 파일을 저장하지 못했습니다", exc_info=True)


def record(usage: dict[str, Any] | None, model: str) -> None:
    """응답 한 건의 토큰 사용량을 누적한다. 절대 예외를 던지지 않는다."""
    if not usage:
        return
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        total = int(usage.get("total_tokens") or (prompt + completion))
    except (TypeError, ValueError):
        return

    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
    try:
        with _lock:
            data = _load()
            day = data["days"].setdefault(today, _empty_day())
            per_model = data.setdefault("models", {}).setdefault(model, _empty_day())
            for bucket in (day, per_model):
                bucket["calls"] += 1
                bucket["prompt_tokens"] += prompt
                bucket["completion_tokens"] += completion
                bucket["total_tokens"] += total
            # 오래된 날짜는 버려 파일이 무한정 자라지 않게 한다
            if len(data["days"]) > _KEEP_DAYS:
                for old in sorted(data["days"])[:-_KEEP_DAYS]:
                    del data["days"][old]
            _save(data)
    except Exception:  # noqa: BLE001 - 집계 실패가 답변을 막으면 안 된다
        logger.exception("사용량 집계 실패")


def summary() -> dict[str, Any]:
    """대시보드용 요약: 오늘 / 전체 / 최근 7일 / 모델별."""
    try:
        with _lock:
            data = _load()
    except Exception:  # noqa: BLE001
        data = {"days": {}, "models": {}}

    days: dict[str, Any] = data.get("days", {})
    today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")

    total = _empty_day()
    for bucket in days.values():
        for key in total:
            total[key] += int(bucket.get(key) or 0)

    recent = [
        {"date": d, **days[d]} for d in sorted(days)[-7:]
    ]
    return {
        "today": {"date": today, **days.get(today, _empty_day())},
        "total": total,
        "recent_days": recent,
        "by_model": data.get("models", {}),
        "note": (
            "이 서버가 호출한 CLOVA API 사용량 누적치입니다. "
            "실제 청구 금액·크레딧 잔액은 NCP 콘솔에서 확인하세요 "
            "(CLOVA API 키로는 조회할 수 없습니다)."
        ),
    }
