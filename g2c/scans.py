"""Сканы страниц: рендер из выданных PDF. Страница N в md (<!-- page N -->) = страница N в PDF (1-based)."""
from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from .paths import Chapter, parse_pages


def pdf_page_count(pdf: Path) -> int:
    with fitz.open(pdf) as doc:
        return doc.page_count


def render_pages(ch: Chapter, pages: list[int] | None = None, dpi: int = 150) -> list[Path]:
    if not ch.pdf_path or not ch.pdf_path.exists():
        raise FileNotFoundError(f"Нет PDF для главы {ch.id}: {ch.pdf_path}")
    out_dir = ch.work_dir / "scans"
    out_dir.mkdir(exist_ok=True)
    pages = pages or parse_pages(ch.pages)
    out = []
    with fitz.open(ch.pdf_path) as doc:
        for p in pages:
            target = out_dir / f"p{p:03d}.png"
            if not target.exists():
                pix = doc[p - 1].get_pixmap(dpi=dpi)
                pix.save(target)
            out.append(target)
    return out
