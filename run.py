"""서버 실행 진입점.

    uv run python run.py            # 0.0.0.0:8000
    PORT=8080 uv run python run.py  # 포트 변경
    RELOAD=1 uv run python run.py   # 개발용 자동 리로드
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _port() -> int:
    raw = os.getenv("PORT", "8000").strip()
    try:
        return int(raw)
    except ValueError:
        raise SystemExit(f"PORT={raw!r} 는 정수여야 합니다")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=os.getenv("HOST", "0.0.0.0"),
        port=_port(),
        reload=os.getenv("RELOAD", "").strip() in ("1", "true", "yes"),
    )
