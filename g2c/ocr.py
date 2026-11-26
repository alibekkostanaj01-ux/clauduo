"""Распознавание страниц книги через Gemini Vision: скан → markdown с формулами в LaTeX.

Нужно для PDF-сканов без текстового слоя и для страниц, где текстовый слой портит формулы.
Исходный текст страницы сохраняется (inputs/books_md/.orig/), распознанная страница подменяет его в md книги.
Один запрос на страницу — на бесплатном тарифе Gemini это быстро упирается в дневной лимит.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from string import Template

import pymupdf

from .books import LIBRARY_META, library
from .llm import generate
from .mdbook import PAGE_RE, load_book
from .paths import BOOKS_MD, BOOKS_PDF, ROOT, WORK


def render_page(pdf: Path, page: int, dpi: int = 200) -> Path:
    out = WORK / ".scans" / pdf.stem / f"p{page:04d}.png"
    if not out.exists():
        out.parent.mkdir(parents=True, exist_ok=True)
        with pymupdf.open(pdf) as doc:
            doc[page - 1].get_pixmap(dpi=dpi).save(out)
    return out


def _replace_page(md_path: Path, page: int, new_text: str) -> None:
    raw = md_path.read_text(encoding="utf-8")
    marks = list(PAGE_RE.finditer(raw))
    for i, m in enumerate(marks):
        if int(m.group(1)) == page:
            end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
            backup = md_path.parent / ".orig" / f"{md_path.stem}.p{page:04d}.md"
            backup.parent.mkdir(exist_ok=True)
            if not backup.exists():
                backup.write_text(raw[m.end():end], encoding="utf-8")
            raw = raw[:m.end()] + "\n" + new_text.strip() + "\n\n" + raw[end:]
            md_path.write_text(raw, encoding="utf-8")
            load_book.cache_clear()
            return
    raise ValueError(f"В {md_path.name} нет метки страницы {page}")


def ocr_pages(md_name: str, pages: list[int], progress=print) -> list[int]:
    meta = library()
    info = meta.get(md_name) or {}
    if not info.get("pdf"):
        raise ValueError("У книги нет PDF — распознавать нечего")
    pdf = BOOKS_PDF / info["pdf"]
    prompt_t = Template((ROOT / "prompts" / "ocr_page.md").read_text(encoding="utf-8"))
    done = []
    for p in pages:
        img = render_page(pdf, p)
        text, u = generate(prompt_t.safe_substitute(page=p), role="vision", images=[img], stage="ocr",
                           chapter=md_name, node=f"page.{p}")
        text = re.sub(r"^```(?:markdown|md)?\s*|\s*```$", "", text.strip())
        _replace_page(BOOKS_MD / md_name, p, text)
        done.append(p)
        progress(f"стр. {p} распознана ({u.seconds} с)")
    info.setdefault("ocr_done", [])
    info["ocr_done"] = sorted(set(info["ocr_done"]) | set(done))
    meta[md_name] = info
    LIBRARY_META.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return done
