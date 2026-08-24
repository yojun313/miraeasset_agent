#!/usr/bin/env python3
"""One-time (or refreshable) full-text index build over the DART filing corpus.

Parses every filing (and its attachments) once, caching clean text to
cache/text/ and indexing it into the SQLite FTS5 database used by
app.data.fulltext.search_fulltext. Safe to re-run: cached text is reused,
existing FTS rows for the same (doc_id, filename) are replaced.

Usage:
    uv run python scripts/build_index.py                          # full corpus
    uv run python scripts/build_index.py --limit 20                # smoke test
    uv run python scripts/build_index.py --company 삼성전자 --doc-group exchange
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.data.cache import UnsupportedFileError, get_parsed, list_available_files  # noqa: E402
from app.data.fulltext import connect, index_filing  # noqa: E402
from app.data.manifest import search_filings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--company", default=None)
    parser.add_argument(
        "--doc-group", default=None, choices=["periodic", "major", "exchange", "holding"]
    )
    parser.add_argument("--limit", type=int, default=100_000)
    args = parser.parse_args()

    filings = search_filings(company=args.company, doc_group=args.doc_group, limit=args.limit)
    print(f"indexing {len(filings)} filings...")

    con = connect()
    start = time.time()
    indexed_files = 0
    skipped_filings = 0

    for i, filing in enumerate(filings, 1):
        doc_id = filing["doc_id"]
        try:
            files = list_available_files(doc_id)
        except UnsupportedFileError:
            skipped_filings += 1
            continue

        for fi in files:
            try:
                doc = get_parsed(doc_id, fi.path.name)
            except Exception as e:  # noqa: BLE001 - keep indexing the rest of the corpus
                print(f"  ! failed to parse {doc_id}/{fi.path.name}: {e}")
                continue
            if not doc.text.strip():
                continue
            index_filing(con, filing, fi.path.name, doc.text)
            indexed_files += 1

        if i % 100 == 0:
            con.commit()
            elapsed = time.time() - start
            print(f"  {i}/{len(filings)} filings ({indexed_files} files indexed, {elapsed:.0f}s elapsed)")

    con.commit()
    con.close()
    elapsed = time.time() - start
    print(
        f"done: {indexed_files} files indexed from {len(filings)} filings "
        f"({skipped_filings} filings skipped, no parseable file), {elapsed:.0f}s"
    )


if __name__ == "__main__":
    main()
