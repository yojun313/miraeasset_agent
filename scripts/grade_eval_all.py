#!/usr/bin/env python3
"""실행 결과에 자동 채점 신호를 붙여 사람이 판정하기 쉽게 요약한다.

    uv run python scripts/grade_eval_all.py            # 요약 표 출력
    uv run python scripts/grade_eval_all.py --detail V3-01

자동 신호는 '참고용'이다 — 최종 판정은 사람이 gold와 대조해서 한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "logs" / "eval_all_results.jsonl"

_NUM = re.compile(r"\d[\d,]{2,}")          # 3자리 이상 숫자 (쉼표 포함)
_DOCID = re.compile(r"(?:major|periodic|exchange|holding)_\d{14}")
_PCT = re.compile(r"\d+\.\d+\s*%|\d+\s*%")
_REFUSAL = re.compile(r"확인할 수 없|확인되지 않|수집되어 있지 않|제공할 수 없|존재하지 않|해당 없|없습니다")


def norm_nums(text: str) -> set[str]:
    """숫자를 쉼표 제거해 비교 가능한 형태로."""
    return {m.group(0).replace(",", "") for m in _NUM.finditer(text or "")}


def load() -> list[dict]:
    if not RESULTS.exists():
        sys.exit("결과 파일이 없습니다. run_eval_all.py를 먼저 실행하세요.")
    out = []
    for line in RESULTS.read_text(encoding="utf-8").splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def signals(rec: dict) -> dict:
    """gold 대비 답변의 자동 신호."""
    gold, ans = rec.get("gold", ""), rec.get("answer", "")
    gold_nums, ans_nums = norm_nums(gold), norm_nums(ans)
    # 4자리 이상 숫자만 비교 대상 (연도·소수 건수는 노이즈)
    key_nums = {n for n in gold_nums if len(n) >= 4}
    hit_nums = key_nums & ans_nums
    gold_docs = set(_DOCID.findall(gold))
    ans_docs = set(_DOCID.findall(ans))
    gold_pcts = {p.replace(" ", "") for p in _PCT.findall(gold)}
    ans_pcts = {p.replace(" ", "") for p in _PCT.findall(ans)}
    return {
        "num_hit": len(hit_nums),
        "num_total": len(key_nums),
        "num_missing": sorted(key_nums - ans_nums)[:4],
        "doc_hit": len(gold_docs & ans_docs),
        "doc_total": len(gold_docs),
        "pct_hit": len(gold_pcts & ans_pcts),
        "pct_total": len(gold_pcts),
        "cited": bool(ans_docs) or bool(re.search(r"(?<!\d)20\d{12}(?!\d)", ans)),
        "refusal": bool(_REFUSAL.search(ans)),
        "len": len(ans),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--detail", help="한 문항의 전체 내용 출력")
    ap.add_argument("--set", help="세트 필터")
    args = ap.parse_args()
    recs = load()

    if args.detail:
        for r in recs:
            if r["id"] == args.detail:
                print("=" * 70)
                print("ID       :", r["id"], "|", r["title"])
                print("질문     :", r["question"])
                print("-" * 70)
                print("GOLD:\n" + r["gold"])
                print("-" * 70)
                print(f"답변 ({r['seconds']}s, 도구 {r['tool_calls']}회):\n" + r["answer"])
                print("-" * 70)
                print("신호:", json.dumps(signals(r), ensure_ascii=False))
                return
        sys.exit(f"{args.detail} 결과가 없습니다")

    print(f"{'ID':<11}{'초':>6}{'도구':>4}  {'수치':>7} {'doc':>5} {'인용':>4} {'거절':>4}  답변 앞부분")
    print("-" * 118)
    for r in recs:
        if args.set and not r["id"].startswith(args.set):
            continue
        s = signals(r)
        print(f"{r['id']:<11}{r['seconds']:>6.0f}{r['tool_calls']:>4}  "
              f"{s['num_hit']:>3}/{s['num_total']:<3} {s['doc_hit']:>2}/{s['doc_total']:<2} "
              f"{'O' if s['cited'] else '-':>4} {'O' if s['refusal'] else '-':>4}  "
              f"{r['answer'][:52].replace(chr(10), ' ')}")
    print(f"\n총 {len(recs)}건")


if __name__ == "__main__":
    main()
