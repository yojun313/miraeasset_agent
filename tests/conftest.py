"""pytest가 실제 운영 로그(logs/answers.jsonl)를 오염시키지 않도록 격리한다.

TestClient로 /answer 를 호출하는 테스트가 실제 감사 로그에 기록을 남기면
대시보드 히스토리에 '캐시 테스트 질문' 같은 노이즈가 그대로 노출된다."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_answer_log(monkeypatch, tmp_path):
    import app.api.history as history
    import app.api.routes as routes
    from app import config

    log = tmp_path / "answers.jsonl"
    usage = tmp_path / "usage.json"

    monkeypatch.setattr(config, "ANSWER_LOG_PATH", log)
    monkeypatch.setattr(routes, "ANSWER_LOG_PATH", log)
    monkeypatch.setattr(history, "ANSWER_LOG_PATH", log)

    import app.agent.usage as usage_module
    monkeypatch.setattr(usage_module, "USAGE_LOG_PATH", usage)
