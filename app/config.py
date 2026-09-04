from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).resolve().parent.parent
CORPUS_DIR = ROOT_DIR / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.jsonl"
UNIVERSE_PATH = CORPUS_DIR / "universe.csv"

CACHE_DIR = ROOT_DIR / "cache"
TEXT_CACHE_DIR = CACHE_DIR / "text"
FTS_DB_PATH = CACHE_DIR / "fulltext.sqlite3"

LOG_DIR = ROOT_DIR / "logs"
ANSWER_LOG_PATH = LOG_DIR / "answers.jsonl"
USAGE_LOG_PATH = LOG_DIR / "usage.json"


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(f"환경변수 {name}={raw!r} 는 정수여야 합니다") from e


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ValueError(f"환경변수 {name}={raw!r} 는 숫자여야 합니다") from e


# --- CLOVA Studio (HyperCLOVA X) ---
# 대회 규정상 LLM은 HyperCLOVA X만 허용된다. 이 코드베이스의 모든 LLM 호출은
# app.agent.clova 를 통해서만 나가야 한다.
CLOVA_API_KEY = os.environ.get("CLOVA_API_KEY", "")
CLOVA_MODEL = os.environ.get("CLOVA_MODEL", "HCX-007")
CLOVA_BASE_URL = os.environ.get(
    "CLOVA_BASE_URL", "https://clovastudio.stream.ntruss.com/v1/openai"
).rstrip("/")
CLOVA_REQUEST_TIMEOUT = _env_float("CLOVA_REQUEST_TIMEOUT", 90.0)  # HTTP 호출당, 초
CLOVA_MAX_RETRIES = _env_int("CLOVA_MAX_RETRIES", 4)  # 429/5xx/네트워크 오류 재시도 횟수
# 계정 한도는 분당 60요청 / 60,000토큰(응답 헤더 x-ratelimit-* 로 확인). 동시 호출을
# 늘리면 토큰 한도를 순식간에 소진하므로 기본 1로 직렬화한다.
CLOVA_MAX_CONCURRENCY = _env_int("CLOVA_MAX_CONCURRENCY", 1)
CLOVA_MIN_INTERVAL = _env_float("CLOVA_MIN_INTERVAL", 1.1)  # 연속 호출 최소 간격(초)
CLOVA_TOKEN_RESERVE = _env_int("CLOVA_TOKEN_RESERVE", 12_000)  # 남은 토큰이 이 아래면 리셋까지 대기
CLOVA_TEMPERATURE = _env_float("CLOVA_TEMPERATURE", 0.2)  # 음수로 두면 필드 자체를 생략
CLOVA_MAX_OUTPUT_TOKENS = _env_int("CLOVA_MAX_OUTPUT_TOKENS", 1536)
CLOVA_ANSWER_OUTPUT_TOKENS = _env_int("CLOVA_ANSWER_OUTPUT_TOKENS", 3072)

# --- agent ---
MAX_AGENT_TURNS = _env_int("MAX_AGENT_TURNS", 12)
ANSWER_TIME_BUDGET = _env_float("ANSWER_TIME_BUDGET", 240.0)  # /answer 요청당 벽시계 예산(초)
ANSWER_CONTEXT_MAX_CHARS = _env_int("ANSWER_CONTEXT_MAX_CHARS", 30_000)
