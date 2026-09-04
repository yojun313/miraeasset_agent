from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import duckdb
import pandas as pd
import re

from app.config import MANIFEST_PATH, UNIVERSE_PATH

_STR_DTYPES = {"corp_code": str, "stock_code": str, "rcept_no": str, "rcept_dt": str}


def _records(df: pd.DataFrame) -> list[dict[str, Any]]:
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

    manifest = manifest.merge(universe[["corp_code", "market"]], on="corp_code", how="left")

    base_nm = manifest["report_nm"].str.replace(r"^\[[^\]]*\]", "", regex=True).str.strip()
    paren = base_nm.str.extract(r"주요사항보고서\((.+)\)", expand=False)
    manifest["report_type"] = paren.fillna(manifest["doc_subtype"])

    con = duckdb.connect(database=":memory:")
    con.register("universe", universe)
    con.register("manifest", manifest)
    return Corpus(con=con, universe=universe, manifest=manifest)


def resolve_company(query: str) -> list[dict[str, Any]]:
    """Resolve a free-text company reference (corp_name, listed_name, or
    stock_code) to universe rows. Tries exact match first, then substring."""
    corpus = load_corpus()
    q = query.strip()

    _ABBREV = {
        "삼전": "삼성전자", "하이닉스": "SK하이닉스", "현차": "현대자동차", "현대차": "현대자동차",
        "엔솔": "LG에너지솔루션", "LG엔솔": "LG에너지솔루션", "삼바": "삼성바이오로직스",
        "셀트": "셀트리온", "에코프로": "에코프로비엠", "한화에어로": "한화에어로스페이스",
        "네이버": "NAVER", "KT": "케이티", "엔씨": "NC", "엔씨소프트": "NC", "LIG넥스원": "LIG디펜스앤에어로스페이스",
        "KAI": "한국항공우주", "카이": "한국항공우주", "삼성화재": "삼성화재해상보험", "HMM": "HMM",
    }
    if q in _ABBREV:
        q = _ABBREV[q]

    if "," in q or "、" in q:
        merged: dict[str, dict[str, Any]] = {}
        for part in re.split(r"[,、]", q):
            part = part.strip()
            if len(_norm(part)) < 2:
                continue
            hits = resolve_company(part)
            if 0 < len(hits) <= 3:
                for m in hits:
                    merged[m["corp_code"]] = m
        if merged:
            return list(merged.values())

    exact = corpus.con.execute(
        """
        SELECT corp_code, stock_code, corp_name, listed_name, sector, industry, market_cap
        FROM universe
        WHERE lower(corp_name) = lower(?)
           OR lower(listed_name) = lower(?)
           OR stock_code = ?
           OR lower(corp_eng_name) = lower(?)
        """,
        [q, q, q, q],
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
    if not fuzzy.empty:
        return _records(fuzzy)

    nq = _norm(q).lower()
    if not nq:
        return []
    everyone = _records(corpus.con.execute(
        """
        SELECT corp_code, stock_code, corp_name, listed_name, sector, industry, market_cap,
               corp_eng_name
        FROM universe
        """
    ).fetchdf())
    def _keys(r: dict[str, Any]) -> tuple[str, str, str]:
        return (_norm(str(r["corp_name"] or "")).lower(),
                _norm(str(r["listed_name"] or "")).lower(),
                _norm(str(r.get("corp_eng_name") or "")).lower())
    matched = [r for r in everyone if nq in _keys(r)]
    if matched:
        return matched
    matched = [r for r in everyone
               if any(k and (nq in k or k in nq) for k in _keys(r))]
    if matched:
        return matched
    if len(nq) >= 4:
        import difflib
        by_key = {}
        for r in everyone:
            for k in _keys(r):
                if k:
                    by_key.setdefault(k, r)
        close = difflib.get_close_matches(nq, by_key.keys(), n=3, cutoff=0.74)
        if close:
            best = difflib.SequenceMatcher(None, nq, close[0]).ratio()
            picked = {}
            for c in close:
                if difflib.SequenceMatcher(None, nq, c).ratio() >= best - 1e-9:
                    r = by_key[c]
                    picked[r["corp_code"]] = r
            return list(picked.values())
    return []


def _norm(text: str) -> str:
    return "".join(ch for ch in text if ch.isalnum())


def resolve_sector(query: str) -> dict[str, Any] | None:
    corpus = load_corpus()
    q = _norm(query)
    if not q:
        return None

    for column, kind in (("sector", "섹터"), ("industry", "업종")):
        for name in corpus.universe[column].unique():
            n = _norm(str(name))
            if n and (n in q or q in n):
                rows = corpus.universe[corpus.universe[column] == name]
                other = "industry" if column == "sector" else "sector"
                comp = rows[other].astype(str).value_counts().to_dict()
                return {
                    "matched": name,
                    "kind": kind,
                    "count": len(rows),
                    f"{'업종' if other == 'industry' else '섹터'}_구성": comp,
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
    report_type: str | None = None,
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

    if report_type:
        # "전환사채권발행결정" 정확 표기를 모델이 모르므로 부분일치
        clauses.append("report_type ILIKE ?")
        params.append(f"%{report_type.strip()}%")

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
               report_type, report_nm, flr_nm, rcept_no, rcept_dt,
               CAST(base_year AS INTEGER) AS base_year,
               CAST(base_month AS INTEGER) AS base_month,
               is_correction, file_path, file_format, n_files
        FROM manifest
        {where}
        ORDER BY rcept_dt {direction}, rcept_no {direction}
        LIMIT ?
    """
    params.append(limit)
    return _records(corpus.con.execute(query, params).fetchdf())


_GROUP_COLUMNS = {
    "corp_name": "corp_name",
    "flr_nm": "flr_nm",
    "doc_group": "doc_group",
    "doc_subtype": "doc_subtype",
    "report_type": "report_type",
    "sector": "sector",
    "industry": "industry",
    "year": "substr(rcept_dt, 1, 4)",
}


def aggregate_filings(
    group_by: str,
    *,
    company: str | None = None,
    sector: str | None = None,
    industry: str | None = None,
    doc_group: str | None = None,
    doc_subtype: str | None = None,
    report_type: str | None = None,
    filer: str | None = None,
    market: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    base_year: int | None = None,
    is_correction: bool | None = None,
    keyword: str | None = None,
    min_count: int = 1,
    limit: int = 30,
) -> list[dict[str, Any]]:
    column = _GROUP_COLUMNS.get(group_by)
    if column is None:
        raise ValueError(f"group_by는 {sorted(_GROUP_COLUMNS)} 중 하나여야 합니다")

    corpus = load_corpus()
    clauses: list[str] = []
    params: list[Any] = []

    if company:
        codes = [m["corp_code"] for m in resolve_company(company)]
        if not codes:
            return []
        clauses.append(f"corp_code IN ({','.join(['?'] * len(codes))})")
        params.extend(codes)
    for value, expr in (
        (sector, "sector ILIKE ?"), (industry, "industry ILIKE ?"),
        (filer, "flr_nm ILIKE ?"), (keyword, "report_nm ILIKE ?"),
        (report_type, "report_type ILIKE ?"),
    ):
        if value:
            clauses.append(expr)
            params.append(f"%{str(value).strip()}%")
    for value, expr in (
        (doc_group, "doc_group = ?"), (doc_subtype, "doc_subtype = ?"),
        (date_from, "rcept_dt >= ?"), (date_to, "rcept_dt <= ?"),
        (base_year, "base_year = ?"),
    ):
        if value is not None:
            clauses.append(expr)
            params.append(value)
    if market:
        clauses.append("upper(market) = ?")
        params.append(market.strip().upper())
    if is_correction is not None:
        clauses.append("is_correction = ?")
        params.append(is_correction)

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    query = f"""
        SELECT {column} AS group_key,
               count(*) AS count,
               sum(CASE WHEN is_correction THEN 1 ELSE 0 END) AS correction_count,
               min(rcept_dt) AS first_dt,
               max(rcept_dt) AS last_dt
        FROM manifest
        {where}
        GROUP BY 1
        HAVING count(*) >= ?
        ORDER BY count DESC
        LIMIT ?
    """
    params.extend([min_count, limit])
    rows = _records(corpus.con.execute(query, params).fetchdf())
    for r in rows:
        total = int(r["count"] or 0)
        corr = int(r["correction_count"] or 0)
        r["count"] = total
        r["correction_count"] = corr
        r["original_count"] = total - corr
        r["correction_pct"] = round(100.0 * corr / total, 1) if total else 0.0
    return rows


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
