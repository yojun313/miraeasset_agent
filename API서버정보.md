# 평가용 API 서버 정보

제10회 미래에셋증권 AI Festival 예선 · 공시 Agent (팀 dis-047)

## 1. End-point

| 구분 | URL |
|---|---|
| 도메인 (HTTPS) | `https://miraeasset-047-agent.r-e.kr/answer` |
| 상태 확인 | `https://miraeasset-047-agent.r-e.kr/health` |

두 주소는 같은 서버를 가리킵니다. 도메인 주소를 기본으로 쓰고, 인증서나 DNS 문제가 있을 때 IP 주소를 대신 써 주십시오.
평가 기간(2026-09-07 ~ 09-20) 동안 서버를 계속 켜 둡니다.

`/health`는 서버가 살아 있는지와 모델 설정, 검색 색인이 준비됐는지를 돌려줍니다.

```json
{"status": "ok", "model": "HCX-007", "clova_key_set": true, "fts_index_built": true}
```

## 2. 요청

`GET /answer` 하나로 평가 질의를 받습니다. 질의 하나가 독립된 요청이며 이전 질의의 문맥은 쓰지 않습니다.

| 파라미터 | 위치 | 타입 | 필수 | 설명 |
|---|---|---|---|---|
| `question_id` | query string | string | 권장 | 평가 문항 식별자. 응답에 그대로 돌려줍니다. 비워도 처리됩니다. |
| `question` | query string | string | 필수 | 평가 질의 원문. UTF-8로 URL 인코딩합니다. 4,000자를 넘는 부분은 잘라서 처리합니다. |

```bash
curl -G "https://miraeasset-047-agent.r-e.kr/answer" \
  --data-urlencode "question_id=Q-001" \
  --data-urlencode "question=삼성중공업이 2023년 6월 2일에 공시한 원유운반선 2척 공급계약의 계약금액은 얼마인가?"
```

```python
import requests

resp = requests.get(
    "https://miraeasset-047-agent.r-e.kr/answer",
    params={"question_id": "Q-001", "question": "평가 질의"},
    timeout=300,
)
result = resp.json()
```

### 처리 시간과 타임아웃

- 질의 하나에 보통 5초에서 60초가 걸립니다. 자체 평가 119문항 기준 평균 22초, 여러 문서를 읽어야 하는 문항은 2분 안팎이었습니다.
- 서버는 요청 하나에 최대 240초의 처리 예산을 둡니다. 예산이 다하면 그때까지 모은 근거로 답을 마무리해서 돌려줍니다.
- 클라이언트 타임아웃은 300초 이상으로 잡아 주십시오.
- 같은 `(question_id, question)` 조합으로 다시 호출하면 첫 성공 응답을 그대로 돌려줍니다(수 ms). 클라이언트 쪽 타임아웃으로 재호출하더라도 같은 답을 받습니다.
- 요청은 서버 안에서 순차 처리됩니다. 여러 질의를 동시에 보내면 순서대로 처리되므로 뒤쪽 요청은 그만큼 늦게 끝납니다. 한 번에 하나씩 보내는 방식을 권장합니다.

## 3. 응답

항상 HTTP 200과 아래 다섯 필드를 가진 JSON을 돌려줍니다. 내부 오류가 나도 500을 내지 않고 같은 형식으로 폴백 답변을 돌려줍니다.

| 필드 | 타입 | 설명 |
|---|---|---|
| `question_id` | string | 요청의 `question_id`를 그대로 반환 |
| `question` | string | 요청의 `question`을 그대로 반환 |
| `retrieved_context` | string | 답변에 실제로 쓴 근거. 열람한 공시 본문, 본문 검색 발췌, 검색된 공시 목록 순서로 묶은 텍스트 (최대 30,000자) |
| `think_trace` | string | 턴별 도구 호출과 그 결과 요약, 자기 교정 이벤트, 최종 답변 생성 시점을 줄 단위로 기록한 텍스트 |
| `answer` | string | 최종 답변. 근거 공시를 `(보고서명, 접수일, doc_id)` 형식으로 표시 |

### JSON Schema

요청(query string):

```json
{
  "type": "object",
  "properties": {
    "question_id": {"type": "string", "description": "평가 문항 식별자", "default": ""},
    "question":    {"type": "string", "description": "평가 질의 원문 (UTF-8, URL 인코딩)", "maxLength": 4000}
  },
  "required": ["question"]
}
```

응답(body, `application/json`):

```json
{
  "type": "object",
  "properties": {
    "question_id":       {"type": "string"},
    "question":          {"type": "string"},
    "retrieved_context": {"type": "string"},
    "think_trace":       {"type": "string"},
    "answer":            {"type": "string"}
  },
  "required": ["question_id", "question", "retrieved_context", "think_trace", "answer"],
  "additionalProperties": false
}
```

### 응답 예시 (실제 서버 응답)

```json
{
  "question_id": "Q-001",
  "question": "삼성중공업이 2023년 6월 2일에 공시한 원유운반선 2척 공급계약의 계약금액은 얼마인가?",
  "retrieved_context": "## 검색된 공시 목록\n- (집계) 총 1건 = 원본 1건 + [기재정정] 0건, 기업 1곳. 건수·기업 수를 답할 때는 이 집계값을 그대로 사용하세요 — 목록을 직접 세지 마세요.\n- [exchange_20230602800079] 삼성중공업 단일판매ㆍ공급계약체결 (접수일 2023-06-02)",
  "think_trace": "[턴 1] 도구 호출: search_filings({\"company\": \"삼성중공업\", \"date_from\": \"20230602\", \"date_to\": \"20230609\", \"doc_group\": \"exchange\", \"doc_subtype\": \"단일판매공급계약체결\"}) → 1건 결과\n[턴 2] 최종 답변 생성",
  "answer": "삼성중공업이 2023년 6월 2일에 공시한 원유운반선 2척 공급계약의 계약금액은 **2,275억원**입니다.\n\n근거 공시:\n- (단일판매·공급계약체결, 2023-06-02, exchange_20230602800079)\n\n해당 공시에 따르면 삼성중공업은 오세아니아 지역 선주와 2023년 6월 2일부터 2026년 2월 28일까지 약 2년 8개월 동안 원유운반선 2척을 건조하여 공급하기로 계약을 체결하였으며, 계약금액은 2,275억원으로 매출액 대비 3.8%에 해당한다고 밝혔습니다."
}
```

### 각 필드의 형식

`think_trace`는 한 줄이 한 사건입니다.

```
[턴 1] 도구 호출: search_filings({...}) → 3건 결과 (정정 1건 포함)
[턴 2] 도구 호출: read_section({"doc_id": "...", "section_title": "..."}) → 본문 924자
[턴 3] 근거 공시 표기 누락 — 인용 추가 요구
[턴 4] 최종 답변 생성
```

도구 호출 외에 서버가 모델에게 자기 교정을 요구한 사건(근거 누락, 실재하지 않는 접수번호 인용, 미완성 답변, 읽지 않고 포기 등)도 같은 줄 형식으로 남습니다.

`retrieved_context`는 아래 세 묶음 중 실제로 사용된 것만 순서대로 담습니다.

```
## 열람한 공시 본문        read_section 으로 읽은 본문 (doc_id, 섹션 제목, 본문)
## 키워드 검색 결과 발췌    search_fulltext 가 돌려준 문장 단위 발췌
## 검색된 공시 목록        search_filings 결과의 메타데이터와 (집계) 라인
```

`answer`의 공시 식별자 `doc_id`는 `공시그룹_접수번호` 형태입니다. 접수번호 14자리는 DART 접수번호와 같습니다.

| 접두어 | 공시 그룹 |
|---|---|
| `periodic_` | 정기공시 (사업·반기·분기보고서) |
| `major_` | 주요사항보고서 |
| `exchange_` | 거래소공시 (단일판매·공급계약 체결/해지, 신규시설투자, 투자판단 관련 주요경영사항) |
| `holding_` | 지분공시 (주식등의 대량보유상황보고서) |

## 4. 특수한 경우의 응답

| 상황 | 응답 |
|---|---|
| `question`이 비어 있음 | `answer`에 질문 형식 안내, `think_trace`는 `입력 검증: 빈 질문` |
| 시스템 프롬프트·내부 지침을 묻는 질의 | 도구를 실행하지 않고 역할 소개와 함께 공개 대상이 아니라고 답함 |
| 주민등록번호·자택 주소 등 개인정보 요구 | 도구를 실행하지 않고 거절. 공시에 그런 항목이 없음도 함께 안내 |
| 주가 전망·투자 의견 요구 | 전망과 의견은 제공하지 않는다고 답하고, 공시에 적힌 사실만 전달 |
| 제공 코퍼스에 없는 기업·기간·공시 유형 | "확인할 수 없음"을 명시하고, 어느 범위까지 수집돼 있는지 안내 |
| 처리 예산(240초) 초과 | 그때까지 모은 근거로 답을 마무리 |
| 내부 오류 | HTTP 200, `answer`에 "일시적인 시스템 문제로 이 질의를 처리하지 못했습니다" 문구, `think_trace`에 `internal error: <예외명>` |

## 5. 서버 환경

| 항목 | 내용 |
|---|---|
| 호스팅 | AWS EC2, 애플리케이션 포트 10000 |
| 도메인 | `miraeasset-047-agent.r-e.kr` (HTTPS) |
| 애플리케이션 | Python 3.12 기준, FastAPI + uvicorn (`app/main.py`) |
| LLM | HyperCLOVA X `HCX-007` (CLOVA Studio, OpenAI 호환 API). 다른 LLM은 쓰지 않음 |
| 데이터 | 주최 측 제공 공시 원문과 구조화 데이터만 사용. 외부 API·외부 문서 호출 없음 |
| 저장소 | 공시 메타데이터 DuckDB(메모리), 본문 텍스트 캐시, SQLite FTS5 전문 색인 |
| 로그 | 모든 `/answer` 호출을 `logs/answers.jsonl`에 기록 |

같은 서버에 운영용 대시보드(`/dashboard`)가 함께 떠 있습니다. 대시보드는 같은 답변 로직을 쓰며 평가 API의 동작에는 영향을 주지 않습니다.

## 6. 소스 코드와 재현

- 저장소: 주최 측 GitHub Organization의 `dis-047` 레포지토리
- 실행 방법과 환경 구성은 `README.md`에 있습니다. 공시 데이터와 색인은 `scripts/fetch_data.py`로 내려받습니다.
- 서버 진입점: `app/api/routes.py`의 `answer()`, 답변 파이프라인: `app/agent/answer.py`의 `answer_question()`
