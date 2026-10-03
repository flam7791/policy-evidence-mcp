"""Load documents from a corpus folder and split them into citable chunks.

Supported formats: Markdown (.md), plain text (.txt), PDF (.pdf) and Word (.docx).

Metadata per document:
- title           from front matter, else the first "# " heading, else the file name
- source          from front matter (a URL or reference), else the relative file path
- classification  from front matter, else the name of the folder it sits in when that name
                  is a sensitivity level (corpus/internal/x.pdf -> "internal"), else "public"

A sidecar file `<name>.meta.json` next to a document, when present, provides the same three
fields for formats that cannot carry front matter (PDF, Word); the SharePoint sync writes one
for every file, with the document's web URL as source and the classification from its label.

Markdown front matter is a simple block of "key: value" lines between two "---" lines at the
top of the file. Chunks never cross a section heading, so every chunk can be cited as
"document > section" (Markdown) or "document, page n" (PDF).
"""

from __future__ import annotations

import json
import re
import unicodedata
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from xml.etree import ElementTree

from .config import CLASSIFICATION_LEVELS

SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf", ".docx"}
DEFAULT_MAX_CHARS = 1200


@dataclass
class Document:
    doc_id: str
    title: str
    source: str
    classification: str
    path: str
    # (page number or None, section heading or None, text) in reading order
    blocks: list[tuple[int | None, str | None, str]]


@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    section: str | None
    page: int | None
    text: str
    classification: str
    source: str

    def citation(self) -> str:
        where = f"page {self.page}" if self.page else (self.section or "")
        return f"{self.title}" + (f", {where}" if where else "") + f" [{self.chunk_id}]"

    def to_dict(self) -> dict:
        return asdict(self)


# --------------------------------------------------------------------------- loading


def clean_text(text: str) -> str:
    """Normalise Unicode and remove control characters that can hide instructions."""
    text = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch)[0] != "C")


def _slug(relative: Path) -> str:
    stem = relative.with_suffix("").as_posix().lower()
    return re.sub(r"[^a-z0-9/]+", "-", stem).strip("-")


def _split_front_matter(text: str) -> tuple[dict[str, str], str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("\n")
    meta: dict[str, str] = {}
    for i, line in enumerate(parts[1:], start=1):
        if line.strip() == "---":
            return meta, "\n".join(parts[i + 1 :])
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip().strip('"')
    return {}, text  # no closing "---": treat everything as body


def _folder_classification(relative: Path) -> str:
    for part in reversed(relative.parts[:-1]):
        if part.lower() in CLASSIFICATION_LEVELS:
            return part.lower()
    return "public"


def _markdown_blocks(body: str) -> tuple[str | None, list[tuple[int | None, str | None, str]]]:
    """Split Markdown into (first H1 title, [(None, section, paragraph), ...])."""
    title = None
    section: str | None = None
    blocks: list[tuple[int | None, str | None, str]] = []
    paragraph: list[str] = []

    def flush() -> None:
        if paragraph:
            blocks.append((None, section, " ".join(paragraph).strip()))
            paragraph.clear()

    for line in body.splitlines():
        heading = re.match(r"^(#{1,6})\s+(.*)$", line)
        if heading:
            flush()
            text = heading.group(2).strip()
            if heading.group(1) == "#" and title is None:
                title = text
            else:
                section = text
        elif not line.strip():
            flush()
        else:
            paragraph.append(line.strip())
    flush()
    return title, blocks


def _pdf_blocks(path: Path) -> tuple[str | None, list[tuple[int | None, str | None, str]]]:
    from pypdf import PdfReader  # imported lazily: only needed for PDFs

    reader = PdfReader(str(path))
    title = (reader.metadata.title if reader.metadata else None) or None
    blocks = []
    for number, page in enumerate(reader.pages, start=1):
        text = " ".join((page.extract_text() or "").split())
        if text:
            blocks.append((number, None, text))
    return title, blocks


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _docx_blocks(path: Path) -> tuple[str | None, list[tuple[int | None, str | None, str]]]:
    """Paragraphs of a Word document, with Heading styles as sections (standard library only)."""
    with zipfile.ZipFile(path) as archive:
        root = ElementTree.fromstring(archive.read("word/document.xml"))
    title, section = None, None
    blocks: list[tuple[int | None, str | None, str]] = []
    for para in root.iter(f"{W}p"):
        text = "".join(t.text or "" for t in para.iter(f"{W}t")).strip()
        if not text:
            continue
        style = para.find(f"{W}pPr/{W}pStyle")
        name = (style.get(f"{W}val") if style is not None else "") or ""
        if name.lower() == "title" or (name.lower() == "heading1" and title is None):
            title = title or text
            if name.lower() == "heading1":
                section = text
        elif name.lower().startswith("heading"):
            section = text
        else:
            blocks.append((None, section, text))
    return title, blocks


def _sidecar(path: Path) -> dict[str, str]:
    side = path.with_name(path.name + ".meta.json")
    if not side.exists():
        return {}
    data = json.loads(side.read_text(encoding="utf-8"))
    return {k: str(v) for k, v in data.items() if k in {"title", "source", "classification"}}


def load_document(path: Path, corpus_root: Path) -> Document:
    relative = path.relative_to(corpus_root)
    meta: dict[str, str] = {}
    if path.suffix.lower() == ".pdf":
        title, blocks = _pdf_blocks(path)
    elif path.suffix.lower() == ".docx":
        title, blocks = _docx_blocks(path)
    else:
        meta, body = _split_front_matter(path.read_text(encoding="utf-8", errors="replace"))
        title, blocks = _markdown_blocks(body)
    meta = {**meta, **_sidecar(path)}

    classification = meta.get("classification", _folder_classification(relative)).lower()
    return Document(
        doc_id=_slug(relative),
        title=clean_text(meta.get("title") or title or path.stem.replace("-", " ").title()),
        source=meta.get("source") or relative.as_posix(),
        classification=classification,
        path=relative.as_posix(),
        blocks=[(page, section, clean_text(text)) for page, section, text in blocks],
    )


def iter_corpus(corpus_root: Path):
    """Yield every supported document under corpus_root, skipping README files."""
    for path in sorted(corpus_root.rglob("*")):
        if (
            path.is_file()
            and path.suffix.lower() in SUPPORTED_SUFFIXES
            and path.stem.lower() != "readme"
        ):
            yield load_document(path, corpus_root)


# --------------------------------------------------------------------------- chunking


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split text longer than max_chars at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?;])\s+", text)
    pieces, current = [], ""
    for sentence in sentences:
        while len(sentence) > max_chars:  # a single enormous "sentence"
            pieces.append(sentence[:max_chars])
            sentence = sentence[max_chars:]
        if current and len(current) + 1 + len(sentence) > max_chars:
            pieces.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        pieces.append(current)
    return pieces


def chunk_document(doc: Document, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    """Group consecutive paragraphs of the same section/page into chunks of <= max_chars."""
    chunks: list[Chunk] = []
    buffer: list[str] = []
    current_key: tuple[int | None, str | None] | None = None

    def emit() -> None:
        if not buffer or current_key is None:
            return
        chunks.append(
            Chunk(
                chunk_id=f"{doc.doc_id}#{len(chunks) + 1}",
                doc_id=doc.doc_id,
                title=doc.title,
                page=current_key[0],
                section=current_key[1],
                text=" ".join(buffer),
                classification=doc.classification,
                source=doc.source,
            )
        )
        buffer.clear()

    for page, section, text in doc.blocks:
        key = (page, section)
        if key != current_key:
            emit()
            current_key = key
        for piece in _split_long(text, max_chars):
            if buffer and len(" ".join(buffer)) + 1 + len(piece) > max_chars:
                emit()
            buffer.append(piece)
    emit()
    return chunks
