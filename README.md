# 공시 Agent (dis-047)

제10회 미래에셋증권 AI Festival 예선 제출물입니다. 주최 측이 제공한 70개 상장사의 DART 공시
원문(정기·주요사항·거래소·지분공시 4,204건, 2023.01 ~ 2026년 1분기)을 근거로 자연어 질의에 답하는
API 서버입니다. LLM은 HyperCLOVA X(HCX-007)만 사용합니다.

- 평가용 엔드포인트와 요청/응답 명세: [`API서버정보.md`](API서버정보.md)
- 설계와 구현 설명: [`기술제안서.md`](기술제안서.md)
- 데이터 설명: [`corpus/README.md`](corpus/README.md), 수집 기준: [`corpus/data_filter.md`](corpus/data_filter.md)

## 동작 방식

질문이 들어오면 HCX-007이 여섯 개의 도구(기업 조회, 메타데이터 검색·집계, 본문 전문검색, 목차 조회,
섹션 읽기, 계산)를 골라 부르며 공시를 찾고 읽은 뒤 답을 씁니다. 건수·합계·순위·비율·기간·정정 이력처럼
계산이 필요한 값은 도구가 미리 계산해 결과에 실어 보내고, 모델은 그 값을 옮겨 적습니다. 결과를 읽지 않고
포기하거나, 없는 접수번호를 인용하거나, 외부 사이트를 안내하는 응답은 서버가 잡아 다시 쓰게 합니다.

벡터 임베딩은 쓰지 않습니다. 메타데이터 필터(DuckDB)와 본문 키워드 검색(SQLite FTS5)으로 문서를
특정하고, 필요한 섹션만 읽습니다.

## 환경

- Python 3.12 이상, [uv](https://docs.astral.sh/uv/)
- 의존성은 `pyproject.toml`과 `uv.lock`에 고정돼 있습니다. `uv sync`가 가상환경을 만들고 같은 버전을 설치합니다.
- CLOVA Studio API 키 (HCX-007 호출용)
- 디스크 약 9GB (corpus 5.3GB + cache 3.2GB), 메모리 1GB면 동작합니다 (실측 RSS 약 200MB)

## 실행

```bash
uv sync
cp .env.example .env            # CLOVA_API_KEY 를 채웁니다

uv run python scripts/fetch_data.py   # corpus/ 와 cache/ 복원 (아래 참고)

uv run python run.py                  # http://0.0.0.0:8000
```

환경변수(`.env`):

| 변수 | 기본값 | 설명 |
|---|---|---|
| `CLOVA_API_KEY` | (없음) | CLOVA Studio API 키. 없으면 서버는 뜨지만 답변 대신 폴백 문구를 돌려줍니다 |
| `CLOVA_MODEL` | `HCX-007` | 사용할 모델 |
| `PORT` | `8000` | 서비스 포트 |
| `HOST` | `0.0.0.0` | 바인드 주소 |
| `RELOAD` | `false` | `1`이면 파일 변경 시 자동 재시작. 개발용이며 운영에서는 끕니다 (재시작 중 진행 중인 질의가 끊깁니다) |

확인:

```bash
curl -s localhost:8000/health
# {"status":"ok","model":"HCX-007","clova_key_set":true,"fts_index_built":true}

curl -G "http://localhost:8000/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=삼성전자의 2024년 연결 기준 매출액은 얼마인가?"
```

배포된 서버는 `scripts/test_api.py`로 점검할 수 있습니다 (`--url`로 대상 지정, `--quick`은 health와 질문 1개).

## 데이터 세팅

`corpus/`(공시 원문 + `manifest.jsonl` + `universe.csv`)와 `cache/`(파싱된 본문·섹션 오프셋 + FTS5 색인)는
git에 포함하지 않습니다. 서버 실행에는 둘 다 필요합니다.

**릴리즈에서 받기 (권장)**

```bash
uv run python scripts/fetch_data.py               # 없는 디렉토리만 받음
uv run python scripts/fetch_data.py --force       # 지우고 다시 받음
uv run python scripts/fetch_data.py --only cache  # 하나만
```

GitHub 릴리즈 `data-v1`의 zstd 아카이브(corpus 약 290MB, cache 약 1.2GB)를 받아 SHA-256을 확인하고
풉니다. 레포는 git remote에서 자동으로 알아내므로 포크나 조직 이관 뒤에도 그대로 동작하고,
`GITHUB_REPO=owner/repo`로 강제할 수 있습니다. 압축 해제는 시스템 `zstd`가 있으면 그것을, 없으면
파이썬 `zstandard`를 씁니다. 임시 파일은 `/tmp`가 아니라 레포 디렉토리에 만듭니다 (`/tmp`가 램 디스크인
서버에서 용량 부족이 났던 경험 때문입니다).

비공개 레포라 인증이 필요합니다. `gh auth login`이 된 gh CLI가 있으면 그것을 쓰고, 없으면
`GITHUB_TOKEN` 환경변수(repo 읽기 권한)로 GitHub API에서 받습니다.

**corpus/ 에서 cache/ 를 직접 만들기**

```bash
uv run python scripts/build_index.py                       # 전체 4,204건 파싱 + 색인, 수 분 소요
uv run python scripts/build_index.py --company 삼성전자     # 일부만 (확인용)
```

색인 없이 서버를 띄워도 검색·읽기는 온디맨드로 파싱해 동작합니다. 다만 본문 전문검색은 색인된 문서만
대상이므로 실제 운영에서는 전체 색인이 필요합니다.

데이터를 갱신해 새 릴리즈를 올릴 때는 `bash scripts/publish_data.sh data-v2`를 쓰고
`scripts/fetch_data.py`의 `TAG`를 맞춥니다.

## API

평가용:

- `GET /answer?question_id=&question=` → `{question_id, question, retrieved_context, think_trace, answer}`.
  항상 HTTP 200을 돌려주며, 같은 질의의 재호출은 첫 성공 응답을 캐시에서 반환합니다.
- `GET /health`

운영·확인용:

- `GET /dashboard` — 웹 대시보드. 질의 실행, 도구 호출 과정과 근거 본문 실시간 표시, 질의 기록 관리
- `POST /runs`, `GET /events` — 대시보드가 쓰는 실행 시작과 SSE 이벤트 스트림
- `GET /answer/stream?question_id=&question=` — 같은 파이프라인을 SSE로 스트리밍
- `GET /history`, `GET /history/{id}`, `DELETE /history/{id}`, `POST /history/delete` — 질의 기록 (파일에 저장, 재시작 후에도 유지)
- `POST /chat` — `{"message": "..."}` 단발 질의. 답변과 인용 목록을 돌려줍니다
- `GET /companies?query=`, `GET /filings?company=&sector=&doc_group=&doc_subtype=&date_from=&date_to=&is_correction=&keyword=&limit=` — 유니버스와 메타데이터 직접 조회

`/answer`와 대시보드는 같은 함수(`app/agent/answer.py`의 `answer_question`)를 씁니다.

## 코드 구성

```
run.py                     서버 진입점 (uvicorn)
app/
├── main.py                FastAPI 앱, /dashboard, /health
├── config.py              경로·모델·예산 설정 (환경변수)
├── api/
│   ├── routes.py          /answer 와 나머지 라우트
│   ├── schemas.py         요청/응답 모델
│   ├── events.py          실행 상태와 SSE 이벤트 (서버 보관)
│   └── history.py         질의 기록 저장
├── agent/
│   ├── answer.py          답변 파이프라인: 입력 정제, 사전 차단, 힌트 생성, 후처리, 근거 조립
│   ├── loop.py            HCX-007 function-calling 루프와 자기 교정 가드
│   ├── tools.py           도구 6종 스키마·실행·결정론 강화 계층
│   ├── system_prompt.py   에이전트 지침
│   ├── clova.py           CLOVA Studio 클라이언트 (재시도, 속도 조절)
│   ├── calc.py            금액 표기·계산 도구
│   └── usage.py           토큰 사용량 집계
├── data/
│   ├── manifest.py        manifest + universe → DuckDB, 검색·집계, 기업명 해석
│   ├── parser.py          DART XML/HTML → 텍스트, 목차 기반 섹션 분할
│   ├── cache.py           파싱 결과 캐시 (cache/text/)
│   └── fulltext.py        SQLite FTS5 전문검색
└── static/                대시보드 (HTML/CSS/JS)
scripts/
├── fetch_data.py          릴리즈에서 corpus/·cache/ 복원
├── build_index.py         전체 파싱 + FTS 색인
├── publish_data.sh        corpus/·cache/ 릴리즈 업로드
├── test_api.py            배포 서버 스모크 테스트
├── run_eval.py            평가 문항 1건 실행
├── run_eval_all.py        docs/EVAL_ALL.md 전 문항 실행
├── grade_eval_all.py      실행 결과 채점 보조
└── make_eval_report.py    docs/EVAL_REPORT.md 생성
tests/
├── test_agent.py          회귀 테스트 (대부분 실제 오답에서 역산)
└── test_parser.py         파서 테스트
```

## 테스트와 평가

```bash
uv run pytest                                   # 회귀 테스트 197개, 약 25초 (corpus/·cache/ 필요)

uv run python scripts/run_eval_all.py --redo    # 119문항 전부 /answer 로 실행, 약 45분
uv run python scripts/grade_eval_all.py         # 자동 채점 신호 표시
```

전수 평가 결과와 문항별 판정은 `docs/EVAL_REPORT.md`에 있습니다.

## 규정 관련

- 모델 호출은 `app/agent/clova.py` 한 곳에서만 나가며 HCX-007만 씁니다.
- 외부 데이터나 외부 공시 API는 쓰지 않습니다. 도구가 읽는 것은 `corpus/`와 `cache/`뿐입니다.
- 답변에는 근거 공시를 `(보고서명, 접수일, doc_id)`로 표시합니다. 확인할 수 없는 내용은 확인할 수 없다고 답하고, 미래 예측과 투자 의견은 내지 않습니다.
- 정기공시 3건은 DART가 XML을 제공하지 않아 PDF와 뷰어 HTML로 대체 수집돼 있습니다. 이 문서는 "원문 미제공"으로 표시해 내용을 지어내지 않게 합니다.
