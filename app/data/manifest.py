"""Loads corpus/manifest.jsonl + corpus/universe.csv into DuckDB and exposes
metadata search over the DART filing corpus."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import duckdb
import pandas as pd

from app.config import MANIFEST_PATH, UNIVERSE_PATH

_STR_DTYPES = {"corp_code": str, "stock_code": str, "rcept_no": str, "rcept_dt": str}


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
    """DataFrame → dict 리스트. 결측(NaN/NaT)은 None으로 바꾼다.

    manifest에는 실제로 결측이 있다(예: major 공시는 doc_subtype이 없음).
    NaN을 그대로 흘리면 (1) 응답 스키마 검증이 500으로 터지고
    (2) json.dumps가 표준 JSON이 아닌 `NaN` 리터럴을 만들어 LLM 쪽 파싱이
    깨진다. 경계면인 여기서 한 번에 정규화한다."""
    if df.empty:
        return []
    return df.astype(object).where(pd.notna(df), None).to_dict(orient="records")


@dataclass(frozen=True)
class Corpus:
    con: duckdb.DuckDBPyConnection
    universe: pd.DataFrame
    manifest: pd.DataFrame


@lru_cache(maxsize=1)
def load_corpus() -> Corpus:
    universe = pd.read_csv(UNIVERSE_PATH, dtype=_STR_DTYPES)
    manifest = pd.read_json(MANIFEST_PATH, lines=True, dtype=_STR_DTYPES)

    # 시장구분(KOSPI/KOSDAQ) 질문에 대비해 유니버스의 market을 결합해 둔다
    manifest = manifest.merge(universe[["corp_code", "market"]], on="corp_code", how="left")

    con = duckdb.connect(database=":memory:")
    con.register("universe", universe)
    con.register("manifest", manifest)
    return Corpus(con=con, universe=universe, manifest=manifest)


def resolve_company(query: str) -> list[dict[str, Any]]:
    """Resolve a free-text company reference (corp_name, listed_name, or
    stock_code) to universe rows. Tries exact match first, then substring."""
    corpus = load_corpus()
    q = query.strip()

    exact = corpus.con.execute(
        """
        SELECT corp_code, stock_code, corp_name, listed_name, sector, industry, market_cap
        FROM universe
        WHERE lower(corp_name) = lower(?)
           OR lower(listed_name) = lower(?)
           OR stock_code = ?
        """,
        [q, q, q],
    ).fetchdf()
    if not exact.empty:
        return _records(exact)

    fuzzy = corpus.con.execute(
        """
        SELECT corp_code, stock_code, corp_name, listed_name, sector, industry, market_cap
        FROM universe
        WHERE lower(corp_name) LIKE lower(?)
           OR lower(listed_name) LIKE lower(?)
        """,
        [f"%{q}%", f"%{q}%"],
    ).fetchdf()
    return _records(fuzzy)


def _norm(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def resolve_sector(query: str) -> dict[str, Any] | None:
    """자유 텍스트에서 섹터/업종을 찾아 소속 기업 목록을 돌려준다.

    모델이 get_company_info에 "방산·항공우주 4개사"처럼 섹터를 넣는 일이
    실제로 있다 — 기업이 아니라고 오류를 내는 대신, 원하는 것(소속 기업들)을
    준다. 가운뎃점·공백 표기 차이는 정규화로 흡수한다."""
    corpus = load_corpus()
    q = _norm(query)
    if not q:
        return None

    for column, kind in (("sector", "섹터"), ("industry", "업종")):
        for name in corpus.universe[column].unique():
            n = _norm(str(name))
            if n and (n in q or q in n):
                rows = corpus.universe[corpus.universe[column] == name]
                return {
                    "matched": name,
                    "kind": kind,
                    "count": len(rows),
                    "companies": _records(
                        rows[["corp_code", "stock_code", "corp_name", "listed_name",
                              "sector", "industry", "market", "market_cap"]]
                    ),
                }
    return None


def search_filings(
    company: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    doc_group: str | None = None,
    doc_subtype: str | None = None,
    filer: str | None = None,
    market: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    base_year: int | None = None,
    base_month: int | None = None,
    is_correction: bool | None = None,
    keyword: str | None = None,
    order: str = "desc",
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Filter manifest rows by structured metadata.

    - company: free text resolved via resolve_company (corp_name/listed_name/stock_code)
    - date_from/date_to: YYYYMMDD, filtered on rcept_dt (제출된 날짜)
    - base_year/base_month: 정기공시가 **다루는 회계기간**. 제출일과 다르다 —
      FY2025 사업보고서는 2026-03에 제출되므로, "2025년 실적"을 찾을 때
      date_from/date_to를 쓰면 FY2024 보고서가 잡힌다. 회계연도로 찾을 때는
      반드시 base_year를 쓸 것.
    - filer: 제출인(flr_nm) 부분일치. 지분공시는 발행회사(corp_name) 기준으로
      저장되므로 "누가 보고했나"는 이걸로 찾는다 — 지분공시의 88%는 제출인이
      유니버스 밖 주체(국민연금, 대주주 등)다.
    - keyword: substring match on report_nm (not full-text search over content)
    """
    corpus = load_corpus()
    clauses: list[str] = []
    params: list[Any] = []

    if company:
        matches = resolve_company(company)
        if not matches:
            return []
        codes = [m["corp_code"] for m in matches]
        placeholders = ",".join(["?"] * len(codes))
        clauses.append(f"corp_code IN ({placeholders})")
        params.extend(codes)

    if sector:
        # 모델이 "방산·항공우주"를 "방산"으로 줄여 쓰는 일이 잦아 부분일치로 받는다
        clauses.append("sector ILIKE ?")
        params.append(f"%{sector.strip()}%")

    if industry:
        clauses.append("industry ILIKE ?")
        params.append(f"%{industry.strip()}%")

    if doc_group:
        clauses.append("doc_group = ?")
        params.append(doc_group)

    if doc_subtype:
        clauses.append("doc_subtype = ?")
        params.append(doc_subtype)

    if filer:
        clauses.append("flr_nm ILIKE ?")
        params.append(f"%{filer.strip()}%")

    if market:
        m = market.strip().upper()
        clauses.append("upper(market) = ?")
        params.append(m)

    if date_from:
        clauses.append("rcept_dt >= ?")
        params.append(date_from)

    if date_to:
        clauses.append("rcept_dt <= ?")
        params.append(date_to)

    if base_year is not None:
        clauses.append("base_year = ?")
        params.append(base_year)

    if base_month is not None:
        clauses.append("base_month = ?")
        params.append(base_month)

    if is_correction is not None:
        clauses.append("is_correction = ?")
        params.append(is_correction)

    if keyword:
        clauses.append("report_nm ILIKE ?")
        params.append(f"%{keyword}%")

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    # 정렬 방향은 값 바인딩이 안 되므로 화이트리스트로만 끼워 넣는다
    direction = "ASC" if str(order).strip().lower() == "asc" else "DESC"
    query = f"""
        SELECT doc_id, corp_name, listed_name, sector, market, doc_group, doc_subtype,
               report_nm, flr_nm, rcept_no, rcept_dt,
               CAST(base_year AS INTEGER) AS base_year,
               CAST(base_month AS INTEGER) AS base_month,
               is_correction, file_path, file_format, n_files
        FROM manifest
        {where}
        ORDER BY rcept_dt {direction}
        LIMIT ?
    """
    params.append(limit)
    return _records(corpus.con.execute(query, params).fetchdf())


def get_filing_by_rcept_no(rcept_no: str) -> dict[str, Any] | None:
    corpus = load_corpus()
    df = corpus.con.execute(
        "SELECT * FROM manifest WHERE rcept_no = ? LIMIT 1", [rcept_no]
    ).fetchdf()
    rows = _records(df)
    return rows[0] if rows else None


def get_filing(doc_id: str) -> dict[str, Any] | None:
    corpus = load_corpus()
    df = corpus.con.execute(
        "SELECT * FROM manifest WHERE doc_id = ? LIMIT 1", [doc_id]
    ).fetchdf()
    rows = _records(df)
    return rows[0] if rows else None
