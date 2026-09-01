# miraeasset_agent

`corpus/`에 수집된 70개 상장기업 DART 공시 원문(정기·주요사항·거래소·지분공시, 4,204건 / XML 4,616개)을
자연어로 질의응답할 수 있는 리서치 어시스턴트 API 서버입니다.

상세 데이터 설명은 [`corpus/README.md`](corpus/README.md), 수집 기준은 [`corpus/data_filter.md`](corpus/data_filter.md) 참고.

## 아키텍처

메타데이터(`manifest.jsonl`)로 구조적 필터링 → 필요한 문서만 온디맨드 파싱 → 키워드 전문검색(FTS5)으로 보조
하는 에이전틱 tool-use 구조입니다 (벡터 임베딩 없이 시작, 필요시 후속 추가).

```
app/
├── main.py            # FastAPI 앱
├── config.py           # 경로/모델 설정
├── data/
│   ├── manifest.py      # manifest.jsonl + universe.csv → DuckDB, search_filings()
│   ├── parser.py         # DART XML(ATOC 목차 기반 섹션 추출) / 거래소 HTML → 텍스트
│   ├── cache.py           # 파싱 결과 텍스트 캐시 (cache/text/)
│   └── fulltext.py         # SQLite FTS5(trigram) 전문검색
├── agent/
│   ├── tools.py            # Claude tool 스키마 + dispatcher
│   ├── system_prompt.py     # 에이전트 지침
│   └── loop.py                # Anthropic tool-use 루프
└── api/
    ├── schemas.py            # Pydantic 요청/응답 모델
    └── routes.py               # POST /chat, GET /companies, GET /filings
scripts/build_index.py    # 전체 코퍼스 사전 파싱 + FTS 인덱스 빌드
tests/test_parser.py      # corpus/ 실제 샘플로 파서 검증
```

## 실행법

```bash
uv sync
cp .env.example .env   # ANTHROPIC_API_KEY 채워넣기

# 캐시 세팅 (아래 "캐시 세팅" 섹션 참고 — 릴리즈에서 받는 쪽이 빠름)
uv run python scripts/fetch_cache.py

uv run uvicorn app.main:app --reload
```

## 캐시 세팅

`cache/`(파싱된 평문 + 섹션 오프셋 + FTS5 전문검색 색인, 3.2GB / 9,239개 파일)는 git에 포함되지 않습니다.
두 가지 방법으로 준비할 수 있고, **방법 1이 빠릅니다** (재파싱 없이 완성본 다운로드).

**방법 1 — GitHub 릴리즈에서 다운로드 (권장)**

```bash
uv run python scripts/fetch_cache.py           # cache/ 없을 때만 받음
uv run python scripts/fetch_cache.py --force   # 기존 cache/ 를 지우고 다시
```

[`cache-v1` 릴리즈](https://github.com/yojun313/miraeasset_agent/releases/tag/cache-v1)에서
zstd 아카이브(1.1GB)를 받아 sha256 검증 후 `cache/`에 풀어줍니다. 시스템에 `zstd`가 필요합니다.

레포가 비공개이므로 다운로드에도 인증이 필요합니다 — 둘 중 하나면 됩니다:
- `gh auth login` 된 gh CLI가 PATH에 있음 (스크립트가 자동 사용)
- `GITHUB_TOKEN` 환경변수 (repo 읽기 권한 토큰) — gh 없이 GitHub API로 직접 받음

**방법 2 — corpus/ 에서 직접 빌드**

```bash
# 전체 코퍼스 사전 인덱싱 - 4,200여 건 파싱, 수 분 소요
uv run python scripts/build_index.py
# 빠른 확인용 부분 인덱싱: uv run python scripts/build_index.py --company 삼성전자
```

캐시를 다시 만든 뒤 새 버전으로 재배포하려면:

```bash
bash scripts/publish_cache.sh cache-v2   # gh CLI 인증 필요
# 이후 scripts/fetch_cache.py 상단의 TAG 를 cache-v2 로 갱신
```

인덱싱 없이 서버를 띄워도 동작합니다 — `search_filings`/`list_sections`/`read_section`은 항상 온디맨드로
파싱하며 자동 캐시됩니다. 다만 `search_fulltext`(본문 키워드 검색)는 `build_index.py`로 인덱싱된 문서만 검색됩니다.

## API

- `POST /chat` — `{"message": "...", "session_id": "(선택, 대화 이어가기)"}` →
  `{"session_id", "answer", "citations": [{"doc_id","report_nm","corp_name","rcept_dt"}], "tool_calls"}`
- `GET /companies?query=현대차` — 유니버스 조회 (query 생략 시 70개사 전체)
- `GET /filings?company=&sector=&doc_group=&doc_subtype=&date_from=&date_to=&is_correction=&keyword=&limit=` — 메타데이터 직접 조회
- `GET /health`

```bash
curl -s -X POST localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "삼성전자 최근 대규모 공급계약 알려줘"}' | python3 -m json.tool
```

## 테스트

```bash
uv run pytest
```

## v1 범위 밖 (의도적 제외)

- 벡터 임베딩/시맨틱 검색 — 메타데이터 필터 + FTS5로 우선 검증
- 인증/레이트리밋 — 실제 외부 노출 전 추가 필요
- 스트리밍 응답, 세션 영속화(현재 인메모리, 재시작 시 소실)
- PDF 원문 파싱 — 대체 수집된 3건(`file_format=pdf+html`)은 `_viewer.html`만 읽음
