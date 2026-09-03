#!/usr/bin/env python3
"""EVAL_ALL.md의 전 문항을 순차 실행하고 결과를 JSONL로 남긴다.

    uv run python scripts/run_eval_all.py            # 전체 (이미 한 것은 건너뜀)
    uv run python scripts/run_eval_all.py --only V3  # 특정 세트만
    uv run python scripts/run_eval_all.py --redo     # 처음부터 다시

평가용 GET /answer 를 그대로 태우므로 대시보드 질의기록에도 남는다.
중단해도 이어서 실행할 수 있게 매 문항 직후 결과를 append 한다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
ITEMS_PATH = ROOT / "logs" / "eval_all_items.json"
OUT_PATH = ROOT / "logs" / "eval_all_results.jsonl"


def parse_items() -> list[dict]:
    """EVAL_ALL.md에서 문항 ID·질문·정답(gold)을 뽑는다."""
    doc = (ROOT / "docs" / "EVAL_ALL.md").read_text(encoding="utf-8")
    items: list[dict] = []
    for block in re.split(r"\n(?=### [A-Z]+[0-9]*-)", doc):
        m = re.match(r"### ([A-Z]+[0-9]*-[A-Z0-9-]+)\s*·?\s*(.*)", block)
        if not m:
            continue
        qm = re.search(r"\*\*질문\*\*:\s*(.+?)(?=\n\*\*|\n\n|\Z)", block, re.S)
        if not qm:
            continue
        gold = []
        for key in ("정답", "정답 방향", "검증 결과", "근거", "주의", "겨냥 지점", "채점", "함정"):
            km = re.search(r"\*\*" + key + r"\*\*:?\s*(.+?)(?=\n\*\*|\Z)", block, re.S)
            if km:
                gold.append(f"[{key}] " + " ".join(km.group(1).split())[:600])
        items.append({
            "id": m.group(1),
            "title": m.group(2).strip(),
            "question": " ".join(qm.group(1).split()),
            "gold": "\n".join(gold),
        })
    return items


def done_ids() -> set[str]:
    if not OUT_PATH.exists():
        return set()
    ids = set()
    for line in OUT_PATH.read_text(encoding="utf-8").splitlines():
        try:
            ids.add(json.loads(line)["id"])
        except Exception:  # noqa: BLE001
            continue
    return ids


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", help="세트 접두어 필터 (V1/V2/ADV/V3/V4)")
    ap.add_argument("--redo", action="store_true", help="기존 결과를 지우고 다시 실행")
    ap.add_argument("--ids", help="쉼표로 나열한 문항만 다시 실행 (기존 기록은 교체)")
    args = ap.parse_args()

    items = parse_items()
    ITEMS_PATH.parent.mkdir(parents=True, exist_ok=True)
    ITEMS_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=1), encoding="utf-8")

    if args.redo and OUT_PATH.exists():
        OUT_PATH.unlink()
    redo_ids = {x.strip() for x in (args.ids or "").split(",") if x.strip()}
    if redo_ids and OUT_PATH.exists():
        kept = [line for line in OUT_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip() and json.loads(line)["id"] not in redo_ids]
        OUT_PATH.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    finished = done_ids()

    targets = [
        it for it in items
        if (not args.only or it["id"].startswith(args.only))
        and (not redo_ids or it["id"] in redo_ids)
        and it["id"] not in finished
    ]
    print(f"[eval-all] 전체 {len(items)}문항 · 완료 {len(finished)} · 이번 실행 {len(targets)}",
          flush=True)

    from fastapi.testclient import TestClient  # noqa: E402  (앱 로딩이 느려 지연 임포트)

    from app.main import app  # noqa: E402

    started_all = time.monotonic()
    with TestClient(app) as client:
        for i, it in enumerate(targets, 1):
            t0 = time.monotonic()
            try:
                res = client.get(
                    "/answer",
                    params={"question_id": it["id"], "question": it["question"]},
                    timeout=400,
                )
                data = res.json()
                status = res.status_code
            except Exception as e:  # noqa: BLE001 — 배치가 한 건 때문에 죽으면 안 된다
                data = {"answer": f"(실행 실패: {type(e).__name__}: {e})",
                        "think_trace": "", "retrieved_context": ""}
                status = 0
            elapsed = round(time.monotonic() - t0, 1)
            record = {
                "id": it["id"],
                "title": it["title"],
                "question": it["question"],
                "gold": it["gold"],
                "status": status,
                "seconds": elapsed,
                "answer": data.get("answer", ""),
                "think_trace": data.get("think_trace", ""),
                "context_chars": len(data.get("retrieved_context", "")),
                "tool_calls": data.get("think_trace", "").count("도구 호출:"),
            }
            with OUT_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            print(f"[{i}/{len(targets)}] {it['id']:<10} {elapsed:>6.1f}s "
                  f"도구{record['tool_calls']:>3} · {record['answer'][:60]}", flush=True)

    print(f"[eval-all] 완료 — 총 {round(time.monotonic() - started_all)}초", flush=True)


if __name__ == "__main__":
    main()
