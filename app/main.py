from fastapi import FastAPI

from app.api.routes import router
from app.data.manifest import load_corpus

app = FastAPI(
    title="DART Filing Research Assistant",
    description="Natural-language Q&A over a 70-company DART filing corpus (2023-01 ~ 2026-03).",
)
app.include_router(router)


@app.on_event("startup")
def _warm_corpus() -> None:
    load_corpus()  # fail fast on bad manifest/universe files, avoid first-request latency


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
