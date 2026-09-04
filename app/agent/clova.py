from __future__ import annotations

import json
import logging
import random
import threading
import time
from typing import Any

import httpx

from app.agent import usage as usage_module
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
    def __init__(self, message: str, status_code: int | None = None, code: str | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code  # CLOVA 오류 코드 (예: "40009" unsupported function)

    @property
    def is_unsupported_function(self) -> bool:
        """모델이 목록에 없는 함수명을 지어내 API가 응답을 거부한 경우."""
        return self.code == "40009"


class DeadlineExceeded(ClovaError):
    """The caller's time budget ran out before or while calling the API."""


_RETRYABLE_STATUS = {408, 429, 500, 502, 503, 504}

_RETRYABLE_400_MARKERS = ("40009", "Unsupported function")
_MAX_LIMIT_WAIT_S = 65.0

_http_client: httpx.Client | None = None
_http_lock = threading.Lock()
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

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_call = 0.0
        self._blocked_until = 0.0
        self._remaining_tokens: int | None = None
        self._window_ends: float = 0.0

    def _wait(self, seconds: float, deadline: float | None, reason: str) -> None:
        if seconds <= 0:
            return
        seconds = min(seconds, _MAX_LIMIT_WAIT_S)
        if deadline is not None and time.monotonic() + seconds >= deadline:
            raise DeadlineExceeded(f"{reason} 대기 중 시간 예산 소진 ({seconds:.0f}s 필요)")
        logger.info("rate limit: %s — %.1fs 대기", reason, seconds)
        time.sleep(seconds)

    def before_request(self, deadline: float | None, need_tokens: int = 0) -> None:
        """호출 전 대기. `need_tokens`(입력 추정 + 출력 예약)가 이번 창의
        잔여 토큰을 넘으면 창이 리셋될 때까지 기다린다 — 429를 맞고 재시도하는
        것보다 빠르고 확실하다."""
        with self._lock:
            now = time.monotonic()
            blocked_for = self._blocked_until - now
            since_last = now - self._last_call
            gap = CLOVA_MIN_INTERVAL - since_last if self._last_call else 0.0
            # 이번 요청이 잔여 한도를 넘길 것으로 보이면 창 종료까지 대기
            short_by = 0.0
            if (
                need_tokens
                and self._remaining_tokens is not None
                and need_tokens > self._remaining_tokens
                and self._window_ends > now
            ):
                short_by = self._window_ends - now

        if blocked_for > 0:
            self._wait(blocked_for, deadline, "토큰 한도 소진")
        elif short_by > 0:
            self._wait(
                short_by, deadline,
                f"예상 {need_tokens:,} 토큰 > 잔여 {self._remaining_tokens:,}",
            )
        elif gap > 0:
            time.sleep(gap)
        with self._lock:
            self._last_call = time.monotonic()

    def observe(self, headers: Any) -> None:
        try:
            remaining_tokens = headers.get("x-ratelimit-remaining-tokens")
            remaining_requests = headers.get("x-ratelimit-remaining-requests")
            reset = _parse_seconds(
                headers.get("x-ratelimit-reset-tokens") or headers.get("x-ratelimit-reset-requests")
            )
            if remaining_tokens is not None and reset:
                with self._lock:
                    self._remaining_tokens = int(remaining_tokens)
                    self._window_ends = time.monotonic() + reset
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
        delay = reset if reset else min(_MAX_LIMIT_WAIT_S, 5.0 * (2**attempt))
        self._wait(delay + random.uniform(0.0, 0.5), deadline, "HTTP 429")


_governor = _RateLimitGovernor()

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


_CHARS_PER_TOKEN = 1.37


def estimate_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    try:
        payload = json.dumps(messages, ensure_ascii=False, default=str)
        if tools:
            payload += json.dumps(tools, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        payload = str(messages) + str(tools or "")
    return int(len(payload) / _CHARS_PER_TOKEN) + 1


def count_tokens_exact(messages: list[dict[str, Any]]) -> int | None:
    if not CLOVA_API_KEY:
        return None
    url = f"{CLOVA_BASE_URL.split('/v1/openai')[0]}/v3/api-tools/chat-tokenize/{CLOVA_MODEL}"
    try:
        resp = _http().post(
            url,
            headers={"Authorization": f"Bearer {CLOVA_API_KEY}", "Content-Type": "application/json"},
            json={"messages": messages},
        )
        if resp.status_code != 200:
            return None
        parts = resp.json()["result"]["messages"]
        return sum(p.get("count", 0) for m in parts for p in m.get("content", []))
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        logger.debug("token calculator unavailable", exc_info=True)
        return None


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
        if temp >= 0:
            payload["temperature"] = temp
        if tools:
            payload["tools"] = tools
        payload["reasoning_effort"] = "none"
        return payload

    global _token_param
    last_error = "no attempt made"
    attempt = 0
    while attempt <= CLOVA_MAX_RETRIES:
        if deadline is not None and time.monotonic() >= deadline:
            raise DeadlineExceeded("LLM 호출 전 시간 예산 소진")
        try:
            payload = build_payload()
            need = estimate_tokens(messages, tools) + int(payload.get(_token_param, 0) or 0)
            with _semaphore:
                _governor.before_request(deadline, need_tokens=need)
                resp = _http().post(url, headers=headers, json=payload)
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
            meta = _meta_from(data, resp.headers)
            usage_module.record(meta.get("usage"), CLOVA_MODEL)
            if on_meta is not None:
                try:
                    on_meta(meta)
                except Exception: 
                    logger.exception("on_meta callback failed")
            return _extract_message(data)

        body = resp.text[:500]

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

        if (
            resp.status_code == 400
            and any(m in body for m in _RETRYABLE_400_MARKERS)
            and attempt < CLOVA_MAX_RETRIES
        ):
            last_error = f"HTTP 400 (unsupported function): {body}"
            logger.warning(
                "CLOVA rejected a hallucinated function call (attempt %d/%d) — 재시도",
                attempt + 1, CLOVA_MAX_RETRIES + 1,
            )
            _sleep_before_retry(attempt, None, deadline)
            attempt += 1
            continue

        if resp.status_code in _RETRYABLE_STATUS and attempt < CLOVA_MAX_RETRIES:
            last_error = f"HTTP {resp.status_code}: {body}"
            logger.warning(
                "CLOVA HTTP %d (attempt %d/%d): %s",
                resp.status_code, attempt + 1, CLOVA_MAX_RETRIES + 1, body[:200],
            )
            if resp.status_code == 429:
                _governor.penalize(resp.headers, attempt, deadline)
            else:
                _sleep_before_retry(attempt, resp.headers.get("Retry-After"), deadline)
            attempt += 1
            continue

        code = None
        try:
            code = str(resp.json()["error"]["code"])
        except Exception:
            pass
        raise ClovaAPIError(
            f"CLOVA API 오류 HTTP {resp.status_code}: {body}",
            status_code=resp.status_code,
            code=code,
        )

    raise ClovaAPIError(f"CLOVA API {CLOVA_MAX_RETRIES + 1}회 시도 후에도 실패: {last_error}")
