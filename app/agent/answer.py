"""Single-question answer pipeline for the evaluation endpoint (GET /answer).

Wraps the agent loop with input sanitation, a wall-clock budget,
retrieved-context assembly from the tool-call log, and a think-trace string.

Contract: answer_question() returns a well-formed AnswerOutcome for every
expected failure mode (LLM down, budget exhausted, empty question). Only a
genuine bug escapes as an exception — and the API route has a final
catch-all so the evaluator still receives valid JSON.
"""

from __future__ import annotations

import datetime
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable

from app.agent import clova
from app.agent.loop import run_agent
from app.config import ANSWER_CONTEXT_MAX_CHARS, ANSWER_TIME_BUDGET
from app.data.manifest import get_filing

logger = logging.getLogger(__name__)

_MAX_QUESTION_CHARS = 4_000
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# 외부 정보원 안내는 시스템 프롬프트로 금지했지만, 강제 마무리 경로처럼
# 모델이 궁지에 몰리면 여전히 새어 나온다. 마지막 방어선으로 문장 단위로 걷어낸다.
# 오탐을 막기 위해 "외부 출처"와 "권유 동사"가 같은 문장에 있을 때만 제거한다.
_EXTERNAL_SOURCE = re.compile(
    r"네이버\s*(금융|증권)|다음\s*금융|구글|뉴스|기사|증권사"
    r"|금융\s*(정보\s*)?(플랫폼|사이트|서비스)|DART|다트|한국거래소|금융감독원|금감원"
    r"|전자공시|공시\s*시스템|공식\s*(?:적인\s*)?(홈페이지|웹\s*사이트|사이트|채널|경로)|IR\s*(자료|페이지)"
    r"|애널리스트|전문가|공식\s*발표\s*자료|보도\s*자료|고객\s*센터|담당\s*부서"
    r"|회사\s*(측|에)|해당\s*기업에|투자자\s*관계|공시\s*담당자"
)
_REFERRAL_VERB = re.compile(
    r"참고|참조|확인(해\s*(보시|주시|보세)|할\s*수\s*있|하실\s*수|이\s*가능)"
    r"|조회(해\s*(보시|주시)|할\s*수\s*있|하실\s*수)|방문|검색해\s*(보시|주시)"
    r"|이용(해\s*(보시|주시)|하시)|권(장|해)|문의|알아보|찾아보시|조언|자문|상담"
    r"|직접\s*확인|통해\s*확인|에서\s*확인"
)


_CONV_RE = re.compile(
    r"([\d,]+)\s*(백만원|천원)[^\n]{0,60}?환산하면\s*([\d,]+)\s*원"
)
# "118,235백만원 = 11조 8,223억 5천만원" 처럼 한글 금액으로 환산한 문장 (100배 오류 실측)
_CONV_KOR_RE = re.compile(
    r"([\d,]+)\s*(백만원|천원)[^\n]{0,40}?(?:=|≈|약|즉|으로|는|은)\s*(?=\d)"
    r"((?:\d[\d,]*\s*조\s*)?(?:\d[\d,]*\s*억\s*)?(?:\d[\d,]*\s*천?만\s*)?(?:\d[\d,]*\s*천\s*)?원?)"
)
_UNIT_MULT = {"백만원": 1_000_000, "천원": 1_000}


def _parse_korean_amount(text: str) -> int | None:
    """'11조 8,223억 5천만' → 정수 원. 조·억·만 단위만 지원."""
    total, found = 0, False
    for num, unit in re.findall(r"(\d[\d,]*)\s*(조|억|천만|만|천)", text):
        found = True
        v = int(num.replace(",", ""))
        total += v * {"조": 10**12, "억": 10**8, "천만": 10**7, "만": 10**4, "천": 10**3}[unit]
    return total if found else None


_DART_URL_RE = re.compile(r"https?://dart\.fss\.or\.kr\S*?rcpNo=(\d{14})\S*")
_DART_URL_BARE_RE = re.compile(r"https?://dart\.fss\.or\.kr\S*")


_ANY_DOCID_RE = re.compile(r"(?:major|periodic|exchange|holding)_(\d{14})|(?<!\d)(20\d{12})(?!\d)")


def strip_fabricated_ids(answer: str, tool_calls: list) -> str:
    """도구 결과에 없는 접수번호·doc_id를 답변에서 지운다 — 재촉 1회로도 안 고쳐져
    실재하지 않는 번호 3개가 근거로 나간 실제 사례(V2-14)의 최종 방어선."""
    import json as _json
    seen = set(re.findall(r"(?<!\d)20\d{12}(?!\d)", _json.dumps(tool_calls, ensure_ascii=False, default=str)))
    if not seen:
        return answer
    def repl(m: "re.Match[str]") -> str:
        num = m.group(1) or m.group(2)
        return m.group(0) if num in seen else "(접수번호 미확인)"
    return _ANY_DOCID_RE.sub(repl, answer)


_DART_MD_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(\s*(https?://dart\.fss\.or\.kr[^)\s]*)\s*\)")


def strip_dart_urls(answer: str) -> str:
    """외부 DART 링크를 접수번호 표기로 바꾼다 — 외부 참조 금지 규칙의 마지막 방어선."""
    # 마크다운 링크 "[보고서명](https://dart…rcpNo=N)" → "보고서명 (접수번호 N)".
    # URL만 바꾸면 "[보고서명]((접수번호 N)" 찌꺼기가 남는다(V2-14·V1-M-06 3차 실측).
    def _md(m: "re.Match[str]") -> str:
        no = re.search(r"rcpNo=(\d{14})", m.group(2))
        return f"{m.group(1)} (접수번호 {no.group(1)})" if no else m.group(1)
    answer = _DART_MD_LINK_RE.sub(_md, answer)
    answer = _DART_URL_RE.sub(lambda m: f"(접수번호 {m.group(1)})", answer)
    return _DART_URL_BARE_RE.sub("", answer)


# 자기 교정 재촉을 받은 뒤 "죄송합니다. 이전 응답에서 도구 호출 결과를 잘못
# 해석했습니다"처럼 내부 진행 상황을 사과문으로 여는 실제 사례가 있었다.
# 평가자에게는 답변만 보여야 하므로 이런 메타 문장을 걷어낸다.
# ("확인할 수 없습니다" 같은 정당한 한계 고지는 건드리지 않는다.)
_SELF_REF_RE = re.compile(
    r"(?:이전|앞선|직전|위)\s*(?:의\s*)?(?:응답|답변)[^.\n]*"
    r"(?:잘못|오류|정정|수정|재작성|다시)"
    r"|도구\s*(?:호출|사용)\s*결과[^.\n]*(?:잘못|오류|해석)"
    r"|다시\s*정리하겠습니다"
    r"|올바른\s*정보를?\s*(?:제공|안내)하기\s*위해"
    # 평가 답변에 섞인 인사·사과·잡담 (V2-05·삼성중공업 실측)
    r"|^\s*(?:죄송합니다|죄송해요|사과드립니다)[.!]?\s*$"
    r"|도움이\s*되지\s*못해"
    r"|제가\s*접근한\s*방식으로는"
    r"|앞서\s*제공된\s*도구|다시\s*시도하였으나|말씀드릴\s*수밖에\s*없습니다"
    r"|잘못된\s*정보를\s*(?:제공|드렸)|(?:정확한\s*정보를\s*(?:제공|드리)기\s*위해\s*)?다시\s*확인해\s*?보겠습니다"
    r"|(?:추가로|추가적으로|추가적인|다른|더)\s*(?:궁금|문의|질문|필요|정보)[^.\n]*(?:말씀|알려|문의|요청)"
    r"|추가적인\s*정보가\s*필요할\s*경우[^.\n]*(?:검토|확인)해야"
)


def strip_self_reference(answer: str) -> str:
    """답변에서 내부 진행 상황·사과 문장을 제거한다."""
    if not answer or not _SELF_REF_RE.search(answer):
        return answer
    kept: list[str] = []
    for line in answer.split("\n"):
        pieces = re.split(r"(?<=[.!?。])\s+", line)
        surviving = [p for p in pieces if not _SELF_REF_RE.search(p)]
        if len(surviving) != len(pieces) and not "".join(surviving).strip():
            continue  # 줄 전체가 메타 문장이면 줄째로 버린다
        kept.append(" ".join(surviving).rstrip() if surviving else "")
    cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()
    # 메타 문장을 걷어내면 "죄송합니다."만 홀로 남는다 — 뒤에 실제 답변이
    # 있으면 이 인사말도 지운다 ("죄송하지만 확인할 수 없습니다"처럼 한 문장에
    # 내용이 함께 있는 정당한 한계 고지는 위 필터에 걸리지 않아 그대로 남는다).
    cleaned = re.sub(r"^\s*(?:죄송합니다|죄송해요|사과드립니다)[.!]?\s*\n+", "", cleaned)
    return cleaned.strip() or answer  # 전부 걷혔으면 원본 유지


def fix_unit_conversions(answer: str) -> str:
    """"905백만원 ... 환산하면 90,500,000원" 같은 10배 오류(실측)를 코드로 교정한다."""
    def repl(m: "re.Match[str]") -> str:
        try:
            base = int(m.group(1).replace(",", ""))
            claimed = int(m.group(3).replace(",", ""))
        except ValueError:
            return m.group(0)
        correct = base * _UNIT_MULT[m.group(2)]
        if claimed == correct:
            return m.group(0)
        return m.group(0).replace(m.group(3), f"{correct:,}")
    answer = _CONV_RE.sub(repl, answer)

    def repl_kor(m: "re.Match[str]") -> str:
        try:
            base = int(m.group(1).replace(",", ""))
        except ValueError:
            return m.group(0)
        claimed = _parse_korean_amount(m.group(3))
        if claimed is None or claimed == 0:
            return m.group(0)
        correct = base * _UNIT_MULT[m.group(2)]
        # 10% 이내면 반올림 표기로 보고 통과, 그 밖이면 자릿수 오류로 교정
        if abs(claimed - correct) <= correct * 0.1:
            return m.group(0)
        from app.agent.calc import korean_amount_short
        return m.group(0).replace(m.group(3), korean_amount_short(correct) + "원")
    return _CONV_KOR_RE.sub(repl_kor, answer)


def strip_external_referrals(answer: str) -> str:
    """답변에서 '외부 사이트에서 확인하세요' 류 문장을 제거한다."""
    if not answer:
        return answer
    kept: list[str] = []
    for line in answer.split("\n"):
        # 한 줄 안에서도 문장 단위로 판단 (문장부호 뒤에서 분리)
        pieces = re.split(r"(?<=[.!?。])\s+", line)
        surviving = [
            p for p in pieces
            if not (_EXTERNAL_SOURCE.search(p) and _REFERRAL_VERB.search(p))
        ]
        if len(surviving) != len(pieces) and not "".join(surviving).strip():
            continue  # 줄 전체가 안내문이었으면 줄째로 버린다
        kept.append(" ".join(surviving).rstrip() if surviving else "")
    cleaned = "\n".join(kept)
    # 제거 결과로 생긴 꼬리 빈 줄 정리
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

# --- 검색 힌트 라우터 -----------------------------------------------------
#
# HCX-007은 시스템 프롬프트의 검색 규칙을 자주 무시한다. 실제 사례: "3월 20일
# 결정한 유상증자" 질문에 엉뚱한 doc_subtype(투자판단관련주요경영사항)과
# date_to 없는 date_from을 걸어 12월 공시를 읽고 "확인 불가"로 답했다.
# 질문 키워드에서 결정론적으로 올바른 검색 경로를 뽑아 힌트로 주입한다.
# 힌트는 짧아야 한다. 길어지면 HCX-007이 없는 함수를 호출하려다 400 40009 를
# 낸다 (실측: 힌트 없음 3/3, 50자 힌트 3/3, 250자 힌트 0/3). 각 힌트는 한 문장,
# 전체는 _HINT_BUDGET 자 이내로 유지한다.
_HINT_BUDGET = 200

_TOPIC_ROUTES: tuple[tuple[tuple[str, ...], str], ...] = (
    # 회계연도/접수일 개념을 묻는 메타 질문 — 정기재무 라우트보다 먼저 와야 한다
    # 부정형(집합차) 질문 — is_correction=false로 '원본이 있는 기업'을 구해
    # "정정 없는 기업"이라 답한 논리 반전 사례(H-02). 있는 쪽을 집계하고
    # 코드가 계산한 여집합(매치없는_기업)을 쓰게 한다.
    (("한 건도 없", "없었던 기업", "없었던 회사", "하지 않은 회사", "않은 기업", "없는 회사는"),
     "'없었던 기업'은 반대로 그 공시가 있는 기업을 group_by=corp_name 으로 집계하세요 "
     "(필터는 '있는' 쪽 기준, 예: 정정 없는 곳 → is_correction=true). 결과의 "
     "매치없는_기업 목록이 답입니다 — 직접 빼기 하지 마세요"),
    # 앞선 대화를 전제하는 지시어("같은 보고서") — 이 API는 무상태 단발 질의라
    # 지시 대상이 없다. 후보를 훑어 해당 항목이 실재하는 문서를 찾게 유도한다.
    (("같은 카카오", "같은 회사의", "해당 보고서", "그 보고서", "위 보고서", "같은 정정", "같은 분기보고서"),
     "이 질의는 단발성이라 '같은 보고서'가 특정되지 않습니다. 조건에 맞는 후보를 모두 "
     "검색한 뒤 read_section(keyword=찾는 항목)으로 각 후보를 확인하세요"),
    # 키워드는 비교·설명형 표현으로만 좁힌다 — "접수일 기준"처럼 일반적인 어구를
    # 넣으면 경계 질문(V2-03)까지 낚아채 진짜 필요한 힌트를 밀어낸 회귀가 있었다.
    (("같은 문서", "같은 보고서", "같은 표현", "공시한 정기보고서"),
     "회계연도와 접수일은 다릅니다: FY20XX 사업보고서는 20XX+1년 3월에 접수됩니다. "
     "따라서 'N년에 공시한 정기보고서'(접수일 기준)는 전년도 사업보고서 + N년 분기·반기보고서이고, "
     "'N년 실적'(base_year 기준)과 다른 문서 집합입니다"),
    (("유상증자", "무상증자", "제3자배정", "주주배정"),
     "유상증자는 doc_group=major, keyword=유상증자 로 찾으세요"),
    (("전환사채", "CB"),
     "전환사채 발행 질문은 doc_group=major, report_type=전환사채권발행 으로 찾으세요 "
     "(매도·매수선택권 행사는 별개 유형이니 섞지 마세요)"),
    (("교환사채", "EB"), "교환사채는 doc_group=major, keyword=교환사채 로 찾으세요"),
    (("신주인수권부사채", "BW"), "doc_group=major, keyword=신주인수권 으로 찾으세요"),
    (("자기주식", "자사주"), "doc_group=major, keyword=자기주식 으로 찾으세요"),
    (("합병",), "doc_group=major, keyword=합병 으로 찾으세요"),
    (("신종자본증권", "조건부자본증권"),
     "doc_group=major, report_type=조건부자본증권 으로 찾으세요 (서식명: 상각형조건부자본증권발행결정)"),
    (("주주총회", "주총 결과", "주총결과", "임원 선임 공시", "영업정지", "소송 공시"),
     "주주총회 결과 등은 수집 대상이 아닙니다(코퍼스는 정기·주요사항·거래소 4유형·지분공시만). "
     "다른 문서로 대체하지 말고 미수집이라 답하세요"),
    (("감자",), "감자는 doc_group=major, report_type=감자 로 찾고, 방법·비율·일정 등 "
     "상세는 read_section 으로 그 공시 본문을 읽으세요"),
    (("무상증자",), "무상증자는 doc_group=major, report_type=무상증자 로 찾으세요"),
    (("분할",), "doc_group=major, keyword=분할 로 찾으세요"),
    (("거래소공시",),
     "거래소공시 전체는 doc_group=exchange 만으로 — 질문에 없는 doc_subtype을 덧붙이면 일부만 셉니다"),
    (("해지",),
     "해지는 doc_subtype=단일판매공급계약해지 로 기간 제한 없이 검색하세요. 체결·해지 시점 "
     "비교는 행에 계산된 계약기간_시작·해지일자·체결후_유지기간 필드로만 판단하세요"),
    # 고유 사업명(○○구역 정비사업, ○○프로젝트)은 보고서명이 아니라 본문에만
    # 있다 — 체결 공시만 뒤지다 엉뚱한 계약을 답한 실제 사례(G-01). 그 사업이
    # 아직 계약 전 단계(투자판단 공시)일 수도 있으니 본문 검색이 정확하다.
    (("구역", "정비사업", "프로젝트", "지구"),
     "특정 사업 이름은 보고서명에 없습니다 — search_fulltext로 본문을 검색해 문서를 "
     "찾으세요. 계약 공시 목록에 없다고 '없음'으로 결론내지 마세요"),
    (("공급계약", "수주", "계약"),
     "doc_group=exchange, doc_subtype=단일판매공급계약체결 로 찾으세요"),
    (("시설투자", "설비투자"), "doc_group=exchange, doc_subtype=신규시설투자등 으로 찾으세요"),
    (("유니버스 소속 기업이", "유니버스 기업이", "계열사가 보고"),
     "특정 회사를 조회하지 말고 doc_group=holding, group_by=flr_nm 로 집계하세요 — "
     "결과의 유니버스_보고자_상위쌍이 (보고자→발행회사) 쌍 순위입니다"),
    (("대량보유", "5%", "지분 변동", "지분변동", "보유비율", "지분"),
     "지분공시는 발행회사를 company 에, 보고한 회사 이름을 filer 에 넣으세요. "
     "보고자별 집계는 group_by=flr_nm"),
    (("등기임원", "임원 현황", "임원현황", "사외이사 명단", "이사회 구성"),
     "임원 현황은 periodic 보고서의 임원 및 직원 등에 관한 사항 섹션 표에서 등기임원 "
     "전원을 나열하세요(offset으로 끝까지). 생년월일은 지어내지 말 것 — 공시엔 출생년월까지만 있습니다"),
    (("사업보고서", "반기보고서", "분기보고서", "매출", "영업이익", "순이익", "실적", "재무"),
     "doc_group=periodic, base_year=회계연도. read_section(section_title=질문의 표 이름 "
     "또는 1. 요약재무정보, keyword=항목명)으로 좁혀 읽으세요"),
)

# "각각"은 넣지 않는다 — "가장 이른/늦은 공시는 각각?" 같은 개별 조회 질문까지
# 집계로 유도해 빈 컨텍스트를 만든 실제 오답(V2-03)이 있었다.
_STATS_KEYWORDS = ("가장 많", "가장 높", "가장 큰 곳", "상위", "몇 곳", "몇 개사",
                   "몇 건", "비율이", "순위", "별로 정리", "보고자", "집계")
_STATS_HINT = ("여러 회사 비교는 group_by=corp_name 집계로 (나열된 몇 곳은 company=\"A,B,C\" "
               "함께). 그룹마다 correction_pct 제공")

_DATE_RE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")
# 사업·현장 고유명("사직4구역", "장위10구역", "청주테크노폴리스지구")은 보고서명에
# 없고 본문에만 있다 — 계약 목록만 훑고 "없음"으로 답한 실제 사례 대응.
_PROJECT_RE = re.compile(r"([가-힣A-Za-z0-9]{2,}\s?\d*\s?(?:구역|지구))")
_PERIOD_RE = re.compile(r"(\d{4})\s*년\s*(상반기|하반기|([1-4])\s*분기)")
_PERIOD_RANGES = {
    "상반기": ("0101", "0630"), "하반기": ("0701", "1231"),
    "1": ("0101", "0331"), "2": ("0401", "0630"),
    "3": ("0701", "0930"), "4": ("1001", "1231"),
}

_MULTI_ASK_RE = re.compile(
    r"(찾고|정리하고|알려주고|비교하고)[,\s]|(얼마|무엇|언제|어디)(인가)?이며|이며.*(무엇|얼마|언제)|각각"
)


def build_search_hint(question: str) -> str | None:
    """질문에서 뽑은 결정론적 검색 힌트. 해당 없으면 None.

    힌트는 반드시 짧게 유지한다 — 길면 모델이 없는 함수를 호출하려다 실패한다."""
    hints: list[str] = []

    # 두 가지 이상을 묻는 질문에서 앞 절반을 통째로 빼먹은 실제 사례(V3-04:
    # "기업을 모두 찾고 + 상세를 알려줘"에서 상세만 답변) — 짧게 못박는다
    if _MULTI_ASK_RE.search(question):
        hints.append("질문이 요구하는 항목이 여러 개입니다 — 하나도 빼지 말고 모두 답하세요")

    if any(k in question for k in ("어느 쪽이 높", "어느 쪽이 큰", "더 높은가", "더 큰가", "비교하면",
                                     "얼마나 많", "더 큰 곳", "더 많은", "더 높은", "중 어디", "중 가장",
                                     "차이는")):
        hints.append("비교 대상 회사가 나열됐으면 company=\"A,B\" 로 한 번에 검색하세요(sector 금지). "
                     "결론은 calculate로 실제 숫자를 빼 부호를 확인한 뒤 쓰세요")

    m_ratio = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|퍼센트|프로)\s*이상", question)
    if m_ratio:
        hints.append(f"반드시 search_filings(doc_group=holding, filer=보고자, min_ratio={m_ratio.group(1)}) "
                     "한 번으로 거르세요. group_by 집계로는 비율을 알 수 없습니다. 결과 note의 기업 목록이 답입니다")

    if any(k in question for k in ("조원 이상", "억원 이상", "억 이상", "원 이상인")):
        hints.append("금액 조건은 min_amount(원 단위 정수)로 거르세요. 건수는 is_correction=false "
                     "원본 기준으로 세고 그 기준을 답에 명시하세요")

    if any(k in question for k in ("합계", "총합", "모두 더", "합산", "총액을 계산")):
        hints.append("실적 합계는 sector/industry 필터 + base_year 로 연도마다 한 번에, limit 없이 "
                     "검색하세요. 결과의 재무_합계가 코드 합산값입니다 — 본문 읽기·직접 덧셈 불필요")

    if any(k in question for k in ("존재하는가", "존재하나", "있는가?", "사례가 있")):
        hints.append("존재 여부: 검색 행의 판정·계산 필드를 근거로만 답하세요. 조건을 만족하는 "
                     "행을 하나도 못 찾았으면 '존재하지 않는다'고 단언하고, 반례 없이 '있다'고 하지 마세요")

    # 통계·순위 질문은 접근 방식 자체가 달라 가장 먼저 온다
    if any(k in question for k in _STATS_KEYWORDS):
        hints.append(_STATS_HINT)

    for keywords, route in _TOPIC_ROUTES:
        if any(k in question for k in keywords):
            hints.append(route)
            break  # 토픽 힌트는 하나만 — 여러 개면 길어져 역효과

    rp = re.search(r"(\d{4})\s*년\s*(반기보고서|사업보고서|1분기\s*보고서|3분기\s*보고서)", question)
    if rp:
        year, kind = rp.group(1), rp.group(2).replace(" ", "")
        month = {"반기보고서": 6, "사업보고서": 12, "1분기보고서": 3, "3분기보고서": 9}[kind]
        hints.append(
            f"{year}년 {kind}는 base_year={year}, base_month={month} 로만. "
            "없으면 미수집 — 다른 연도·분기 수치로 대체 금지"
        )

    pm = _PERIOD_RE.search(question)
    if pm and not rp:
        year, key = pm.group(1), (pm.group(3) or pm.group(2))
        financial = any(k in question for k in (
            "실적", "매출", "영업이익", "순이익", "재무",
            # "1분기 분기보고서의 타법인출자"처럼 재무 키워드가 없어도 보고서
            # 자체를 지칭하면 접수일이 아니라 기준기간(base_month)으로 찾아야 한다
            "분기보고서", "반기보고서", "사업보고서", "정기보고서"))
        if financial:
            # 기간별 실적은 해당 기준기간의 정기보고서로만 답할 수 있다.
            # "상반기"에 1분기 보고서 수치를 그대로 내놓은 실제 오답 대응.
            month = {"상반기": 6, "1": 3, "2": 6, "3": 9, "4": 12, "하반기": 12}[key]
            report = {"상반기": "반기보고서", "1": "1분기 보고서", "2": "반기보고서",
                      "3": "3분기 보고서", "4": "사업보고서", "하반기": "사업보고서"}[key]
            hints.append(
                f"{pm.group(0)}는 {report}(base_year={year}, base_month={month})로만 "
                "답하세요. 없으면 확인 불가라 답하고 다른 분기로 대체하지 마세요"
            )
        else:
            lo, hi = _PERIOD_RANGES[key]
            hints.append(f"{pm.group(0)}는 date_from={year}{lo}, date_to={year}{hi}")

    # "2024년 3월" 같은 특정 월 — 1~3월(분기)로 넓혀 318건을 답한 사례(V3-13)
    months = re.findall(r"(20\d{2})년\s*(\d{1,2})월(?!\s*(?:\d|말))", question)
    seen_m: list[str] = []
    for y_, m_ in months:
        mi = int(m_)
        if not 1 <= mi <= 12:
            continue
        import calendar as _cal
        last = _cal.monthrange(int(y_), mi)[1]
        tag = f"{y_}년 {mi}월=date_from={y_}{mi:02d}01,date_to={y_}{mi:02d}{last:02d}"
        if tag not in seen_m:
            seen_m.append(tag)
    if seen_m:
        hints.append("월 범위(접수일): " + "; ".join(seen_m[:3]) + " (분기로 넓히지 마세요)")
    # 표 안의 특정 행(고유명사·㈜법인명·영문 법인명)을 묻는 질문 — 섹션을 잘못 고르고
    # 한 번 읽은 뒤 '확인 불가'로 끝낸 사례(ADV-D-01 사후 점검). keyword 점프를 지목한다.
    ent = re.search(r"(㈜\s*[가-힣A-Za-z]{2,}|[가-힣]{2,}홀딩스|[A-Z][A-Za-z]+(?:\s+[A-Za-z.]+){1,3}\s*(?:Corp|Inc|Ltd|Co)\.?)", question)
    if ent and re.search(r"현황|표|항목|장부가액|취득금액|출자", question):
        kw = re.sub(r"(?:에서|에게|에|의|은|는|이|가|을|를)$", "", ent.group(1).strip().rstrip("."))
        hints.append(f"read_section(section_title=해당 표 제목, keyword=\"{kw}\") 로 그 행 위치까지 바로 읽으세요")
    yr = re.search(r"(20\d{2})\s*(?:년)?\s*[~∼～\-–]\s*(20\d{2})\s*년", question)
    if yr and int(yr.group(1)) < int(yr.group(2)):
        hints.append(f"{yr.group(0).strip()} 전체는 date_from={yr.group(1)}0101, date_to={yr.group(2)}1231 "
                     "(base_year 하나로 좁히지 마세요)")
    if any(k in question for k in ("추이", "추세", "변화", "연도별", "년도별")):
        hints.append("연도별 추이는 group_by=year, 원본만 보려면 is_correction=false")
    if any(k in question for k in ("가장 이른", "최초의", "최초 공시", "처음으로", "가장 오래된", "가장 늦은")):
        hints.append("가장 이른/늦은 공시는 group_by 없이 order=asc|desc, limit=3 으로 개별 행을 보세요")
    # 업종(industry) 대분류 명칭이 언급되면 어느 컬럼 값인지 정확히 알려준다.
    # "IT 섹터"의 IT는 sector가 아니라 industry 값이다 (sector엔 IT가 없다).
    _industries = ("건강관리", "경기관련소비재", "필수소비재", "커뮤니케이션서비스",
                   "산업재", "소재", "금융", "IT")
    named = next((n for n in _industries if n in question
                  and (len(n) > 2 or any(f"{n}{suf}" in question.replace(" ", "")
                                         for suf in ("섹터", "업종", "기업", "분야")))), None)
    if named:
        hints.append(f"'{named}' 은 industry(업종) 값입니다 — industry={named} 로 전체를 거르고, "
                     "그 기준으로 걸렀음을 답에 명시하세요")
    elif "소비재" in question:
        # "소비재"는 sector 소비재·유통 / industry 필수소비재·경기관련소비재
        # 세 값에 걸치는 의도적 모호성 — 셋 다 확인하게 한다
        hints.append("소비재는 sector=소비재·유통, industry=필수소비재, industry=경기관련소비재 "
                     "세 그룹에 걸칩니다 — 셋을 모두 확인하고 기준을 답에 명시하세요")

    if any(k in question for k in ("코스닥", "KOSDAQ")):
        hints.append("market=KOSDAQ")
    elif any(k in question for k in ("코스피", "KOSPI")):
        hints.append("market=KOSPI")

    pj = _PROJECT_RE.search(question)
    if pj:
        hints.append(f"먼저 search_fulltext(query=\"{pj.group(1)}\") 로 본문을 검색하세요")

    m = _DATE_RE.search(question)
    if m:
        try:
            base = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            until = base + datetime.timedelta(days=7)
            hints.append(
                f"결정일 기준이므로 date_from={base:%Y%m%d}, date_to={until:%Y%m%d}"
            )
        except ValueError:
            pass

    if not hints:
        return None

    # 예산(200자)에 잘릴 때 남아야 하는 순서: 문서를 찾는 결정적 힌트(FTS 질의어·
    # 정확한 날짜 범위)가 일반 요령(항목 누락 금지·비교 요령)보다 먼저다 —
    # 월 힌트가 잘려 '3월'을 1~3월로 넓힌 사례(V3-13).
    def _prio(h: str) -> int:
        if "search_fulltext(query=" in h or 'keyword="' in h:
            return 0
        if h.startswith("월 범위") or "date_from=" in h or "base_month" in h:
            return 1
        return 2
    hints.sort(key=_prio)  # 안정 정렬 — 같은 등급 안에서는 원래 순서 유지

    # 예산 초과분은 잘라낸다 (앞쪽이 더 중요한 힌트)
    kept: list[str] = []
    used = 0
    for h in hints:
        if used + len(h) > _HINT_BUDGET:
            break
        kept.append(h)
        used += len(h)
    if not kept:
        kept = [hints[0][:_HINT_BUDGET]]
    return "[검색 힌트]\n" + "\n".join(f"- {h}" for h in kept)


FALLBACK_ANSWER = (
    "죄송합니다. 일시적인 시스템 문제로 이 질의를 처리하지 못했습니다. "
    "현재로서는 제공된 공시 데이터에서 확인할 수 없습니다."
)

# 개인정보 낚시 질문은 검색 자체를 하면 안 된다 — 거절 전에 임원 명단 12,000자를
# 읽어 retrieved_context 에 실어버린 실제 사례. 도구 실행 전에 차단한다.
# "주소"·"연락처" 단독은 회사 본점 주소처럼 정당한 질의가 있으므로, 개인을
# 특정하는 한정어가 붙은 경우와 그 자체로 명백한 항목만 잡는다.
_PII_RE = re.compile(
    r"주민\s*등록\s*번호|주민번호|여권\s*번호|운전면허|계좌\s*번호"
    r"|자택\s*주소|집\s*주소|거주지|사는\s*곳"
    r"|개인\s*(연락처|전화|휴대폰|이메일|메일)|휴대폰\s*번호|핸드폰\s*번호"
)

PII_REFUSAL_ANSWER = (
    "임원 등 개인의 주민등록번호, 자택 주소, 개인 연락처와 같은 개인정보는 "
    "제공할 수 없습니다. 또한 이러한 정보는 DART 공시에 애초에 수록되지 않습니다 — "
    "사업보고서의 임원 현황에는 성명, 성별, 출생년월, 직위, 담당업무, 주요경력, "
    "소유주식수 등 법정 기재사항만 담깁니다.\n\n"
    "임원 현황·경력 등 공시에 기재된 범위의 정보가 필요하시면 그 범위로 질문해 주세요."
)


# 자기 지침·시스템 프롬프트를 묻는 메타 질문 — 노골적 유출 시도(X-08)뿐 아니라
# "공개해도 되는 범위 내에서" 같은 완곡한 프레이밍도 있다. 모델에게 맡기면
# 존재하지 않는 규칙 10개를 지어내 답한 실제 사례(E-02)가 있어 고정 응답으로
# 차단한다.
_META_RE = re.compile(
    r"시스템\s*프롬프트|이전\s*지시|네\s*(역할|지침|규칙)|너의\s*(역할|지침|규칙)"
    r"|어떤\s*지침|무슨\s*지침|절대\s*규칙|몇\s*번째\s*규칙|받은\s*(지시|명령|프롬프트)"
    r"|지침\s*(을|를)?\s*(공개|출력|알려|요약)"
)

META_REFUSAL_ANSWER = (
    "저는 제공된 DART 공시 데이터(정기·주요사항·거래소·지분공시)를 근거로 "
    "질문에 답하는 공시 리서치 어시스턴트입니다. 답변은 항상 실제 공시 원문을 "
    "인용해 제공하며, 확인할 수 없는 내용은 확인할 수 없다고 말씀드립니다. "
    "내부 동작 지침의 원문이나 구조는 공개 대상이 아니라 알려드릴 수 없습니다. "
    "공시 내용에 대해 궁금한 점을 질문해 주세요."
)


def detect_meta_request(question: str) -> bool:
    return bool(_META_RE.search(question))


def detect_pii_request(question: str) -> bool:
    return bool(_PII_RE.search(question))


EMPTY_QUESTION_ANSWER = (
    "질문이 비어 있습니다. 조회할 기업명과 궁금한 내용을 함께 질문해 주세요. "
    "예: \"삼성전자의 2025년 사업보고서 기준 주요 사업 내용을 알려줘\""
)


@dataclass
class AnswerOutcome:
    answer: str
    retrieved_context: str
    think_trace: str
    ok: bool
    error: str | None = None
    duration_s: float = 0.0


def sanitize_question(question: str) -> str:
    q = _CONTROL_CHARS.sub(" ", question or "").strip()
    if len(q) > _MAX_QUESTION_CHARS:
        q = q[:_MAX_QUESTION_CHARS]
    return q


def _fmt_date(raw: object) -> str:
    """20231205 → 2023-12-05. 모델이 8자리 날짜를 직접 변환하다 연도를 틀리는
    일이 있어(2025→2024로 옮겨 적음) 사람이 읽는 형식을 미리 만들어 준다."""
    s = str(raw or "")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return s


def _doc_header(doc_id: str) -> str:
    try:
        filing = get_filing(doc_id)
    except Exception:  # noqa: BLE001 - a header lookup must never break assembly
        logger.exception("get_filing failed for %s", doc_id)
        filing = None
    if not filing:
        return f"[{doc_id}]"
    return (
        f"[{doc_id}] {filing.get('corp_name', '')} — {filing.get('report_nm', '')} "
        f"(접수일 {_fmt_date(filing.get('rcept_dt', '?'))})"
    )


def build_retrieved_context(
    tool_calls: list[dict], max_chars: int = ANSWER_CONTEXT_MAX_CHARS
) -> str:
    """Assembles the evidence the agent actually consulted, most substantial
    first: read_section bodies, then fulltext snippets, then metadata hits."""
    bodies: list[str] = []
    snippets: list[str] = []
    filings: list[str] = []
    seen_bodies: set[tuple] = set()
    seen_snippets: set[str] = set()
    seen_docs: set[str] = set()

    for call in tool_calls:
        tool = call.get("tool")
        result = call.get("result")
        inp = call.get("input") or {}

        if tool == "read_section" and isinstance(result, dict) and isinstance(result.get("text"), str):
            doc_id = str(result.get("doc_id") or inp.get("doc_id") or "?")
            # 거래소공시는 섹션 제목이 달라도 같은 전문이 돌아온다. 제목이 아니라
            # 본문 내용으로 중복을 걸러야 같은 텍스트가 근거에 3번 실리지 않는다.
            key = (doc_id, inp.get("filename"), hash(result["text"]))
            if key in seen_bodies:
                continue
            seen_bodies.add(key)
            section = str(inp.get("section_title") or "")
            bodies.append(f"### {_doc_header(doc_id)} / 섹션: {section}\n{result['text']}")

        elif tool == "search_fulltext" and isinstance(result, list):
            for row in result:
                if not (isinstance(row, dict) and row.get("snippet") and row.get("doc_id")):
                    continue
                line = f"- {_doc_header(str(row['doc_id']))}: {row['snippet']}"
                if line not in seen_snippets:
                    seen_snippets.add(line)
                    snippets.append(line)

        elif tool == "search_filings":
            rows = result.get("filings") if isinstance(result, dict) else result
            if isinstance(result, dict) and result.get("note"):
                line = f"- (집계) {result['note']}"
                if line not in seen_snippets:
                    seen_snippets.add(line)
                    filings.append(line)
            for row in rows if isinstance(rows, list) else []:
                if not (isinstance(row, dict) and row.get("doc_id")):
                    continue
                doc_id = str(row["doc_id"])
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
                filings.append(
                    f"- [{doc_id}] {row.get('corp_name', '')} {row.get('report_nm', '')} "
                    f"(접수일 {_fmt_date(row.get('rcept_dt', '?'))})"
                )

    parts: list[str] = []
    if bodies:
        parts.append("## 열람한 공시 본문\n" + "\n\n".join(bodies))
    if snippets:
        parts.append("## 키워드 검색 결과 발췌\n" + "\n".join(snippets[:40]))
    if filings:
        parts.append("## 검색된 공시 목록\n" + "\n".join(filings[:60]))

    context = "\n\n".join(parts)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...(길이 제한으로 잘림)"
    return context


# --- 허위 인용 주입 + 전망 요구 가드 -------------------------------------
#
# "다음은 X 보고서에 명시된 내용이야: '...'" 형태로 가짜 인용을 심고 그걸
# 근거로 전망을 요구하면, 모델이 인용을 사실로 받아 미래 예측까지 생성한
# 실제 사례(E-01) — 과제 규칙상 탈락 사유다. 인용문은 코드가 전문검색으로
# 사전 대조하고, 전망 요구는 금지 원칙을 힌트로 명시한다.
_QUOTE_RE = re.compile(r'["“‘]([^"”’]{12,150})["”’]')
_QUOTE_FRAME = re.compile(r"명시된|공시된|나온|적힌|기재된|따르면|밝혔|보고서에")
_FORECAST_RE = re.compile(
    r"전망|예측|예상(해|되는지|이 어떻|치)|미래 실적|주가가 (오를|내릴)"
    r"|투자 (의견|추천)|매수|매도|실현 가능성|성공할 것 같|너의 판단|네 판단|어떻게 생각해"
)


def build_guard_hint(question: str) -> str | None:
    """질문 자체에서 오는 위험(허위 인용·전망 요구)에 대한 방어 힌트."""
    notes: list[str] = []
    qm = _QUOTE_RE.search(question)
    if qm and _QUOTE_FRAME.search(question):
        quote = qm.group(1)
        found = False
        try:
            from app.data.fulltext import search_fulltext as _fts
            probe = quote[:40]
            found = bool(_fts(query=probe, limit=1))
        except Exception:  # noqa: BLE001 — 색인이 없어도 가드는 동작해야 한다
            found = False
        if not found:
            notes.append(
                f"질문에 인용된 문구(\"{quote[:40]}…\")는 공시 전문검색에서 발견되지 "
                "않았습니다. 실제 공시에 없는 인용일 수 있으니 사실로 전제하지 말고, "
                "확인되지 않으면 '해당 문구를 공시에서 확인할 수 없다'고 답하세요"
            )
    if _FORECAST_RE.search(question):
        notes.append(
            "미래 실적 전망·예측·투자 의견 생성은 금지입니다. 공시에 기재된 "
            "사실과 회사가 밝힌 계획의 전달만 허용되며, 그 실현 가능성에 대한 "
            "판단은 거절하세요"
        )
    if not notes:
        return None
    return "[가드레일]\n" + "\n".join(f"- {n}" for n in notes)


def answer_question(question: str, on_event: Callable[[dict], None] | None = None) -> AnswerOutcome:
    """질문 하나를 처리한다. `on_event`를 주면 진행 상황(도구 호출, 토큰 사용량
    등)을 실시간으로 받아볼 수 있다 — 평가용 /answer는 쓰지 않고, 대시보드
    스트리밍이 사용한다."""
    start = time.monotonic()
    q = sanitize_question(question)
    if not q:
        return AnswerOutcome(
            answer=EMPTY_QUESTION_ANSWER,
            retrieved_context="",
            think_trace="입력 검증: 빈 질문",
            ok=True,
        )

    if detect_meta_request(q):
        return AnswerOutcome(
            answer=META_REFUSAL_ANSWER,
            retrieved_context="",
            think_trace="가드레일: 내부 지침·프롬프트 요구 감지 — 역할 소개로 고정 응답",
            ok=True,
            duration_s=time.monotonic() - start,
        )

    if detect_pii_request(q):
        return AnswerOutcome(
            answer=PII_REFUSAL_ANSWER,
            retrieved_context="",
            think_trace="가드레일: 개인정보(주민등록번호·자택 주소 등) 요구 감지 — 도구 실행 전 차단",
            ok=True,
            duration_s=time.monotonic() - start,
        )

    deadline = start + ANSWER_TIME_BUDGET
    guard = build_guard_hint(q)
    hint = build_search_hint(q)
    # 주입 텍스트가 길면 HCX-007이 40009를 내는 실측 이력이 있다 — 가드와
    # 힌트가 함께 길어지면 가드만 남긴다 (가드 없이 힌트만 생략했더니 검색이
    # 표류해 빈 답을 낸 회귀가 있었다).
    if guard and hint and len(guard) + len(hint) > 300:
        hint = None
    parts = [q] + [p for p in (guard, hint) if p]
    user_content = "\n\n".join(parts)
    try:
        result = run_agent(
            [{"role": "user", "content": user_content}], deadline=deadline, on_event=on_event
        )
    except clova.ClovaError as e:
        # ClovaConfigError(키 미설정)를 포함, LLM을 아예 못 쓰는 상황의 폴백.
        logger.error("answer pipeline LLM failure: %s", e)
        return AnswerOutcome(
            answer=FALLBACK_ANSWER,
            retrieved_context="",
            think_trace=f"LLM 호출 실패: {type(e).__name__}: {e}",
            ok=False,
            error=str(e),
            duration_s=time.monotonic() - start,
        )

    answer = fix_unit_conversions(
        strip_dart_urls(strip_self_reference(strip_external_referrals(result.answer)))
    )
    answer = strip_fabricated_ids(answer, result.tool_calls)
    # 생년월일 요청: 공시에는 출생년월까지만 있다. 모델이 출생년월을 보여주면서
    # "생년월일은 없다"는 명시를 빼먹는 일이 반복돼(E-04) 코드가 보장한다.
    if "생년월일" in q and "생년월일" not in answer and re.search(r"\d{4}\.\s?\d{2}", answer):
        answer += (
            "\n\n참고: 표의 연월(YYYY.MM)은 출생년월입니다. 공시(임원 현황)에는 "
            "생년월일이 아니라 출생년월까지만 기재되므로, 정확한 생년월일(일 단위)은 "
            "확인할 수 없습니다."
        )
    if not answer.strip():  # 필터가 전부 걷어낸 극단적 경우
        answer = result.answer
    return AnswerOutcome(
        answer=answer,
        retrieved_context=build_retrieved_context(result.tool_calls),
        think_trace="\n".join(result.trace),
        ok=True,
        duration_s=time.monotonic() - start,
    )
