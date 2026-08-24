"""Resolves manifest doc_ids to files on disk and caches parsed text so a
6MB annual-report XML is only ever parsed once."""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from app.config import CORPUS_DIR, TEXT_CACHE_DIR
from app.data.manifest import get_filing
from app.data.parser import (
    FileInfo,
    ParsedDoc,
    Section,
    list_documents_in_folder,
    parse_file,
    slice_section,
)


class FilingNotFoundError(Exception):
    pass


class UnsupportedFileError(Exception):
    pass


def get_document_folder(doc_id: str) -> Path:
    filing = get_filing(doc_id)
    if filing is None:
        raise FilingNotFoundError(f"no filing with doc_id={doc_id!r}")
    return CORPUS_DIR / filing["file_path"]


def list_available_files(doc_id: str) -> list[FileInfo]:
    """Lists parseable (xml/html) files for a filing. PDF-only attachments
    are omitted here; callers should note when a filing is pdf+html format."""
    return list_documents_in_folder(get_document_folder(doc_id))


def _resolve_file(doc_id: str, filename: str | None) -> Path:
    files = list_available_files(doc_id)
    if not files:
        raise UnsupportedFileError(
            f"doc_id={doc_id!r} has no parseable xml/html files (may be pdf-only)"
        )
    if filename is None:
        return files[0].path  # list_documents_in_folder sorts main-first
    for fi in files:
        if fi.path.name == filename:
            return fi.path
    raise UnsupportedFileError(f"{filename!r} not found among files for doc_id={doc_id!r}")


def _cache_paths(doc_id: str, file_path: Path) -> tuple[Path, Path]:
    doc_dir = TEXT_CACHE_DIR / doc_id
    doc_dir.mkdir(parents=True, exist_ok=True)
    stem = file_path.stem
    return doc_dir / f"{stem}.txt", doc_dir / f"{stem}.sections.json"


def _load_cached(text_path: Path, sections_path: Path) -> ParsedDoc | None:
    if not text_path.exists():
        return None
    text = text_path.read_text(encoding="utf-8")
    sections: list[Section] = []
    if sections_path.exists():
        raw = json.loads(sections_path.read_text(encoding="utf-8"))
        sections = [Section(**s) for s in raw]
    return ParsedDoc(text=text, sections=sections)


def _save_cache(text_path: Path, sections_path: Path, doc: ParsedDoc) -> None:
    text_path.write_text(doc.text, encoding="utf-8")
    sections_path.write_text(
        json.dumps([asdict(s) for s in doc.sections], ensure_ascii=False),
        encoding="utf-8",
    )


def get_parsed(doc_id: str, filename: str | None = None) -> ParsedDoc:
    """Returns the parsed (and cached) text/sections for a filing. filename
    defaults to the main document; pass an attachment filename (as returned
    by list_available_files) to read an attachment like an audit report."""
    file_path = _resolve_file(doc_id, filename)
    text_path, sections_path = _cache_paths(doc_id, file_path)

    cached = _load_cached(text_path, sections_path)
    if cached is not None:
        return cached

    doc = parse_file(file_path)
    _save_cache(text_path, sections_path, doc)
    return doc


def read_section(doc_id: str, section_title: str, filename: str | None = None) -> str:
    return slice_section(get_parsed(doc_id, filename), section_title)
