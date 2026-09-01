"""Minimal CLOVA Studio chat-completions client (OpenAI-compatible wire
format).

We deliberately do not use the openai SDK: the surface we need is a single
POST endpoint, and owning the HTTP layer gives explicit control over
timeouts, retries, concurrency, and error types — which matters for an
evaluation server that must never hang or crash.

Error contract:
  - ClovaConfigError:  permanent local misconfiguration (missing API key).
  - ClovaAPIError:     the API refused the request (non-retryable 4xx) or
                       stayed unavailable through all retries.
  - DeadlineExceeded:  the caller's wall-clock budget ran out.
All are subclasses of ClovaError; nothing else escapes chat_completion().
"""

from __future__ import annotations

import logging
import random
import threading
import time
from typing import Any

import httpx

from app.config import (
    CLOVA_API_KEY,
    CLOVA_BASE_URL,
    CLOVA_MAX_CONCURRENCY,
    CLOVA_MAX_OUTPUT_TOKENS,
    CLOVA_MAX_RETRIES,
    CLOVA_MIN_INTERVAL,
    CLOVA_MODEL,
    CLOVA_REQUEST_TIMEOUT,
    CLOVA_TEMPERATURE,
    CLOVA_TOKEN_RESERVE,
)

logger = logging.getLogger(__name__)


class ClovaError(Exception):
    """Base class for every failure raised by this module."""


class ClovaConfigError(ClovaError):
    """The client is not configured (e.g. CLOVA_API_KEY missing)."""


class ClovaAPIError(ClovaError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class DeadlineExceeded(ClovaError):
    """The caller's time budget ran out before or while calling the API."""


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}
_MAX_LIMIT_WAIT_S = 65.0  # rate-limit 창(60초)보다 조금 길게

_http_client: httpx.Client | None = None
_http_lock = threading.Lock()
# Cap simultaneous CLOVA calls so a burst of evaluation requests degrades
# into queueing instead of a rate-limit storm.
_semaphore = threading.BoundedSemaphore(max(1, CLOVA_MAX_CONCURRENCY))


def _parse_seconds(raw: str | None) -> float | None:
    """'49s' / '1m30s' / '12' 형태의 헤더 값을 초로 바꾼다."""
    if not raw:
        return None
    text = raw.strip().lower()
    try:
        return float(text)
    except ValueError:
        pass
    total, number = 0.0, ""
    for ch in text:
        if ch.isdigit() or ch == ".":
            number += ch
        elif ch in ("h", "m", "s") and number:
            total += float(number) * {"h": 3600, "m": 60, "s": 1}[ch]
            number = ""
        else:
            number = ""
    return total or None


class _RateLimitGovernor:
    """계정 한도(분당 60요청 / 60,000토큰)를 응답 헤더로 추적해 선제적으로 쉰다.

    한도를 넘긴 뒤 429를 받고 재시도하는 것보다, 남은 토큰이 바닥나기 전에
    창이 리셋될 때까지 기다리는 편이 전체 지연이 짧고 실패도 없다."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._blocked_until = 0.0

    def _wait(self, seconds: float, deadline: float | None, reason: str) -> None:
        if seconds <= 0:
            return
        seconds = min(seconds, _MAX_LIMIT_WAIT_S)
        if deadline is not None and time.monotonic() + seconds >= deadline:
            raise DeadlineExceeded(f"{reason} 대기 중 시간 예산 소진 ({seconds:.0f}s 필요)")
        logger.info("rate limit: %s — %.1fs 대기", reason, seconds)
        time.sleep(seconds)

    def before_request(self, deadline: float | None) -> None:
        with self._lock:
            now = time.monotonic()
            blocked_for = self._blocked_until - now
            since_last = now - self._last_call
            gap = CLOVA_MIN_INTERVAL - since_last if self._last_call else 0.0
        if blocked_for > 0:
            self._wait(blocked_for, deadline, "토큰 한도 소진")
        elif gap > 0:
            time.sleep(gap)
        with self._lock:
            self._last_call = time.monotonic()

    def observe(self, headers: Any) -> None:
        """응답 헤더의 잔여 한도를 보고, 다음 호출을 막아야 하면 표시해 둔다."""
        try:
            remaining_tokens = headers.get("x-ratelimit-remaining-tokens")
            remaining_requests = headers.get("x-ratelimit-remaining-requests")
            reset = _parse_seconds(
                headers.get("x-ratelimit-reset-tokens") or headers.get("x-ratelimit-reset-requests")
            )
            low = (remaining_tokens is not None and int(remaining_tokens) < CLOVA_TOKEN_RESERVE) or (
                remaining_requests is not None and int(remaining_requests) < 2
            )
            if low and reset:
                with self._lock:
                    self._blocked_until = max(self._blocked_until, time.monotonic() + reset)
                logger.info(
                    "rate limit 여유 부족 (tokens=%s, requests=%s) — %.0fs 후 재개",
                    remaining_tokens, remaining_requests, reset,
                )
        except (TypeError, ValueError):
            pass  # 헤더 형식이 예상과 다르면 그냥 무시하고 진행

    def penalize(self, headers: Any, attempt: int, deadline: float | None) -> None:
        """429를 받았을 때: 리셋 헤더가 알려주는 만큼 실제로 기다린다."""
        reset = _parse_seconds(
            (headers.get("retry-after") if headers else None)
            or (headers.get("x-ratelimit-reset-tokens") if headers else None)
            or (headers.get("x-ratelimit-reset-requests") if headers else None)
        )
        # 헤더가 없으면 창 길이(60초)를 모르므로 점증 백오프로 대체
        delay = reset if reset else min(_MAX_LIMIT_WAIT_S, 5.0 * (2**attempt))
        self._wait(delay + random.uniform(0.0, 0.5), deadline, "HTTP 429")


_governor = _RateLimitGovernor()

# CLOVA's OpenAI-compatible layer may accept either token-cap field name
# depending on model/rollout. Start with the modern one; if the API rejects
# it with a 400 that names the field, flip to the legacy name once,
# process-wide.
_token_param = "max_completion_tokens"
_token_param_lock = threading.Lock()


def _http() -> httpx.Client:
    global _http_client
    with _http_lock:
        if _http_client is None:
            _http_client = httpx.Client(
                timeout=httpx.Timeout(CLOVA_REQUEST_TIMEOUT, connect=10.0),
                limits=httpx.Limits(max_connections=max(4, CLOVA_MAX_CONCURRENCY * 2)),
            )
        return _http_client


def _sleep_before_retry(attempt: int, retry_after: str | None, deadline: float | None) -> None:
    delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0.0, 0.5)
    if retry_after:
        try:
            delay = max(delay, min(30.0, float(retry_after)))
        except ValueError:
            pass  # non-numeric Retry-After header; keep the computed backoff
    if deadline is not None and time.monotonic() + delay >= deadline:
        raise DeadlineExceeded("재시도 대기 중 시간 예산 소진")
    time.sleep(delay)


def _extract_message(data: Any) -> dict[str, Any]:
    try:
        message = data["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ClovaAPIError(f"CLOVA 응답 형식이 예상과 다릅니다: {str(data)[:300]}") from e
    if not isinstance(message, dict):
        raise ClovaAPIError(f"CLOVA 응답의 message 가 객체가 아닙니다: {str(message)[:300]}")
    return message


def _meta_from(data: Any, headers: Any) -> dict[str, Any]:
    """usage(토큰 사용량) + 계정 잔여 한도를 관측용으로 뽑아낸다."""
    usage = data.get("usage") if isinstance(data, dict) else None
    meta: dict[str, Any] = {"usage": usage if isinstance(usage, dict) else {}}
    try:
        meta["rate_limit"] = {
            "remaining_tokens": headers.get("x-ratelimit-remaining-tokens"),
            "limit_tokens": headers.get("x-ratelimit-limit-tokens"),
            "remaining_requests": headers.get("x-ratelimit-remaining-requests"),
            "limit_requests": headers.get("x-ratelimit-limit-requests"),
            "reset": headers.get("x-ratelimit-reset-tokens")
            or headers.get("x-ratelimit-reset-requests"),
        }
    except AttributeError:
        meta["rate_limit"] = {}
    return meta


def chat_completion(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None = None,
    deadline: float | None = None,
    temperature: float | None = None,
    max_output_tokens: int | None = None,
    on_meta: Any = None,
) -> dict[str, Any]:
    """One chat-completions call. Returns the assistant message dict
    ({role, content, tool_calls?, reasoning_content?}).

    `deadline` is a time.monotonic() value; the call refuses to start (and
    refuses to back off) past it. Transient failures (429/5xx/network/
    malformed 200 body) are retried with exponential backoff + jitter,
    honoring a numeric Retry-After header when present.
    """
    if not CLOVA_API_KEY:
        raise ClovaConfigError("CLOVA_API_KEY 가 설정되지 않았습니다 (.env.example 참고)")

    url = f"{CLOVA_BASE_URL}/chat/completions"
    headers = {"Authorization": f"Bearer {CLOVA_API_KEY}", "Content-Type": "application/json"}

    def build_payload() -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": CLOVA_MODEL,
            "messages": messages,
            _token_param: (
                CLOVA_MAX_OUTPUT_TOKENS if max_output_tokens is None else max_output_tokens
            ),
        }
        temp = CLOVA_TEMPERATURE if temperature is None else temperature
        if temp >= 0:  # 음수 설정 = temperature 필드 생략 (모델 기본값 사용)
            payload["temperature"] = temp
        if tools:
            payload["tools"] = tools
        # HCX-007은 reasoning(thinking)이 기본 활성인데 tool 사용과 동시에 쓸 수
        # 없다 (400 "Invalid parameter: tools, reasoning"). tools를 보낼 때뿐
        # 아니라 **대화 이력에 tool 메시지가 남아 있기만 해도** 거부되므로,
        # 에이전트 루프의 모든 호출에서 항상 명시적으로 끈다.
        # (라이브 검증: 2026-09-01, tools 없이 reasoning_effort=none → 200 OK)
        payload["reasoning_effort"] = "none"
        return payload

    global _token_param
    last_error = "no attempt made"
    attempt = 0
    while attempt <= CLOVA_MAX_RETRIES:
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded("LLM 호출 전 시간 예산 소진")
        try:
            with _semaphore:
                _governor.before_request(deadline)
                resp = _http().post(url, headers=headers, json=build_payload())
            _governor.observe(resp.headers)
        except httpx.HTTPError as e:
            last_error = f"{type(e).__name__}: {e}"
            logger.warning(
                "CLOVA request failed (attempt %d/%d): %s",
                attempt + 1, CLOVA_MAX_RETRIES + 1, last_error,
            )
            if attempt < CLOVA_MAX_RETRIES:
                _sleep_before_retry(attempt, None, deadline)
            attempt += 1
            continue

        if resp.status_code == 200:
            try:
                data = resp.json()
            except ValueError:
                last_error = f"200이지만 JSON이 아닌 응답: {resp.text[:200]}"
                logger.warning("CLOVA returned non-JSON 200 body (attempt %d)", attempt + 1)
                if attempt < CLOVA_MAX_RETRIES:
                    _sleep_before_retry(attempt, None, deadline)
                attempt += 1
                continue
            if on_meta is not None:
                try:
                    on_meta(_meta_from(data, resp.headers))
                except Exception:  # noqa: BLE001 - 관측 실패가 응답을 막으면 안 된다
                    logger.exception("on_meta callback failed")
            return _extract_message(data)

        body = resp.text[:500]

        # One-time field-name adaptation; does not consume a retry attempt.
        if (
            resp.status_code == 400
            and _token_param == "max_completion_tokens"
            and "max_completion_tokens" in body
        ):
            with _token_param_lock:
                _token_param = "max_tokens"
            logger.info("CLOVA rejected max_completion_tokens; switching to max_tokens")
            last_error = f"HTTP 400 (token param): {body}"
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < CLOVA_MAX_RETRIES:
            last_error = f"HTTP {resp.status_code}: {body}"
            logger.warning(
                "CLOVA HTTP %d (attempt %d/%d): %s",
                resp.status_code, attempt + 1, CLOVA_MAX_RETRIES + 1, body[:200],
            )
            if resp.status_code == 429:
                # 분당 창이 리셋될 때까지 실제로 기다린다 — 짧은 지수 백오프로는
                # 같은 창 안에서 계속 429를 맞는다.
                _governor.penalize(resp.headers, attempt, deadline)
            else:
                _sleep_before_retry(attempt, resp.headers.get("Retry-After"), deadline)
            attempt += 1
            continue

        raise ClovaAPIError(
            f"CLOVA API 오류 HTTP {resp.status_code}: {body}", status_code=resp.status_code
        )

    raise ClovaAPIError(f"CLOVA API {CLOVA_MAX_RETRIES + 1}회 시도 후에도 실패: {last_error}")
