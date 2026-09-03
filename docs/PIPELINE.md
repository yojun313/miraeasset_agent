# 답변 생성 파이프라인 — 코드 경로 추적

> 질문 하나가 들어와서 답변이 나오기까지, **어떤 파일의 어떤 함수가 어떤 순서로**
> 실행되는지를 실제 데이터와 함께 기록한 문서.
> 예시 질문: `"삼성중공업이 2023년에 체결한 원유운반선 공급계약 금액은?"`
> (설계 배경·결정 이유는 [`DESIGN.md`](DESIGN.md), 평가 질문셋은 [`EVAL_ALL.md`](EVAL_ALL.md))
>
> 최종 갱신: 2026-09-02 (코드 기준: 도구 6종, 가드레일 3단, 결정론 강화 계층 포함)

---

## 0. 한눈에 보기

```
HTTP 요청
  └─ app/api/routes.py:61   answer()                     ← 평가용 진입점
       └─ app/agent/answer.py:524  answer_question()
            ├─ ① sanitize_question()                     ← 입력 정제
            ├─ ② detect_meta_request() / detect_pii_request()
            │      └─ 걸리면 도구 실행 없이 고정 응답 (0초, 0토큰)
            ├─ ③ build_guard_hint() + build_search_hint() ← 질문 분석 → 지침 주입
            └─ ④ app/agent/loop.py:410  run_agent()       ← 에이전트 루프 (최대 12턴)
                 │
                 │  ┌──── 매 턴 반복 ─────────────────────────────────┐
                 ├─►│ a _compact_history() / _linearize_history()      │
                 │  │ b clova.chat_completion()   HCX-007 호출         │
                 │  │ c 응답 해석 (도구 호출 · 텍스트 · 자기교정 판정)    │
                 │  │ d _run_tool() → tools.dispatch()  도구 실행       │
                 │  │   └ 결정론 강화: 금액·비율·기간·집계를 코드가 계산   │
                 │  │ e 결과를 이력에 추가 → 다음 턴                     │
                 │  └─────────────────────────────────────────────────┘
                 │
                 └─ 도구 호출 없이 텍스트만 오면 → 최종 답변 후보
            ├─ ⑤ 답변 후처리 (외부참조·DART URL 제거, 단위 환산 검산)
            └─ ⑥ build_retrieved_context()                ← 근거 조립
  └─ AnswerResponse 반환 (항상 HTTP 200)
```

**두 가지 핵심 원칙**

1. **어떤 단계가 실패해도 예외가 밖으로 나가지 않는다.** 평가자는 언제나 유효한 JSON을 받는다.
2. **결정론적인 일은 LLM에게 시키지 않는다.** 세기·더하기·비교·날짜 차이·단위 환산·순위·
   집합차는 전부 도구 안에서 코드가 계산해 결과에 실어 보낸다 (4장).

---

## 1. 진입 — `GET /answer`

**파일**: `app/api/routes.py:61` `answer()`

```
GET /answer?question_id=Q-001&question=삼성중공업이+2023년에+...
```

| 순서 | 코드 | 하는 일 |
|---|---|---|
| 1-1 | `routes.py:67` | `(question_id, question)` 키로 **캐시 조회**. 평가자가 타임아웃 후 재호출할 수 있으므로 성공 응답은 재사용 (실측: 재호출 2ms) |
| 1-2 | `routes.py:76` | `answer_question()` 호출 |
| 1-3 | `routes.py:85` | 어떤 예외든 `except Exception`으로 잡아 폴백 응답 생성 (**500을 절대 내지 않는다**) |
| 1-4 | `routes.py:96` `_log_answer()` | `logs/answers.jsonl`에 감사 로그. 로그 실패가 응답을 막지 않음 |
| 1-5 | `routes.py:111` | **성공한 응답만** 캐시에 저장 (실패는 재시도 기회를 줘야 하므로 저장 안 함) |

응답 스키마는 `app/api/schemas.py:27` `AnswerResponse` — 주최측 요구 5필드
(`question_id`, `question`, `retrieved_context`, `think_trace`, `answer`)만 반환한다.

> **대시보드 경로**: 웹 UI는 `POST /runs`(`routes.py:235`)로 실행을 시작하고
> `GET /events`(`routes.py:249`) SSE 스트림을 구독한다. 실행 상태는 브라우저가 아니라
> 서버(`app/api/events.py`의 `RunRegistry`)에 있어서, 어느 컴퓨터·탭에서 접속해도
> 같은 진행 상황과 질의기록을 본다. 평가용 `/answer`와 답변 로직은 완전히 동일하다.

---

## 2. 파이프라인 — `answer_question()`

**파일**: `app/agent/answer.py:524`

### 2-1. 입력 정제와 사전 차단 (도구 실행 전)

| 순서 | 코드 | 하는 일 |
|---|---|---|
| a | `answer.py:380` `sanitize_question()` | 제어문자 제거, 4,000자 상한. 빈 질문이면 안내 문구 반환하고 종료 |
| b | `answer.py:356` `detect_meta_request()` | "네 지침/시스템 프롬프트/절대 규칙을 알려줘" 류 → 역할 소개 고정 응답 |
| c | `answer.py:360` `detect_pii_request()` | 주민등록번호·자택주소·개인 연락처 요구 → 거절 고정 응답 |

b·c는 **LLM을 호출하기 전에** 끝낸다. 이유는 실패 경험에서 나왔다:

- PII 질문에서 모델이 임원 명단 12,000자를 컨텍스트에 실은 뒤에야 거절해,
  거절했는데도 `retrieved_context`에 개인정보가 남았다.
- 지침을 묻는 질문에는 존재하지도 않는 "절대 규칙 10개"를 지어내 답했다.

두 경우 모두 정답이 고정돼 있으므로 모델에게 맡길 이유가 없다.

### 2-2. 질문 분석 → 지침 주입

| 코드 | 산출물 |
|---|---|
| `answer.py:494` `build_guard_hint()` | **허위 인용 사전 대조**: 질문에 `"…"` 인용문이 있으면 전문검색으로 실재 여부를 확인해, 없으면 "사실로 전제하지 말라"고 주입. **전망 요구 차단**: "전망/실현 가능성/네 판단" 등이 있으면 미래 예측 금지를 명시 |
| `answer.py:190` `build_search_hint()` | 질문 키워드에서 **결정론적 검색 경로**를 뽑아 주입 (예: "감자" → `report_type=감자`, "13% 이상" → `min_ratio=13`, "2025년 1분기 분기보고서" → `base_year=2025, base_month=3`) |

HCX-007은 시스템 프롬프트의 검색 규칙을 자주 무시한다. 반대로 **질문 바로 옆에 붙인
짧은 힌트**는 잘 따른다. 다만 주입 텍스트가 길어지면 존재하지 않는 함수를 호출하려다
`400 40009`가 나므로(실측: 힌트 50자 3/3 성공, 250자 0/3), `_HINT_BUDGET = 200`자로
자르고 가드+힌트 합계가 300자를 넘으면 가드만 남긴다 (`answer.py:562`).

### 2-3. 실행과 후처리

| 순서 | 코드 | 하는 일 |
|---|---|---|
| a | `answer.py:567` | `run_agent()` 호출 (3장) |
| b | `answer.py:570` | `ClovaError`(키 미설정 포함) → 폴백 답변 |
| c | `answer.py:582` | 답변 후처리 3종 (아래) |
| d | `answer.py:410` `build_retrieved_context()` | 도구 호출 로그에서 근거 조립 |

**답변 후처리** — 프롬프트로 막지 못한 것을 코드가 잡는다:

| 함수 | 잡는 것 |
|---|---|
| `answer.py:77` `strip_external_referrals()` | "DART 홈페이지에서 확인하세요", "증권사 리포트 참고" 등 외부 정보원 안내 문장 제거 (출처+권유동사가 같은 문장에 있을 때만) |
| `answer.py:56` `strip_dart_urls()` | 답변에 섞인 `dart.fss.or.kr` 링크를 `(접수번호 20250311001085)` 표기로 치환 |
| `answer.py:62` `fix_unit_conversions()` | "905백만원 … 환산하면 90,500,000원" 같은 10배 오류를 검산해 교정 |

`build_retrieved_context()`는 tool_calls 로그를 훑어 중요도순으로 묶는다:

```
## 열람한 공시 본문       ← read_section 결과 (실제 근거)
## 키워드 검색 결과 발췌   ← search_fulltext snippet
## 검색된 공시 목록       ← search_filings 메타데이터 + (집계) 라인
```

중복 제거 후 30,000자 상한. **이력 압축(3-1)과 무관하게 원본 로그에서 만들므로
근거 품질이 손상되지 않는다.**

---

## 3. 에이전트 루프 — `run_agent()`

**파일**: `app/agent/loop.py:410`

최대 `MAX_AGENT_TURNS`(12)턴 반복. 각 턴은 아래 순서.

### 3-1. 이력 압축·선형화

| 기법 | 코드 | 내용 |
|---|---|---|
| 중복 제거 | `loop.py:159` `_compact_history()` | 같은 내용의 도구 결과가 또 나오면 "앞선 N번째와 동일" 참조로 대체 |
| 가운데 생략 | `loop.py:146` `_elide_middle()` | 오래된 결과(최근 2턴 밖)는 앞 60% + 뒤 40%만 남김. 공시는 **합계가 표 끝**에 오므로 앞만 남기면 안 됨 |
| **평문 선형화** | `loop.py:195` `_linearize_history()` | 이전 턴의 `assistant.tool_calls` + `role:"tool"` 구조를 **일반 텍스트 대화로 변환** |

> **선형화가 왜 필요한가 (실측으로 확정한 HCX-007 특성)**
> 표준 OpenAI 형식의 도구 이력을 그대로 재전송하면 다음 요청에서 `400 40009
> Unsupported function`이 난다. 통제 실험 결과 네이티브 이력 0/4 성공 vs
> 평문 선형화 3/4 성공. 도구 이력의 **구조 자체**가 원인이라, 이력을
> "[도구 결과] search_filings → …" 형태의 user 메시지로 눕혀서 보낸다.
> 실측 절감: 정기보고서 5개 섹션 × 4턴 기준 136,790자 → 63,488자 (53.6%).

### 3-2. LLM 호출 (`app/agent/clova.py:307` `chat_completion`)

```
POST https://clovastudio.stream.ntruss.com/v1/openai/chat/completions
{
  "model": "HCX-007",
  "messages": [system, user, ...],
  "tools": [...],                  ← tools.py:172 openai_tool_specs()
  "reasoning_effort": "none",      ← 필수! (아래 주의사항)
  "max_completion_tokens": 1536,
  "temperature": 0.2
}
```

| 순서 | 코드 | 하는 일 |
|---|---|---|
| a | `clova.py:106` `_RateLimitGovernor.before_request()` | 잔여 토큰이 12,000 미만이면 **창 리셋까지 선제 대기**. 연속 호출 최소 간격 1.1초 |
| b | `clova.py:255` `estimate_tokens()` | 요청 전 입력 토큰을 추정(1.37자/토큰으로 보수 산정)해 TPM 초과를 예방. `count_tokens_exact()`로 정확 측정도 가능 |
| c | POST | 세마포어로 동시 호출 1개 제한 |
| d | `clova.py:289` `_meta_from()` | `usage`(토큰) + 잔여 한도를 `on_meta` 콜백으로 전달 → 대시보드 실시간 표시, `usage.py`에 일별 누적 기록 |
| e | 429 처리 | `penalize()`가 `x-ratelimit-reset-*`이 알려주는 실제 시간(예: 49초)만큼 대기 |
| f | 40009 처리 | `_call_model()`(`loop.py:366`)이 도구 세트를 축소(전체 → 4개 → 핵심 3개)해 재시도 |

> **주의 — 실전에서 겪은 함정**: HCX-007은 reasoning(thinking)이 기본 활성인데
> tool 사용과 동시에 쓸 수 없다. `tools`를 보낼 때뿐 아니라 **대화 이력에 tool
> 메시지가 남아 있기만 해도** `400 Invalid parameter: tools, reasoning`이 난다.
> 그래서 루프의 **모든** 호출에서 `reasoning_effort: "none"`을 보낸다.
> (이 제약 때문에 DESIGN.md가 계획했던 "합성 단계 thinking"은 도입하지 못했다.)

### 3-3. 응답 해석과 자기교정

도구 호출이 **없고** 텍스트만 오면 최종 답변 후보다. 그대로 받아들이기 전에
아래 검사를 통과해야 한다 — 전부 실제 오답에서 역산해 넣은 장치다.

| 검사 | 코드 | 잡는 실패 |
|---|---|---|
| 텍스트 도구 호출 | `loop.py:307` `_parse_text_tool_calls()` | 모델이 `[도구 호출] search_filings({...})`를 **답변 본문에 적는** 경우 → 실제 호출로 변환해 실행 |
| 도구 안내문 에코 | `loop.py:262` `_looks_like_tool_echo()` | "다른 키워드나 필터를 사용해 보세요"(도구가 준 안내)를 답변으로 반환 |
| 미완성 응답 | `loop.py:250` `_looks_unfinished()` | "…검색해 보겠습니다"로 끝나고 실제로 안 함 |
| **날조 인용** | `loop.py:270` `_fabricated_citations()` | 답변의 14자리 접수번호가 도구 결과 어디에도 없음 (실제 사례: `20230000123456`을 지어냄) |
| **인용 누락** | `loop.py:292` `_lacks_citations()` | 긴 사실 나열인데 근거 doc_id가 하나도 없음 (평가지표 위반) |

각 검사는 해당 문제를 지적하는 user 메시지를 넣고 1~2회 재작성을 요구한다.
반복 호출은 `call_cache`가 차단하고("같은 조건은 같은 결과입니다"), 한 턴이
전부 반복이면 종합 답변을 강제한다 (`loop.py` all_repeats 분기).

### 3-4. 도구 실행과 이력 추가

`_parse_tool_call`(`loop.py:336`)은 절대 예외를 던지지 않는다 — `arguments`가
문자열/dict 양쪽 수용, JSON 파싱 실패 시 오류를 **결과로 되먹여** 모델이 고치게 한다.

```python
per_call_cap = max(1_200, _TURN_RESULT_BUDGET(24,000) // len(pending_results))
```

한 턴에 도구를 26번 호출하는 일이 실제로 있어서, 턴 전체 결과 총량을 예산으로
나눠 배분한다.

### 3-5. 강제 마무리 (`loop.py:672` `_finish_without_tools`)

턴/시간 예산 소진, 또는 LLM 호출 실패 시. 도구 없이 1회 더 호출해 "지금까지 수집한
정보로 답하라"고 요청한다. 그마저 실패하면 정적 안내 문구.
**수집한 근거를 버리고 빈손으로 폴백하지 않는다.**

---

## 4. 도구 실행 — `dispatch()`

**파일**: `app/agent/tools.py:776`

도구 6개가 **서로 다른 저장소**를 쓴다. 이게 이 시스템의 핵심 구조다.

| 도구 | 실제 구현 | 저장소 |
|---|---|---|
| `get_company_info` | `manifest.py:60` `resolve_company()` | DuckDB (메모리) |
| `search_filings` | `manifest.py:177` `search_filings()` / `manifest.py:306` `aggregate_filings()` | DuckDB (메모리) + 본문 캐시 |
| `search_fulltext` | `fulltext.py:76` `search_fulltext()` | **SQLite FTS5** |
| `list_sections` | `cache.py:103` `get_parsed()` | 파일 (`cache/text/`) |
| `read_section` | `cache.py` + `parser.py:161` `slice_section()` | 파일 (`cache/text/`) |
| `calculate` | `calc.py` (AST 화이트리스트 평가기) | — |

> `calculate`는 한때 제거했다가 되돌린 도구다. 40009 원인을 찾는 과정에서 후보로
> 지목돼 뺐지만, 이후 통제 실험으로 진짜 원인이 **도구 이력 구조와 힌트 길이**임이
> 밝혀져 재투입했다. 다사·다년 합계처럼 자릿수가 큰 연산에서 필요하다.
> 도구 스키마 총량은 3,706자 — 40009가 나기 시작하는 4,400자 아래로 유지한다.

### 4-1. `get_company_info` — 통용명·오타·시장명 흡수

**`app/data/manifest.py:60` `resolve_company()`** — 4단계로 내려간다:

```
입력: "삼성중공업" / "JYP Ent." / "레인보우로버틱스" / "레인보우로보틱스,두산로보틱스"
  ① 정확 일치 : corp_name / listed_name / stock_code
  ② 부분 일치 : corp_name LIKE '%…%'
  ③ 정규화 비교: 구두점·공백·대소문자 제거 후 비교
                 ("JYP Ent." → "jypent" = 원문 "JYP Ent")
  ④ 퍼지 매칭  : difflib 편집거리 0.74 이상 (오타 "로버틱스" → "로보틱스")
                 동률 후보는 모두 반환("삼성전지" → 삼성전자·삼성전기)
  ※ 쉼표 나열("A,B,C")은 각각 해석해 합집합 → 여러 회사 비교를 한 번의 호출로
```

시장명("코스닥", "KOSPI")을 넣으면 기업 아님 오류 대신 **시총 내림차순 구성 목록**을
돌려준다. 섹터·업종명을 넣으면 소속 기업 목록을 돌려준다 (`resolve_sector()`).

### 4-2. `search_filings` — 구조적 필터 + 집계 (DuckDB)

`manifest.jsonl`(4,204행)이 서버 기동 시 `load_corpus()`(`manifest.py:39`)로
DuckDB에 등록된다. 이때 **`report_type` 파생 컬럼**을 만든다:

```
report_nm "[기재정정]주요사항보고서(전환사채권발행결정)"
  → 앞머리 [기재정정] 제거 → 괄호 안 추출 → report_type = "전환사채권발행결정"
```

주요사항보고서의 세부 유형(유상증자·전환사채발행·자기주식취득·감자·무상증자 등)을
정확히 거르기 위한 것이다. `keyword=전환사채`로 찾으면 "자기전환사채매도결정",
"제3자의전환사채매수선택권행사"까지 섞여 발행 건수를 부풀린다(실제 오답).

지원 파라미터 (전부 선택):

| 축 | 파라미터 |
|---|---|
| 주체 | `company`(발행회사, 쉼표 나열 가능) · `filer`(제출인) |
| 분류 | `sector` · `industry` · `market` · `doc_group` · `doc_subtype` · `report_type` |
| 기간 | `base_year`/`base_month`(회계기간) · `date_from`/`date_to`(접수일) |
| 조건 | `is_correction` · `keyword` · `min_amount`(금액 하한) · `min_ratio`(보유비율 하한) |
| 출력 | `order` · `limit` · `group_by` · `min_count` |

값은 전부 `?` 파라미터 바인딩 — **LLM이 만든 인자가 SQL에 직접 들어가지 않는다.**

**모델의 잘못된 인자를 코드가 교정한다** (전부 실제 실패에서 유래):

| 증상 | 교정 |
|---|---|
| `sector="카카오"` (기업명을 섹터 자리에) | company 필터로 자동 전환 + note |
| `sector="IT"` (업종을 섹터 자리에) | industry 필터로 자동 전환 |
| `company="삼성전자"` + `sector="IT"` (추측한 필터 덧붙임) | company가 풀리면 sector/industry 폐기 |
| 이벤트 공시에 `base_year=2025` | 접수일 범위 `20250101~20251231`로 변환 |
| exchange 전용 subtype을 major에 | 유효 목록을 알려주며 오류 반환 |
| 0건 | `_diagnose_empty()`가 필터를 하나씩 빼보고 "기간을 빼면 3건(최신 2026-03-16)"처럼 안내 |

> **회계연도 vs 접수일 (중요)**
> 정기공시는 회계기간이 끝난 뒤 제출된다. FY2025 사업보고서는 2026-03 접수.
> - `"2025년 실적"` → `base_year=2025`
> - `"2025년에 공시된 것"` → `date_from/date_to`
>
> 접수일로 회계연도를 찾으면 **한 해 전 보고서**가 잡힌다. 실제 발생했던 오답이며
> `tests/test_agent.py`에 회귀 테스트가 있다.
> (분기·반기는 같은 해에 접수되므로 이 함정이 없다: FY2025 1Q → 2025-05-15 접수)

### 4-3. 결정론적 강화 — LLM에게 계산을 시키지 않는다

검색 결과를 그대로 주면 모델은 표를 눈으로 훑다가 틀린다. 실제로 겪은 것들:
20여 건 금액을 암산해 합계가 구성 항목보다 작아지고, 4조와 8천억을 비교해 8천억을
최대라 하고, 21건 동률 둘을 적어놓고 20건을 1위라 결론냈다.

그래서 `dispatch()`는 검색 결과에 **코드가 계산한 값**을 실어 보낸다.

| 붙는 값 | 계산 내용 | 관련 함수 |
|---|---|---|
| `계약금액`·`투자금액`·`계약상대`·`해지사유` | 거래소공시 본문에서 정형 필드 추출 + 억/조 환산 | `tools.py:302` `_contract_fields()` |
| `체결후_유지기간`·`계약기간_길이`·`체결·해지_같은해` | 날짜 차이(년·월·일)와 동일연도 여부 판정 | `tools.py:286` `_duration_between()` |
| `신주수`·`증자전주식총수`·`증자비율`·`전환가액`·`표면이자율`·`사채만기일` | 주요사항보고서 서식 필드 + 비율 나눗셈 | `tools.py:504` `_major_fields()` |
| `매출액`·`영업이익`·`당기순이익`·`주당현금배당금`·`현금배당성향` | 요약재무정보 3항목 + 배당 2항목, **원 단위로 정규화** (문서마다 원/천원/백만원이 달라 증감률이 1000배로 튀던 문제) | `tools.py:631` `_periodic_financials()` |
| `재무_합계` | 여러 보고서 재무값의 코드 합산 (회사·기간 중복 제거, 잘린 결과면 계산 거부) | 〃 |
| `최초공시일`·`최초공시`·`정정사유`·`정정사항_발췌` | 정정본 머리에서 원본 제출일을 뽑아 원본 doc_id까지 연결 | `tools.py:542` `_link_corrections_to_originals()` |
| `field_change_log` | 정정 체인의 필드별 변경 이력을 시간순 diff로 | `tools.py:689` `_field_change_log()` |
| `보유비율_이번/직전`·`보고사유`·`ratio_trend` | 지분공시 머리 요약 파싱 + 비율 시계열 | `tools.py:716` `_holding_summary()` |
| `count`·`correction_count`·`company_count`·`correction_pct` | 건수·정정비율 (단, `is_correction` 필터가 걸린 검색에서는 자명한 0/100%라 제거) | `dispatch()` |
| `최다`(동률 포함)·`최대_건`·`total_count` | 1위 판독과 최대 금액 건을 코드가 지목 | `tools.py:393` `_amount_totals()` |
| `매치없는_기업` | "정정이 한 건도 없는 기업" 같은 **집합차**를 유니버스 대비 계산 | `dispatch()` |
| `date_range` | 조건에 맞는 최초·최후 개별 공시 (집계에는 개별 행이 없어 모델이 지어내던 문제) | 〃 |
| `유니버스_보고자_상위쌍` | 유니버스 기업이 유니버스 기업을 보고한 (보고자→발행회사) 쌍 순위 | 〃 |
| `등기임원_명단` | 임원 표에서 사내·사외이사 행만 추출 (미등기 임원 혼입 방지) | `read_section` 분기 |
| `행_파싱` | 타법인출자 현황 표의 고정 열 순서를 라벨링 (수량을 장부가액으로 오독하던 문제) | `tools.py:600` `_equity_row_parse()` |
| `unit_note` | 본문에 `(단위: 백만원)` 선언이 있으면 단위를 붙이라고 경고 | `read_section` 분기 |

`annotate_amounts()`(`tools.py:239`)는 본문 안의 큰 숫자 **바로 뒤에** 억/조 환산을
괄호로 붙인다. 표마다 선언된 단위(백만원 등)를 추적해 반영하며, 별도 목록으로 주면
정작 필요한 값이 빠지므로 숫자 옆에 직접 붙인다.

### 4-4. `search_fulltext` — 키워드 검색 (SQLite FTS5)

**`app/data/fulltext.py:76`**

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

두 가지 방어가 붙어 있다:
- 질의의 특수문자를 따옴표로 감싸 리터럴 매치로 강제 (`Kakao piccoma Corp.`의 마침표가
  `fts5: syntax error`를 내던 문제)
- 질의가 3자 미만이면(trigram 제약) 오류 대신 **보고서명 검색으로 자동 전환** —
  "감자" 검색이 데드엔드가 되던 문제

### 4-5. `list_sections` / `read_section` — 본문 읽기 (파일)

**`app/data/cache.py:103` `get_parsed()`**

```
① cache/text/{doc_id}/{stem}.txt 가 있으면 재사용
② 없으면 corpus/raw/... 원문을 parser.py로 파싱 후 캐시 저장
```

**`app/data/parser.py:150` `parse_file()`**
- DART XML → `_parse_dart_xml()`: `<TITLE ATOC="Y">`를 섹션 경계로 잡고 표를 `키 | 값` 라인으로 변환
- 거래소 HTML → `_parse_html_filing()`: BeautifulSoup으로 표 추출

산출물 2종:

```
20240312000736.txt              ← 정제 평문
20240312000736.sections.json    ← [{atocid, title, offset}, ...]
```

`.sections.json`의 `offset`이 **부분 읽기의 핵심**이다. 삼성화재 사업보고서는
본문 1,124,669자 / 섹션 152개인데, "2-2. 연결 포괄손익계산서"의 offset부터 다음
섹션 offset까지 슬라이스하면 4,363자만 꺼낼 수 있다.

**섹션 제목은 LLM이 채우므로 4단계로 관대하게 찾는다** (`parser.py:161` `slice_section()`):

```
① 완전/부분 일치 → ② 소문자 비교 → ③ 공백 무시 비교
   ("유상증자결정" vs 목차 "유상증자 결정")
→ ④ 토큰 AND (모든 토큰이 제목에 있는 것 중 가장 짧은 것)
   ("연결 손익계산서" → "연결 포괄손익계산서")
목차에 아예 없으면 → 본문에서 제목 토큰 위치를 찾아 그 주변 반환
   (정정 분기보고서의 "타법인출자 현황(상세)"은 ATOC에 없고 본문에만 있다)
```

**추가 파라미터**:
- `keyword` — 긴 표에서 특정 회사·항목 행으로 바로 점프한다. 표 머리(열 이름·단위 선언)를
  함께 붙여 반환하므로 열을 오독하지 않는다. 못 찾으면 빈손 대신 섹션 전체 + 알림.
- `offset` — 12,000자를 넘으면 페이징 (`tools.py:753` `_page()`):
  ```json
  {"text": "...", "truncated": true, "total_chars": 45000,
   "next_offset": 12000, "note": "offset=12000 으로 다시 호출하세요"}
  ```
- `filename` — 첨부(감사보고서 등) 지정. 경로·확장자 생략·문서 라벨 모두 수용하고,
  파일명 형태가 아니면 본문으로 폴백한다 (`cache.py:43` `_resolve_file()`).

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
| 부분 읽기 | 가능 (섹션 슬라이스, offset 페이징, keyword 점프) | 불가 (통짜 저장) |
| 없으면 | 원문에서 자동 재생성 | 검색만 안 됨 |

**검증**: `cache/text/{doc_id}/`를 지우고 `read_section`을 호출하면 SQLite를
쳐다보지 않고 `corpus/raw` 원문을 다시 파싱해 캐시를 재생성한다.
코드상으로도 `cache.py`는 sqlite를 import하지 않고, `fulltext.py`는
`cache/text/`를 건드리지 않는다.

즉 **답변에 인용되는 문장은 전부 파일에서 나온다.**

> 새 장비에서는 이 두 산출물을 다시 만들 필요가 없다. `scripts/fetch_data.py`가
> GitHub 릴리즈에서 `corpus`(274MB 압축)와 `cache`(1.1GB 압축)를 받아 배치한다.

---

## 6. 실제 추적 예시

질문: `"삼성중공업이 2023년에 체결한 원유운반선 공급계약 금액은?"`

```
[턴 1] get_company_info({"query": "삼성중공업"})
       manifest.py:60 → DuckDB universe
       → corp_name=삼성중공업, stock_code=010140, sector=조선

[턴 2] search_filings({"company":"삼성중공업", "doc_group":"exchange",
                       "doc_subtype":"단일판매공급계약체결",
                       "date_from":"20230101", "date_to":"20231231"})
       manifest.py:177 → DuckDB manifest → 13건
       + 결정론 강화: 각 행에 계약금액·계약상대·계약기간이 이미 붙어 있음
       → exchange_20230602800079 | 계약금액 227,500,000,000 (2,275억)

[턴 3] read_section({"doc_id":"exchange_20230602800079", ...})
       cache.py → cache/text/exchange_20230602800079/20230602800079.txt
       → "1. 판매ㆍ공급계약 구분 | 공사수주
          - 체결계약명 | 원유운반선 2척
          2. 계약내역 | 계약금액(원) | 227,500,000,000(2,275억)
          3. 계약상대 | 오세아니아 지역 선주"  (525자)

[턴 4] 도구 호출 없이 텍스트만 반환 → 최종 답변
       "227,500,000,000원 (약 2,275억원) — (단일판매·공급계약체결, 2023-06-02,
        exchange_20230602800079)"
```

> 이 문항은 초기에 "2조 275억원"으로 틀렸다. 모델이 12자리 숫자를 눈으로 읽다
> 자릿수를 잘못 끊은 것이라, 이후 `annotate_amounts()`가 숫자 옆에 환산값을
> 붙이도록 바꿨다. 4-3의 강화 계층은 대부분 이런 식으로 만들어졌다.

### 어느 경로로 가는가

| 질문 유형 | 경로 |
|---|---|
| "삼성화재 2025년 실적" | 회사+연도+유형으로 특정 → **FTS 불필요** |
| "삼성중공업 2023년 공급계약" | 메타데이터로 좁혀짐 → **FTS 불필요** |
| "감자를 공시한 기업 전부" | `report_type` 필터 한 번 → **FTS 불필요** |
| "원유운반선 수주한 회사가 어디야?" | 어느 문서인지 모름 → **FTS 필요** |
| "Kakao piccoma 최초취득금액" | 본문 표에만 있는 고유명사 → **FTS 필요** |

의도된 동작이다. 공시는 "회사 × 연도 × 유형"으로 이미 구조화돼 있어서
구조 필터(L1)가 대부분을 처리하고 FTS(L2)는 백스톱 역할이다. 처음부터 벡터/
키워드로 뒤지는 일반적인 RAG보다 이 순서가 정확하다.

---

## 7. 실패해도 답이 나오는 이유

| 실패 지점 | 코드 | 대응 |
|---|---|---|
| 도구 인자 JSON 깨짐 | `loop.py:336` | 오류를 결과로 되먹여 모델이 자가 수정 |
| 도구 자체가 예외 | `loop.py:76` | `{"error": ...}`로 감싸 루프 유지 |
| 검색 0건 | `tools.py:347` | 어떤 필터가 죽였는지 진단해 다음 호출을 유도 |
| 잘못된 필터 축 | `tools.py` dispatch | sector↔company↔industry 자동 교정 + note |
| 파일명·섹션명 오지정 | `cache.py:43` / `parser.py:161` | 관대 해석 4단계, 실패 시 사용 가능 목록 안내 |
| 짧은 FTS 질의 | `tools.py` dispatch | 보고서명 검색으로 자동 전환 |
| 빈 LLM 응답 | `loop.py` | 1회 재촉 후 다음 턴 |
| 답변이 도구 안내문 에코 | `loop.py:262` | 실제 답변 재요청 |
| 인용 날조·누락 | `loop.py:270`/`292` | 재작성 요구 |
| 같은 호출 반복 | `loop.py` call_cache | 실행 없이 차단 + 조건 변경 요구, 턴 전체가 반복이면 종합 답변 강제 |
| 429 rate limit | `clova.py:106` | 헤더가 알려주는 실제 시간만큼 대기 |
| 400 40009 | `loop.py:366` | 도구 세트 축소 재시도 |
| 5xx / 네트워크 | `clova.py` | 지수 백오프 재시도 (최대 4회) |
| 턴/시간 예산 소진 | `loop.py:672` | 수집된 정보로 강제 마무리 |
| LLM 완전 불가 | `answer.py:570` | 폴백 답변 |
| 그 외 모든 예외 | `routes.py:85` | 폴백 + **HTTP 200 유지** |

평가 GET이 실패하면 그 문항은 0점이므로, **어떤 경우에도 200 + 유효 JSON**을
반환하는 것이 최우선 원칙이다.

---

## 8. 관련 파일 요약

| 파일 | 줄 | 역할 |
|---|---:|---|
| `app/api/routes.py` | 485 | HTTP 진입점 (`/answer`, `/runs`, `/events`, `/history`, `/chat`, `/filings`, `/usage`) |
| `app/api/events.py` | 155 | SSE 브로드캐스터 + 실행 레지스트리 (대시보드 상태를 서버가 보유) |
| `app/api/history.py` | 128 | 질의기록 조회·삭제 (`logs/answers.jsonl` 기반, 물리 삭제) |
| `app/agent/answer.py` | 605 | 질문 1건 파이프라인, 가드레일, 검색 힌트, 근거 조립, 답변 후처리 |
| `app/agent/loop.py` | 712 | function-calling 루프, 이력 압축·선형화, 자기교정 장치, 이벤트 방출 |
| `app/agent/clova.py` | 400+ | CLOVA Studio HTTP 클라이언트, rate limit 관리, 토큰 추정 |
| `app/agent/tools.py` | 1,748 | 도구 스키마 + dispatcher + **결정론적 강화 계층** |
| `app/agent/calc.py` | — | AST 화이트리스트 계산기, 한국어 금액 표기 |
| `app/agent/system_prompt.py` | 83 | 에이전트 지침 (가드레일 포함) |
| `app/agent/usage.py` | 113 | 토큰 사용량 일별 집계 |
| `app/data/manifest.py` | 408 | DuckDB 메타데이터 검색·집계 (L1), 회사명 해석 |
| `app/data/fulltext.py` | 134 | SQLite FTS5 키워드 검색 (L2) |
| `app/data/cache.py` | 120 | 파싱 결과 캐시, 파일 해석 |
| `app/data/parser.py` | 240 | DART XML / 거래소 HTML → 평문 + 섹션 오프셋 |
| `app/config.py` | — | 경로·모델·예산 설정 |
| `scripts/build_index.py` | — | 오프라인 전처리 (캐시 + FTS 인덱스) |
| `scripts/fetch_data.py` | — | GitHub 릴리즈에서 corpus·cache 복원 |
| `scripts/run_eval.py` | — | 평가 문항 1건 실행 (대시보드 기록에도 남음) |
| `scripts/run_eval_all.py` | — | `docs/EVAL_ALL.md` 전 문항을 `GET /answer`로 순차 실행 → `logs/eval_all_results.jsonl` (`--redo` 전체, `--only V3` 세트, `--ids V1-E-12,…` 지정 문항만 교체) |
| `scripts/grade_eval_all.py` | — | 결과에 자동 채점 신호(수치·doc_id 일치, 인용·거절 여부) 표시, `--detail ID`로 문항 전문 |
| `scripts/make_eval_report.py` | — | 사람 판정(`logs/eval_all_verdicts.json`) + 결과 + 이전 판정(`--prev`)으로 `docs/EVAL_REPORT.md` 생성 (통계·이동·문항별 표 자동, 서술은 `--narrative`) |
| `tests/test_agent.py` | — | 회귀 테스트 188개 (대부분 실제 오답에서 역산) |
