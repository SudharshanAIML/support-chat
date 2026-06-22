"""Ingest knowledge docs into the Chroma store for ASK mode.

Usage
-----
    python -m app.rag.ingest                 # ingest KNOWLEDGE_DOCS_DIR
    python -m app.rag.ingest --reset         # wipe + re-ingest
    python -m app.rag.ingest --path ./docs   # ingest a specific folder

Reads ``.md`` / ``.txt`` / ``.markdown`` files, splits them into overlapping
chunks (by headings + size), and upserts them with source metadata.  If the
docs folder is missing or empty, it falls back to ``CRM_BACKEND_API.md`` so
ASK mode has *something* to ground on out of the box.
"""

from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path

from app.core.config import get_settings
from app.rag.store import get_knowledge_store

logger = logging.getLogger(__name__)

_DOC_EXTENSIONS = {".md", ".markdown", ".txt"}
_CHUNK_SIZE = 1200  # characters
_CHUNK_OVERLAP = 150


def _split_text(text: str, *, size: int = _CHUNK_SIZE, overlap: int = _CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping character windows, preferring paragraph breaks."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []

    chunks: list[str] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + size, n)
        # Try to break on a paragraph or newline boundary near the window end.
        if end < n:
            for sep in ("\n\n", "\n", ". "):
                idx = text.rfind(sep, start + size // 2, end)
                if idx != -1:
                    end = idx + len(sep)
                    break
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        start = max(end - overlap, end) if end <= start else end - overlap
        if start <= 0:
            start = end
    return chunks


def _collect_files(docs_dir: Path) -> list[Path]:
    if not docs_dir.exists():
        return []
    return [
        p
        for p in sorted(docs_dir.rglob("*"))
        if p.is_file() and p.suffix.lower() in _DOC_EXTENSIONS
    ]


def ingest(path: str | None = None, *, reset: bool = False) -> int:
    """Ingest docs into the knowledge store. Returns the number of chunks upserted.

    ``CRM_DOCUMENTATION.md`` (the primary CRM knowledge base) is always ingested
    when present, in addition to any files found in ``KNOWLEDGE_DOCS_DIR``.
    If neither source yields files, the legacy ``CRM_BACKEND_API.md`` is used as
    a last-resort fallback.
    """
    settings = get_settings()
    docs_dir = Path(path or settings.KNOWLEDGE_DOCS_DIR)
    files = _collect_files(docs_dir)

    # Always include the primary CRM knowledge base if it exists.
    crm_doc = Path("CRM_DOCUMENTATION.md")
    if crm_doc.exists() and crm_doc not in files:
        files = [crm_doc] + list(files)
        logger.info("Including primary knowledge base: %s", crm_doc)

    if not files:
        fallback = Path("CRM_BACKEND_API.md")
        if fallback.exists():
            logger.warning(
                "No docs in %s — falling back to %s", docs_dir, fallback
            )
            files = [fallback]
        else:
            logger.error("No docs found in %s and no fallback available.", docs_dir)
            return 0

    store = get_knowledge_store()
    if reset:
        store.reset()

    total = 0
    for file in files:
        try:
            text = file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            logger.exception("Failed to read %s", file)
            continue

        chunks = _split_text(text)
        if not chunks:
            continue

        rel = os.path.relpath(str(file))
        ids = [f"{rel}::{i}" for i in range(len(chunks))]
        metadatas = [{"source": rel, "chunk": i} for i in range(len(chunks))]
        store.upsert(ids=ids, documents=chunks, metadatas=metadatas)
        total += len(chunks)
        logger.info("Ingested %d chunks from %s", len(chunks), rel)

    logger.info("Ingestion complete: %d chunks total (store now %d)", total, store.count())
    return total


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Ingest knowledge docs for ASK mode.")
    parser.add_argument("--path", help="Docs folder (defaults to KNOWLEDGE_DOCS_DIR).")
    parser.add_argument("--reset", action="store_true", help="Wipe the collection first.")
    args = parser.parse_args()
    ingest(args.path, reset=args.reset)


if __name__ == "__main__":
    main()
