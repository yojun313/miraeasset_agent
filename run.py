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
        # 운영 서버에서는 끈다. 켜 두면 파일 편집·테스트 저장마다 재시작돼 진행 중인
        # 질의가 끊긴다. 개발 중에만 RELOAD=1 로 켠다.
        reload=os.getenv("RELOAD", "").strip().lower() in ("1", "true", "yes"),
        # 열린 SSE 연결이 있어도 Ctrl+C 후 3초 안에 강제 종료한다
        # (기본값은 무한 대기 — 대시보드 탭이 열려 있으면 서버가 안 죽는다)
        timeout_graceful_shutdown=3,
    )
