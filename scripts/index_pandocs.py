#!/usr/bin/env python3
"""
Index ~/pandocs documentation into ChromaDB for RAG retrieval.

Usage:
    uv run scripts/index_pandocs.py --pandocs ~/pandocs --chroma data/chroma

Options:
    --pandocs   Path to the directory containing .md documentation files.
    --chroma    Path to the ChromaDB persistence directory (default: data/chroma).
    --recreate  Drop the existing 'pandocs' collection and rebuild from scratch.

Each .md file is:
  1. YAML frontmatter parsed for title, article_id, source_url, tags.
  2. Body split into chunks: first by '## ' section headers, then by blank lines
     for any chunk exceeding 1200 characters.
  3. Each chunk embedded with all-MiniLM-L6-v2 and upserted into ChromaDB.
     Chunks whose content hash hasn't changed are skipped (incremental indexing).

Run after adding or updating documentation. This script does NOT run at server startup.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import sys
from pathlib import Path

# Ensure the package root is on the path when running with uv run
sys.path.insert(0, str(Path(__file__).parent.parent))

from panpilot.intelligence.rag import RAG_EMBEDDING_MODEL, _load_model, chunk_document


def _parse_frontmatter(content: str) -> tuple[dict, str]:
    """Return (metadata dict, body without frontmatter)."""
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if not match:
        return {}, content

    fm_text = match.group(1)
    body = content[match.end():]

    meta: dict = {}
    for line in fm_text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta, body


def _doc_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def main() -> None:
    parser = argparse.ArgumentParser(description="Index pandocs into ChromaDB for RAG.")
    parser.add_argument("--pandocs", required=True, type=Path, help="Path to .md documentation directory")
    parser.add_argument("--chroma", default="data/chroma", type=Path, help="ChromaDB persistence directory")
    parser.add_argument("--recreate", action="store_true", help="Drop and rebuild the collection from scratch")
    args = parser.parse_args()

    pandocs_dir: Path = args.pandocs.expanduser().resolve()
    chroma_dir: Path = args.chroma.expanduser().resolve()

    if not pandocs_dir.exists():
        print(f"ERROR: pandocs directory not found: {pandocs_dir}", file=sys.stderr)
        sys.exit(1)

    md_files = sorted(pandocs_dir.glob("**/*.md"))
    if not md_files:
        print(f"ERROR: no .md files found in {pandocs_dir}", file=sys.stderr)
        sys.exit(1)

    print(f"Loading embedding model {RAG_EMBEDDING_MODEL!r} …")
    model = _load_model()

    import chromadb  # noqa: PLC0415
    chroma_dir.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(chroma_dir))

    if args.recreate:
        try:
            client.delete_collection("pandocs")
            print("Dropped existing 'pandocs' collection.")
        except Exception:
            pass

    collection = client.get_or_create_collection(
        name="pandocs",
        metadata={"hnsw:space": "cosine"},
    )

    n_docs = 0
    n_chunks_total = 0
    n_skipped = 0

    for md_path in md_files:
        content = md_path.read_text(encoding="utf-8")
        fm, body = _parse_frontmatter(content)

        title = fm.get("title") or md_path.stem
        article_id = fm.get("article_id") or ""
        source_url = fm.get("source_url") or ""

        chunks = chunk_document(body, title)
        n_docs += 1
        filename = md_path.name

        for i, chunk_text in enumerate(chunks):
            doc_id = f"{filename}_{i}"
            h = _doc_hash(chunk_text)

            existing = collection.get(ids=[doc_id])
            if (
                existing["ids"]
                and existing["metadatas"]
                and existing["metadatas"][0].get("doc_hash") == h
            ):
                n_skipped += 1
                continue

            embedding = model.encode(chunk_text).tolist()
            collection.upsert(
                ids=[doc_id],
                embeddings=[embedding],
                documents=[chunk_text],
                metadatas=[{
                    "filename": filename,
                    "article_id": article_id,
                    "title": title,
                    "chunk_index": i,
                    "source_url": source_url,
                    "doc_hash": h,
                }],
            )
            n_chunks_total += 1

        if n_docs % 50 == 0:
            print(f"  … {n_docs}/{len(md_files)} docs processed")

    print(
        f"Done. Indexed {n_chunks_total} chunks from {n_docs} docs "
        f"({n_skipped} unchanged chunks skipped)."
    )
    print(f"Collection 'pandocs' now contains {collection.count()} total chunks.")


if __name__ == "__main__":
    main()
