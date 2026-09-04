"""SQLite FTS5 keyword search over cached filing text. Complements
search_filings (structured metadata) for queries that need to look inside
the document content, e.g. "which filings mention X counterparty"."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.config import FTS_DB_PATH
from app.data.manifest import resolve_company

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS filing_text USING fts5(
    doc_id UNINDEXED,
    filename UNINDEXED,
    corp_name UNINDEXED,
    sector UNINDEXED,
    doc_group UNINDEXED,
    doc_subtype UNINDEXED,
    rcept_dt UNINDEXED,
    report_nm UNINDEXED,
    content,
    tokenize='trigram'
)
"""
# Korean is agglutinative and unsegmented (no spaces between morphemes), so
# the default unicode61 tokenizer's exact-token matching misses most
# sub-word queries (e.g. "위탁생산" wouldn't match a "위탁생산계약" token).
# trigram indexes 3-character sequences instead, giving substring search
# that works regardless of word boundaries. Queries need len >= 3 chars.


def connect() -> sqlite3.Connection:
    FTS_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(FTS_DB_PATH)
    con.execute(_SCHEMA)
    return con


def index_filing(con: sqlite3.Connection, meta: dict[str, Any], filename: str, text: str) -> None:
    con.execute(
        "DELETE FROM filing_text WHERE doc_id = ? AND filename = ?", [meta["doc_id"], filename]
    )
    con.execute(
        """
        INSERT INTO filing_text
            (doc_id, filename, corp_name, sector, doc_group, doc_subtype, rcept_dt, report_nm, content)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            meta["doc_id"],
            filename,
            meta["corp_name"],
            meta["sector"],
            meta["doc_group"],
            meta["doc_subtype"],
            meta["rcept_dt"],
            meta["report_nm"],
            text,
        ],
    )


def is_index_built() -> bool:
    if not FTS_DB_PATH.exists():
        return False
    con = connect()
    try:
        (count,) = con.execute("SELECT count(*) FROM filing_text").fetchone()
        return count > 0
    finally:
        con.close()


def search_fulltext(
    query: str,
    company: str | None = None,
    sector: str | None = None,
    doc_group: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    con = connect()
    try:
        # "Kakao piccoma Corp." 의 마침표가 fts5 문법 오류를 일으킨 실제 사례 —
        # 토큰을 따옴표로 감싸 리터럴 문자열 매치로 강제한다
        safe_query = " ".join(
            f'"{t}"' for t in query.replace('"', " ").split() if t
        ) or '""'
        clauses = ["filing_text MATCH ?"]
        params: list[Any] = [safe_query]

        if company:
            matches = resolve_company(company)
            if not matches:
                return []
            names = [m["corp_name"] for m in matches]
            placeholders = ",".join(["?"] * len(names))
            clauses.append(f"corp_name IN ({placeholders})")
            params.extend(names)

        if sector:
            # 모델이 "바이오·제약"을 "바이오"로 줄여 쓰는 일이 잦다.
            # ⚠ FTS5(trigram) 가상 테이블에서 `sector LIKE ?`는 UNINDEXED 열에 대해
            # 항상 0건이다(FTS5가 LIKE를 가로챔) — '항공우주' 필터로 실재하는 해지
            # 공시를 못 찾아 확인 불가로 끝난 실제 사례(V1-M-11). instr()로 거른다.
            clauses.append("instr(sector, ?) > 0")
            params.append(sector.strip())

        if doc_group:
            clauses.append("doc_group = ?")
            params.append(doc_group)

        if date_from:
            clauses.append("rcept_dt >= ?")
            params.append(date_from)

        if date_to:
            clauses.append("rcept_dt <= ?")
            params.append(date_to)

        sql = f"""
            SELECT doc_id, filename, corp_name, sector, doc_group, report_nm, rcept_dt,
                   snippet(filing_text, -1, '[', ']', ' ... ', 16) AS snippet
            FROM filing_text
            WHERE {' AND '.join(clauses)}
            ORDER BY rank
            LIMIT ?
        """
        params.append(limit)
        con.row_factory = sqlite3.Row
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()
