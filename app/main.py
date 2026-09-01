import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.routes import router
from app.config import CLOVA_API_KEY, CLOVA_MODEL
from app.data.fulltext import is_index_built
from app.data.manifest import load_corpus

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    load_corpus()  # fail fast on bad manifest/universe files, avoid first-request latency
    if not CLOVA_API_KEY:
        logger.warning("CLOVA_API_KEY 가 설정되지 않았습니다 — /answer, /chat 은 폴백 응답만 반환합니다")
    if not is_index_built():
        logger.warning("FTS 인덱스가 없습니다 — scripts/build_index.py 실행 전까지 search_fulltext 는 0건입니다")
    yield


app = FastAPI(
    title="공시 Agent (Disclosure Analyst)",
    description="70개사 DART 공시 코퍼스(2023-01 ~ 2026-03) 자연어 질의응답 — HyperCLOVA X 기반.",
    lifespan=_lifespan,
)
app.include_router(router)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/dashboard", include_in_schema=False)
def dashboard() -> FileResponse:
    """브라우저에서 API를 시험해 볼 수 있는 대시보드."""
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


@app.get("/health")
def health() -> dict[str, object]:
    try:
        fts = is_index_built()
    except Exception:  # noqa: BLE001 - health must never fail
        fts = False
    return {
        "status": "ok",
        "model": CLOVA_MODEL,
        "clova_key_set": bool(CLOVA_API_KEY),
        "fts_index_built": fts,
    }
