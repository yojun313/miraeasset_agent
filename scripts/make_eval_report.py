#!/usr/bin/env python3
"""전수 실행 결과 + 사람 판정을 합쳐 docs/EVAL_REPORT.md 를 만든다.

    uv run python scripts/make_eval_report.py \
        --results logs/eval_all_results.jsonl --verdicts logs/eval_all_verdicts.json \
        --prev logs/eval_all_verdicts_run1.json --narrative logs/eval_report_narrative.md

통계 표(1장)·문항별 상세(마지막 장)는 자동 생성하고, 원인 분석 등 서술은
--narrative 파일(마크다운)을 그 사이에 그대로 끼워 넣는다.
"""

from __future__ import annotations

import argparse
import json
from collections import OrderedDict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SETS: "OrderedDict[str, str]" = OrderedDict([
    ("V1-E", "V1 🟢 쉬움"), ("V1-M", "V1 🟡 중간"), ("V1-H", "V1 🟠 어려움"),
    ("V1-X", "V1 🔴 매우 어려움"), ("V2", "V2 확장"), ("ADV", "ADV 적대적"),
    ("V3", "V3 초고난도"), ("V4", "V4 미커버 유형"),
])


def set_of(qid: str) -> str:
    for pref in SETS:
        if qid.startswith(pref + "-"):
            return pref
    return "?"


def load_results(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]


def pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "—"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results", default=str(ROOT / "logs/eval_all_results.jsonl"))
    ap.add_argument("--verdicts", default=str(ROOT / "logs/eval_all_verdicts.json"))
    ap.add_argument("--prev", help="이전 실행 판정 json (비교용)")
    ap.add_argument("--narrative", help="서술 장(원인 분석 등) 마크다운")
    ap.add_argument("--out", default=str(ROOT / "docs/EVAL_REPORT.md"))
    ap.add_argument("--run-label", default="2차")
    ap.add_argument("--prev-label", default="1차")
    args = ap.parse_args()

    recs = load_results(Path(args.results))
    verdicts = json.loads(Path(args.verdicts).read_text(encoding="utf-8"))
    prev = json.loads(Path(args.prev).read_text(encoding="utf-8")) if args.prev else {}
    for r in recs:
        v = verdicts.get(r["id"], {})
        r["verdict"], r["note"] = v.get("verdict", "?"), v.get("note", "")
        r["prev"] = prev.get(r["id"], {}).get("verdict", "")

    n = len(recs)
    cnt = {k: sum(1 for r in recs if r["verdict"] == k) for k in "○△✗"}
    pcnt = {k: sum(1 for v in prev.values() if v.get("verdict") == k) for k in "○△✗"}
    total_sec = sum(r["seconds"] for r in recs)
    fails = sum(1 for r in recs if r["status"] != 200)

    out: list[str] = []
    out.append("# 공시 Agent 전수 평가 리포트")
    out.append("")
    out.append(f"> `docs/EVAL_ALL.md` **{n}문항 전체**를 평가용 `GET /answer`로 순차 실행하고 gold와 대조한 결과 ({args.run_label} 실행).")
    out.append(f"> 실행일 {date.today().isoformat()} · 총 소요 {total_sec:,.0f}초({total_sec / 60:.1f}분) · "
               f"평균 {total_sec / n:.1f}초/문항 · 실행 실패 {fails}건")
    out.append("> 재현: `uv run python scripts/run_eval_all.py --redo` → `logs/eval_all_results.jsonl` → 판정 후 `scripts/make_eval_report.py`")
    out.append("")
    out.append("## 1. 종합 결과")
    out.append("")
    if prev:
        out.append(f"| 판정 | {args.prev_label} | {args.run_label} | 변화 | 기준 |")
        out.append("|---|---:|---:|---:|---|")
        crit = {"○": "핵심 값·결론이 gold와 일치하고 근거를 제시",
                "△": "핵심은 맞으나 요구 항목 일부 누락·근거 부실",
                "✗": "값이 틀렸거나, 있는 데이터를 못 찾았거나, 환각"}
        for k, lab in (("○", "○ 정답"), ("△", "△ 부분"), ("✗", "✗ 오답")):
            d = cnt[k] - pcnt[k]
            out.append(f"| {lab} | {pcnt[k]} ({pct(pcnt[k], n)}) | {cnt[k]} ({pct(cnt[k], n)}) | {d:+d} | {crit[k]} |")
        p1, p2 = 100 * pcnt["○"] / n, 100 * cnt["○"] / n
        h1, h2 = 100 * (pcnt["○"] + pcnt["△"] / 2) / n, 100 * (cnt["○"] + cnt["△"] / 2) / n
        out.append(f"| **정답률** | **{p1:.1f}%** | **{p2:.1f}%** | **{p2 - p1:+.1f}%p** | 부분점수(△=0.5) 포함 {h1:.1f}% → {h2:.1f}% |")
    else:
        out.append("| 판정 | 문항 | 비율 |")
        out.append("|---|---:|---:|")
        for k, lab in (("○", "○ 정답"), ("△", "△ 부분"), ("✗", "✗ 오답")):
            out.append(f"| {lab} | {cnt[k]} | {pct(cnt[k], n)} |")
    out.append("")
    out.append("### 세트·난이도별")
    out.append("")
    hdr = "| 세트 | 문항 | ○ | △ | ✗ | 정답률" + (f" | {args.prev_label} 정답률 | 변화" if prev else "") + " | 평균 소요 | 평균 도구 |"
    out.append(hdr)
    out.append("|---|---:|---:|---:|---:|---:|" + ("---:|---:|" if prev else "") + "---:|---:|")
    for pref, label in SETS.items():
        rs = [r for r in recs if set_of(r["id"]) == pref]
        if not rs:
            continue
        c = {k: sum(1 for r in rs if r["verdict"] == k) for k in "○△✗"}
        row = f"| {label} | {len(rs)} | {c['○']} | {c['△']} | {c['✗']} | **{100 * c['○'] / len(rs):.0f}%**"
        if prev:
            pc = sum(1 for r in rs if r["prev"] == "○")
            row += f" | {100 * pc / len(rs):.0f}% | {100 * (c['○'] - pc) / len(rs):+.0f}%p"
        row += f" | {sum(r['seconds'] for r in rs) / len(rs):.0f}s | {sum(r['tool_calls'] for r in rs) / len(rs):.1f} |"
        out.append(row)
    if prev:
        out.append("")
        out.append(f"### {args.prev_label} → {args.run_label} 판정 이동")
        out.append("")
        moves: dict[tuple[str, str], list[str]] = {}
        for r in recs:
            if r["prev"] and r["prev"] != r["verdict"]:
                moves.setdefault((r["prev"], r["verdict"]), []).append(r["id"])
        out.append("| 이동 | 건수 | 문항 |")
        out.append("|---|---:|---|")
        order = [("✗", "○"), ("△", "○"), ("✗", "△"), ("○", "△"), ("△", "✗"), ("○", "✗")]
        for key in order:
            ids = moves.get(key)
            if ids:
                out.append(f"| {key[0]} → {key[1]} | {len(ids)} | {', '.join(ids)} |")
        same = sum(1 for r in recs if r["prev"] == r["verdict"])
        out.append(f"| 유지 | {same} | |")
    out.append("")

    if args.narrative:
        out.append(Path(args.narrative).read_text(encoding="utf-8").rstrip())
        out.append("")

    out.append("## 문항별 상세")
    out.append("")
    for pref, label in SETS.items():
        rs = [r for r in recs if set_of(r["id"]) == pref]
        if not rs:
            continue
        out.append(f"### {label}")
        out.append("")
        out.append("| 문항 | 판정 |" + (f" {args.prev_label} |" if prev else "") + " 초 | 도구 | 비고 |")
        out.append("|---|:--:|" + (":--:|" if prev else "") + "--:|--:|---|")
        for r in rs:
            note = (r["note"] or "—").replace("|", "／")
            out.append(f"| {r['id']} | {r['verdict']} |" + (f" {r['prev'] or '—'} |" if prev else "")
                       + f" {r['seconds']:.0f} | {r['tool_calls']} | {note} |")
        out.append("")
    out.append("---")
    out.append("")
    out.append(f"*생성: `scripts/make_eval_report.py` · 원본 결과 `{Path(args.results).name}` · "
               f"판정 `{Path(args.verdicts).name}`*")
    Path(args.out).write_text("\n".join(out) + "\n", encoding="utf-8")
    print(f"[report] {args.out} — {n}문항, 정답 {cnt['○']} 부분 {cnt['△']} 오답 {cnt['✗']}")


if __name__ == "__main__":
    main()
