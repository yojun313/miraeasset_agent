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
    return list_documents_in_folder(get_document_folder(doc_id))


def _resolve_file(doc_id: str, filename: str | None) -> Path:
    files = list_available_files(doc_id)
    if not files:
        raise UnsupportedFileError(
            f"doc_id={doc_id!r} has no parseable xml/html files (may be pdf-only)"
        )
    if filename is None or not str(filename).strip():
        return files[0].path  # list_documents_in_folder sorts main-first

    wanted = Path(str(filename).strip()).name  # tolerate a path being passed
    for fi in files:
        if fi.path.name == wanted:
            return fi.path
    for fi in files:  # extension omitted, e.g. "20240312000736"
        if fi.path.stem == wanted or fi.path.stem == Path(wanted).stem:
            return fi.path
    for fi in files:  # 문서 라벨을 넣는 경우, e.g. "주요사항보고서(감자결정)"
        if wanted and (wanted in fi.label or fi.label in wanted):
            return fi.path
    stems = [fi.path.stem for fi in files]
    if wanted == files[0].path.parent.name or any(
            wanted.startswith(st) or st.startswith(wanted) for st in stems if len(wanted) >= 8):
        return files[0].path
    if not any(ch.isdigit() for ch in wanted):
        return files[0].path

    available = ", ".join(fi.path.name for fi in files)
    raise UnsupportedFileError(
        f"{filename!r} not found among files for doc_id={doc_id!r}. "
        f"사용 가능한 파일: {available} (filename을 생략하면 본문을 읽습니다)"
    )


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
