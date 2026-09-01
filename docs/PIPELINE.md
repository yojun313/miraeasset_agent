# 답변 생성 파이프라인 — 코드 경로 추적

> 질문 하나가 들어와서 답변이 나오기까지, **어떤 파일의 어떤 함수가 어떤 순서로**
> 실행되는지를 실제 데이터와 함께 기록한 문서.
> 예시 질문: `"삼성중공업이 2023년에 체결한 원유운반선 공급계약 금액은?"`
> (관련 설계 배경은 [`DESIGN.md`](DESIGN.md) 참고)

---

## 0. 한눈에 보기

```
HTTP 요청
  └─ app/api/routes.py :58   answer()              ← 평가용 진입점
       └─ app/agent/answer.py :134  answer_question()
            ├─ sanitize_question()                 ← 입력 정제
            └─ app/agent/loop.py :211  run_agent()  ← 에이전트 루프 (최대 12턴)
                 │
                 │  ┌──── 매 턴 반복 ────────────────────────────────┐
                 ├─►│ ① _compact_history()   이력 압축 (토큰 절약)    │
                 │  │ ② clova.chat_completion()  HCX-007 호출        │
                 │  │ ③ _parse_tool_call()   도구 호출 파싱           │
                 │  │ ④ _run_tool() → tools.dispatch()  도구 실행     │
                 │  │ ⑤ 결과를 이력에 추가 → 다음 턴                   │
                 │  └────────────────────────────────────────────────┘
                 │
                 └─ 도구 호출 없이 텍스트만 오면 → 최종 답변
       └─ build_retrieved_context()               ← 근거 조립
  └─ AnswerResponse 반환 (항상 HTTP 200)
```

**핵심 원칙**: 어떤 단계가 실패해도 예외가 밖으로 나가지 않는다. 평가자는
언제나 유효한 JSON을 받는다.

---

## 1. 진입 — `GET /answer`

**파일**: `app/api/routes.py:58` `answer()`

```
GET /answer?question_id=Q-001&question=삼성중공업이+2023년에+...
```

| 순서 | 코드 | 하는 일 |
|---|---|---|
| 1-1 | `routes.py:63` | `(question_id, question)` 키로 **캐시 조회**. 평가자가 타임아웃 후 재호출할 수 있으므로 성공 응답은 재사용 |
| 1-2 | `routes.py:72` | `answer_question()` 호출 |
| 1-3 | `routes.py:81` | 어떤 예외든 `except Exception`으로 잡아 폴백 응답 생성 (**500을 절대 내지 않는다**) |
| 1-4 | `routes.py:92` `_log_answer()` | `logs/answers.jsonl`에 감사 로그. 로그 실패가 응답을 막지 않음 |
| 1-5 | `routes.py:106` | **성공한 응답만** 캐시에 저장 (실패는 재시도 기회를 줘야 하므로 저장 안 함) |

> 대시보드는 같은 로직을 SSE로 감싼 `routes.py:123` `answer_stream()`을 쓴다.
> 별도 스레드에서 `answer_question(on_event=...)`을 돌리고 `queue.Queue`로 받은
> 이벤트를 `_sse()`로 흘려보낸다. 평가용 `/answer`와 동작은 동일하다.

---

## 2. 파이프라인 — `answer_question()`

**파일**: `app/agent/answer.py:134`

| 순서 | 코드 | 하는 일 |
|---|---|---|
| 2-1 | `answer.py:51` `sanitize_question()` | 제어문자 제거, 4,000자 상한. 빈 질문이면 안내 문구 반환하고 종료 |
| 2-2 | `answer.py:148` | `deadline = now + ANSWER_TIME_BUDGET(240초)` 설정 — 이후 모든 LLM 호출이 이 예산을 공유 |
| 2-3 | `answer.py:150` | `run_agent()` 호출 |
| 2-4 | `answer.py:151` | `ClovaError`(키 미설정 포함) → 폴백 답변 |
| 2-5 | `answer.py:72` `build_retrieved_context()` | 도구 호출 로그에서 **근거 조립** |

`build_retrieved_context()`는 tool_calls 로그를 훑어 중요도순으로 묶는다:

```
## 열람한 공시 본문       ← read_section 결과 (실제 근거)
## 키워드 검색 결과 발췌   ← search_fulltext snippet
## 검색된 공시 목록       ← search_filings 메타데이터
```
중복 제거 후 30,000자 상한. **이력 압축(4-1)과 무관하게 원본 로그에서 만들므로
근거 품질이 손상되지 않는다.**

---

## 3. 에이전트 루프 — `run_agent()`

**파일**: `app/agent/loop.py:211`

최대 `MAX_AGENT_TURNS`(12)턴 반복. 각 턴은 아래 순서.

### 3-1. 이력 압축 (`loop.py:136` `_compact_history`)

이력은 매 턴 통째로 재전송되므로 그대로 두면 분당 토큰 한도(60,000)를 넘긴다.

| 기법 | 코드 | 내용 |
|---|---|---|
| 중복 제거 | `loop.py:145` | 같은 내용의 도구 결과가 또 나오면 "앞선 N번째와 동일" 참조로 대체 |
| 가운데 생략 | `loop.py:124` `_elide_middle()` | 오래된 결과(최근 2턴 밖)는 앞 60% + 뒤 40%만 남김. 공시는 **합계가 표 끝**에 오므로 앞만 남기면 안 됨 |

> 실측: 정기보고서 5개 섹션 × 4턴 기준 136,790자 → 63,488자 (**53.6% 절감**)
> 원본 `history`는 변경하지 않는다 (세션 저장·추적용으로 온전해야 함).

### 3-2. LLM 호출 (`app/agent/clova.py:219` `chat_completion`)

```
POST https://clovastudio.stream.ntruss.com/v1/openai/chat/completions
{
  "model": "HCX-007",
  "messages": [system, user, assistant(tool_calls), tool, ...],
  "tools": [...],                  ← tools.py:185 openai_tool_specs()
  "reasoning_effort": "none",      ← 필수! (아래 주의사항 참고)
  "max_completion_tokens": 4096,
  "temperature": 0.2
}
```

| 순서 | 코드 | 하는 일 |
|---|---|---|
| a | `clova.py:92` `_RateLimitGovernor.before_request()` | 잔여 토큰이 12,000 미만이면 **창 리셋까지 선제 대기**. 연속 호출 최소 간격 1.1초 |
| b | `clova.py` POST | 세마포어로 동시 호출 1개 제한 |
| c | `clova.py` `observe()` | 응답 헤더 `x-ratelimit-remaining-tokens` 읽어 다음 호출 차단 여부 판단 |
| d | `clova.py:201` `_meta_from()` | `usage`(토큰) + 잔여 한도를 `on_meta` 콜백으로 전달 → 대시보드 실시간 표시 |
| e | `clova.py` 429 처리 | `penalize()`가 `x-ratelimit-reset-*`이 알려주는 실제 시간(예: 49초)만큼 대기 |

> **주의 — 실전에서 겪은 함정**: HCX-007은 reasoning(thinking)이 기본 활성인데
> tool 사용과 동시에 쓸 수 없다. `tools`를 보낼 때뿐 아니라 **대화 이력에 tool
> 메시지가 남아 있기만 해도** `400 Invalid parameter: tools, reasoning`이 난다.
> 그래서 루프의 **모든** 호출에서 `reasoning_effort: "none"`을 보낸다.

### 3-3. 응답 해석 (`loop.py:240~`)

```python
raw_tool_calls = msg.get("tool_calls") or msg.get("toolCalls")  # snake/camel 양쪽 허용
```
- 도구 호출이 **없고** 텍스트가 있으면 → **최종 답변, 루프 종료**
- 도구 호출이 **없고** 텍스트도 없으면 → 답변 재요청 후 다음 턴
- 도구 호출이 **있으면** → 3-4로

### 3-4. 도구 실행 (`loop.py:172` `_parse_tool_call` → `loop.py:62` `_run_tool`)

`_parse_tool_call`은 절대 예외를 던지지 않는다:
- `arguments`가 문자열/dict 양쪽 수용
- JSON 파싱 실패 → 오류 메시지를 **결과로 되먹여** 모델이 스스로 고치게 함

`_run_tool` → `app/agent/tools.py:238` `dispatch()` → 아래 4장.

### 3-5. 이력 추가 (`loop.py:250~`)

```python
per_call_cap = max(1_200, _TURN_RESULT_BUDGET(24,000) // len(pending_results))
```
한 턴에 도구를 26번 호출하는 일이 실제로 있어서, 턴 전체 결과 총량을 예산으로
나눠 배분한다. 그 뒤 `assistant(tool_calls)` → `tool` 메시지 순서로 추가.

### 3-6. 강제 마무리 (`loop.py:353` `_finish_without_tools`)

턴/시간 예산 소진, 또는 LLM 호출 실패 시. 도구 없이 1회 더 호출해
"지금까지 수집한 정보로 답하라"고 요청한다. 그마저 실패하면 정적 안내 문구.
**수집한 근거를 버리고 빈손으로 폴백하지 않는다.**

---

## 4. 도구 실행 — `dispatch()`

**파일**: `app/agent/tools.py:238`

도구 5개가 **서로 다른 저장소**를 쓴다. 이게 이 시스템의 핵심 구조다.

| 도구 | 실제 구현 | 저장소 |
|---|---|---|
| `get_company_info` | `manifest.py:48` `resolve_company()` | DuckDB (메모리) |
| `search_filings` | `manifest.py:79` `search_filings()` | DuckDB (메모리) |
| `search_fulltext` | `fulltext.py:76` `search_fulltext()` | **SQLite FTS5** |
| `list_sections` | `cache.py:96` `get_parsed()` | 파일 (`cache/text/`) |
| `read_section` | `cache.py:112` `read_section()` | 파일 (`cache/text/`) |

### 4-1. `get_company_info` — 통용명 → 정식 법인명

**`app/data/manifest.py:48` `resolve_company()`**

```
입력: "삼성중공업"
  ① 정확 일치: corp_name = ? OR listed_name = ? OR stock_code = ?
  ② (0건이면) 부분 일치: corp_name LIKE '%...%'
출력: corp_code=00126362, corp_name=삼성중공업, sector=조선
```

`현대차→현대자동차`, `KT→케이티` 같은 표기 차이를 여기서 흡수한다.
이후 도구에는 **이름이 아니라 `corp_code`로 변환해서** 넘긴다.

### 4-2. `search_filings` — 구조적 필터 (DuckDB)

**`app/data/manifest.py:79`**

`manifest.jsonl`(4,204행)이 서버 기동 시 `load_corpus()`(`manifest.py:38`)로
DuckDB에 등록돼 있다. 들어온 인자만 조건으로 누적한다:

```sql
SELECT doc_id, corp_name, ..., base_year, base_month, ...
FROM manifest
WHERE corp_code IN (?) AND doc_group = ? AND doc_subtype = ?
  AND rcept_dt >= ? AND rcept_dt <= ?
ORDER BY rcept_dt DESC
LIMIT ?
```

값은 전부 `?` 파라미터 바인딩 — **LLM이 만든 인자가 SQL에 직접 들어가지 않는다.**

> **회계연도 vs 접수일 (중요)**
> 정기공시는 회계기간이 끝난 뒤 제출된다. FY2025 사업보고서는 2026-03 접수.
> - `"2025년 실적"` → `base_year=2025` (→ `periodic_20260312001399`)
> - `"2025년에 공시된 것"` → `date_from/date_to`
>
> 접수일로 회계연도를 찾으면 **한 해 전 보고서**가 잡힌다. 실제 발생했던 오답이며
> `tests/test_agent.py`에 회귀 테스트가 있다.
> (분기·반기는 같은 해에 접수되므로 이 함정이 없다: FY2025 1Q → 2025-05-15 접수)

반환 결과가 0건이면 `tools.py`에서 "조건을 넓혀 재시도하라"는 힌트를 함께 준다.

### 4-3. `search_fulltext` — 키워드 검색 (SQLite FTS5)

**`app/data/fulltext.py:76` `search_fulltext()`**

```sql
SELECT doc_id, corp_name, report_nm, rcept_dt,
       snippet(filing_text, -1, '[', ']', ' ... ', 16) AS snippet
FROM filing_text
WHERE filing_text MATCH ?          -- content 컬럼 색인 검색
  AND corp_name IN (?)             -- UNINDEXED 컬럼으로 필터
ORDER BY rank                      -- BM25 유사 관련도
LIMIT ?
```

**`cache/fulltext.sqlite3` 내부 구조** (FTS5가 자동 생성하는 섀도 테이블):

| 테이블 | 행 수 | 크기 | 역할 |
|---|---:|---:|---|
| `filing_text` | 4,619 | — | 가상 테이블 (창구 역할, 실체 없음) |
| `filing_text_content` | 4,619 | 1,029 MB | 원문 사본 (`c0`~`c8` = 선언 순서대로 매핑) |
| `filing_text_data` | 322,928 | 1,284 MB | 역색인 본체 (trigram → 문서·위치) |
| `filing_text_idx` | 147,365 | 3 MB | data 블록 위치 색인 |
| `filing_text_docsize` | 4,619 | ~0 | 문서 길이 (rank 계산용) |
| `filing_text_config` | 1 | ~0 | `version=4` |

**`content`만 색인되고 나머지는 `UNINDEXED`인 이유**:

```
MATCH 'content:삼성중공업'     → 339건   (본문에 실제 언급된 문서)
MATCH 'corp_name:삼성중공업'   →   0건   (색인 대상 아님)
WHERE corp_name = '삼성중공업' → 106건   (등호 비교는 가능 → 필터로 사용)
```
회사명까지 색인하면 `"삼성전자"` 검색 시 본문에 없어도 전 문서가 걸린다.

**trigram을 쓰는 이유**: 한국어는 교착어라 띄어쓰기 기준 토큰이 안 맞는다.

```
본문 "위탁생산계약을" / 질의 "위탁생산"
  일반 토크나이저 → 토큰 불일치 → 못 찾음
  trigram        → 위탁생·탁생산·생산계·산계약 로 색인 → 찾음
```
질의는 최소 3글자여야 한다.

### 4-4. `list_sections` / `read_section` — 본문 읽기 (파일)

**`app/data/cache.py:96` `get_parsed()`**

```
① cache/text/{doc_id}/{stem}.txt 가 있으면 재사용
② 없으면 corpus/raw/... 원문을 parser.py로 파싱 후 캐시 저장
```

**`app/data/parser.py:148` `parse_file()`**
- DART XML → `_parse_dart_xml()` (`parser.py:113`): `<TITLE ATOC="Y">`를 섹션
  경계로 잡고 표를 `키 | 값` 라인으로 변환
- 거래소 HTML → `_parse_html_filing()` (`parser.py:124`): BeautifulSoup으로 표 추출

산출물 2종:
```
20240312000736.txt              ← 정제 평문
20240312000736.sections.json    ← [{atocid, title, offset}, ...]
```

`.sections.json`의 `offset`이 **부분 읽기의 핵심**이다. 삼성화재 사업보고서는
본문 1,124,669자 / 섹션 152개인데, "2-2. 연결 포괄손익계산서"의 offset부터 다음
섹션 offset까지 슬라이스하면 4,363자만 꺼낼 수 있다.

**`tools.py:215` `_page()`** — 12,000자를 넘으면 페이징:
```json
{"text": "...", "truncated": true, "total_chars": 45000,
 "next_offset": 12000, "note": "offset=12000 으로 다시 호출하세요"}
```
무작정 자르지 않고 모델이 필요할 때 이어 읽게 한다.

**`cache.py:43` `_resolve_file()`** — `filename`은 LLM이 채우므로 관대하게 해석:
전체 경로·폴더 경로·확장자 생략 모두 수용, 실패 시 사용 가능한 파일명을 알려줌.

---

## 5. 저장소 3종 — 왜 나눠져 있나

같은 텍스트가 세 곳에 있다. 역할이 다르다.

```
corpus/raw/.../20230602800079.xml          ← 원본 (진실의 원천, 5.3GB)
        │ parse_file()
        ├────────────────────────────┐
        ▼                            ▼
cache/text/.../20230602800079.txt   fulltext.sqlite3 의 content
   평문 + .sections.json (1.0GB)       색인용 사본 (2.3GB)
        │                            │
        ▼                            ▼
   read_section 이 읽음           search_fulltext 가 MATCH/snippet
   → 답변의 실제 근거              → "어느 문서를 볼지"만 알려줌
```

| | `cache/text/` 파일 | SQLite `content` |
|---|---|---|
| 목적 | **정독** (답변 근거) | **탐색** (문서 찾기) |
| 부가 데이터 | `.sections.json` 목차 오프셋 | 역색인 (trigram) |
| 부분 읽기 | 가능 (섹션 슬라이스, offset 페이징) | 불가 (통짜 저장) |
| 없으면 | 원문에서 자동 재생성 | 검색만 안 됨 |

**검증**: `cache/text/{doc_id}/`를 지우고 `read_section`을 호출하면 SQLite를
쳐다보지 않고 `corpus/raw` 원문을 다시 파싱해 캐시를 재생성한다.
코드상으로도 `cache.py`는 sqlite를 import하지 않고, `fulltext.py`는
`cache/text/`를 건드리지 않는다.

즉 **답변에 인용되는 문장은 전부 파일에서 나온다.**

---

## 6. 실제 추적 예시

질문: `"삼성중공업이 2023년에 체결한 원유운반선 공급계약 금액은?"`

```
[턴 1] get_company_info({"query": "삼성중공업"})
       manifest.py:48 → DuckDB universe
       → corp_name=삼성중공업, stock_code=010140, sector=조선

[턴 2] search_filings({"company":"삼성중공업", "doc_group":"exchange",
                       "doc_subtype":"단일판매공급계약체결",
                       "date_from":"20230101", "date_to":"20231231"})
       manifest.py:79 → DuckDB manifest
       → 13건: exchange_20231226800129, ..., exchange_20230602800079

[턴 3] read_section({"doc_id":"exchange_20230602800079", ...})
       cache.py:112 → cache/text/exchange_20230602800079/20230602800079.txt
       → "1. 판매ㆍ공급계약 구분 | 공사수주
          - 체결계약명 | 원유운반선 2척
          2. 계약내역 | 계약금액(원) | 227,500,000,000
          3. 계약상대 | 오세아니아 지역 선주"  (525자)

[턴 4] 도구 호출 없이 텍스트만 반환 → 최종 답변
       "227,500,000,000원 (단일판매·공급계약체결, 2023-06-02,
        exchange_20230602800079)"
```

### 어느 경로로 가는가

지금까지 기록된 15건의 실제 질의에서 **`search_fulltext`는 한 번도 호출되지
않았다.** 전부 `search_filings → read_section` 경로였다.

| 질문 유형 | 경로 |
|---|---|
| "삼성화재 2025년 실적" | 회사+연도+유형으로 특정 → **FTS 불필요** |
| "삼성중공업 2023년 공급계약" | 메타데이터로 좁혀짐 → **FTS 불필요** |
| "원유운반선 수주한 회사가 어디야?" | 어느 문서인지 모름 → **FTS 필요** |
| "리튬 공급 계약을 맺은 곳은?" | 본문에만 있는 키워드 → **FTS 필요** |

의도된 동작이다. 공시는 "회사 × 연도 × 유형"으로 이미 구조화돼 있어서
구조 필터(L1)가 대부분을 처리하고 FTS(L2)는 백스톱 역할이다. 처음부터 벡터/
키워드로 뒤지는 일반적인 RAG보다 이 순서가 정확하다.

---

## 7. 실패해도 답이 나오는 이유

| 실패 지점 | 코드 | 대응 |
|---|---|---|
| 도구 인자 JSON 깨짐 | `loop.py:172` | 오류를 결과로 되먹여 모델이 자가 수정 |
| 도구 자체가 예외 | `loop.py:62` | `{"error": ...}`로 감싸 루프 유지 |
| 검색 0건 | `tools.py` | 조건을 넓히라는 힌트 반환 |
| 파일명 오지정 | `cache.py:43` | 경로/확장자 관대 해석, 사용 가능 목록 안내 |
| 빈 LLM 응답 | `loop.py` | 1회 재촉 후 다음 턴 |
| 429 rate limit | `clova.py:92` | 헤더가 알려주는 실제 시간만큼 대기 |
| 5xx / 네트워크 | `clova.py` | 지수 백오프 재시도 (최대 4회) |
| 턴/시간 예산 소진 | `loop.py:353` | 수집된 정보로 강제 마무리 |
| LLM 완전 불가 | `answer.py:151` | 폴백 답변 |
| 그 외 모든 예외 | `routes.py:81` | 폴백 + **HTTP 200 유지** |

평가 GET이 실패하면 그 문항은 0점이므로, **어떤 경우에도 200 + 유효 JSON**을
반환하는 것이 최우선 원칙이다.

---

## 8. 관련 파일 요약

| 파일 | 역할 |
|---|---|
| `app/api/routes.py` | HTTP 진입점 (`/answer`, `/answer/stream`, `/chat`, `/filings`, `/companies`) |
| `app/agent/answer.py` | 질문 1건 처리 파이프라인, 근거 조립 |
| `app/agent/loop.py` | function-calling 루프, 이력 압축, 이벤트 방출 |
| `app/agent/clova.py` | CLOVA Studio HTTP 클라이언트, rate limit 관리 |
| `app/agent/tools.py` | 도구 스키마 + dispatcher |
| `app/agent/system_prompt.py` | 에이전트 지침 (가드레일 포함) |
| `app/data/manifest.py` | DuckDB 메타데이터 검색 (L1) |
| `app/data/fulltext.py` | SQLite FTS5 키워드 검색 (L2) |
| `app/data/cache.py` | 파싱 결과 캐시, 파일 해석 |
| `app/data/parser.py` | DART XML / 거래소 HTML → 평문 |
| `app/config.py` | 경로·모델·예산 설정 |
| `scripts/build_index.py` | 오프라인 전처리 (캐시 + FTS 인덱스) |
