"""Библиотека книг: загрузка PDF/md, перевод PDF в md с метками страниц, оглавление, создание глав в chapters.json.

Конвейер работает с md, где каждая страница отмечена <!-- page N --> (как у организаторов).
- md организаторов кладётся как есть (метки уже есть);
- PDF с текстовым слоем переводится в md кодом (быстро, бесплатно), но формулы при этом теряются —
  такие страницы отмечаются и распознаются заново через Gemini (g2c.ocr) с формулами в LaTeX;
- PDF-сканы без текстового слоя распознаются через Gemini постранично.
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from .mdbook import PAGE_RE, load_book, norm, toc as md_toc
from .paths import BOOKS_MD, BOOKS_PDF, CHAPTERS_FILE, ROOT, load_chapters, parse_pages

LIBRARY_META = ROOT / "inputs" / "library.json"
ERRATA_DIR = ROOT / "inputs" / "errata"
PREREQ_DIR = ROOT / "inputs" / "prereq"

_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
               ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
                "h", "c", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"]))


def slugify(s: str, maxlen: int = 40) -> str:
    s = "".join(_TR.get(ch, ch) for ch in s.lower())
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s[:maxlen].strip("_") or "book"


# --- метаданные библиотеки ------------------------------------------------------
def library() -> dict[str, dict]:
    """Все книги: md из inputs/books_md + сведения о загрузке (inputs/library.json)."""
    meta = json.loads(LIBRARY_META.read_text(encoding="utf-8")) if LIBRARY_META.exists() else {}
    for md in sorted(BOOKS_MD.glob("*.md")):
        meta.setdefault(md.name, {"md": md.name, "title": md.stem, "pdf": None, "origin": "организаторы"})
        if not meta[md.name].get("pdf"):
            # PDF организаторов лежат под другими именами — сопоставляем по фамилии автора из имени md
            key = md.stem.split("_", 1)[-1][:6].lower()
            for pdf in BOOKS_PDF.glob("*.pdf"):
                if slugify(pdf.stem).startswith(slugify(key)[:5]):
                    meta[md.name]["pdf"] = pdf.name
                    break
    return meta


def _save_library(meta: dict) -> None:
    LIBRARY_META.parent.mkdir(parents=True, exist_ok=True)
    LIBRARY_META.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


@dataclass
class ImportReport:
    md: str
    pages: int
    pages_with_text: int
    pages_need_ocr: list[int]      # нет текстового слоя — нужен скан → Gemini
    pages_with_math: list[int]     # есть формулы/спецсимволы, которые текстовый слой портит
    toc_entries: int


MATH_HINT_RE = re.compile(r"[∂∇∑∫√≈≤≥±×÷αβγδεηθκλμνπρστφχψωΔΣΩ]|[-]|\b[a-zA-Z]\s*[=<>]\s*[\w(]")


def import_pdf(pdf_src: Path, title: str, use_md: Path | None = None) -> ImportReport:
    """Загрузить книгу. Если дан md с метками страниц — берём его, PDF нужен для сканов.
    Иначе md собирается из текстового слоя PDF; страницы без текста и с формулами отмечаются для распознавания."""
    BOOKS_PDF.mkdir(parents=True, exist_ok=True)
    BOOKS_MD.mkdir(parents=True, exist_ok=True)
    base = slugify(title)
    pdf_name = f"{base}.pdf"
    shutil.copyfile(pdf_src, BOOKS_PDF / pdf_name)
    md_name = f"{base}.md"
    doc = pymupdf.open(BOOKS_PDF / pdf_name)
    toc = doc.get_toc()  # [[уровень, заголовок, страница], ...]
    need_ocr, with_math, with_text = [], [], 0
    if use_md is not None:
        text = use_md.read_text(encoding="utf-8")
        if not PAGE_RE.search(text):
            raise ValueError("В md нет меток страниц <!-- page N -->: загрузите md организаторов или только PDF")
        (BOOKS_MD / md_name).write_text(text, encoding="utf-8")
        origin = "md + PDF"
    else:
        headings: dict[int, list[tuple[int, str]]] = {}
        for lvl, t, p in toc:
            headings.setdefault(p, []).append((lvl, t.strip()))
        parts = [f"# {title}\n"]
        for i, page in enumerate(doc, 1):
            txt = page.get_text("text").strip()
            if len(txt) < 40:
                need_ocr.append(i)
            else:
                with_text += 1
                if len(MATH_HINT_RE.findall(txt)) >= 5:
                    with_math.append(i)
            lines = txt.splitlines()
            for lvl, t in headings.get(i, []):
                # заголовок из оглавления PDF ставим перед строкой, где он начинается (или в начало страницы)
                idx = next((k for k, l in enumerate(lines) if norm(t)[:20] and norm(l).startswith(norm(t)[:20])), None)
                h = "#" * min(lvl, 6) + " " + t
                if idx is None:
                    lines.insert(0, h)
                else:
                    lines[idx] = h
            parts.append(f"<!-- page {i} -->\n" + "\n".join(lines))
        (BOOKS_MD / md_name).write_text("\n".join(parts) + "\n", encoding="utf-8")
        origin = "PDF (текстовый слой)"
    pages = doc.page_count
    doc.close()
    meta = library()
    meta[md_name] = {"md": md_name, "title": title, "pdf": pdf_name, "origin": origin,
                     "need_ocr": need_ocr, "with_math": with_math, "ocr_done": []}
    _save_library(meta)
    return ImportReport(md_name, pages, with_text, need_ocr, with_math, len(toc))


def import_md_only(md_src: Path, title: str) -> str:
    text = md_src.read_text(encoding="utf-8")
    if not PAGE_RE.search(text):
        raise ValueError("В md нет меток страниц <!-- page N -->")
    md_name = f"{slugify(title)}.md"
    (BOOKS_MD / md_name).write_text(text, encoding="utf-8")
    meta = library()
    meta[md_name] = {"md": md_name, "title": title, "pdf": None, "origin": "md без PDF (сканов не будет)"}
    _save_library(meta)
    return md_name


# --- оглавление и главы -------------------------------------------------------------
@dataclass
class TocEntry:
    page: int
    level: int
    title: str
    end_page: int          # последняя страница раздела (до следующего заголовка того же или более высокого уровня)
    next_title: str | None  # заголовок, перед которым раздел кончается


def toc_entries(md_name: str) -> list[TocEntry]:
    book = load_book(BOOKS_MD / md_name)
    raw = md_toc(book)
    out = []
    for i, (p, lvl, t) in enumerate(raw):
        nxt = next(((q, tt) for (q, l2, tt) in raw[i + 1:] if l2 <= lvl), None)
        end = (nxt[0] if nxt else book.max_page)
        # если следующий раздел начинается с начала страницы, наша глава кончается на предыдущей
        out.append(TocEntry(p, lvl, t, max(p, end), nxt[1] if nxt else None))
    return out


def page_text_head(md_name: str, page: int, n: int = 300) -> str:
    return load_book(BOOKS_MD / md_name).pages.get(page, "")[:n]


def save_chapter(ch: dict) -> None:
    """Добавить или обновить главу в chapters.json (проверка: страницы есть в md, маркеры границ находятся)."""
    from .ingest import chapter_pages
    from .paths import Chapter

    data = json.loads(CHAPTERS_FILE.read_text(encoding="utf-8"))
    required = ["id", "prefix", "split", "book_md", "pages"]
    missing = [k for k in required if not ch.get(k)]
    if missing:
        raise ValueError(f"Не заполнено: {', '.join(missing)}")
    if not re.fullmatch(r"[a-z0-9_.-]+", ch["id"]) or not re.fullmatch(r"[a-z0-9_]+", ch["prefix"]):
        raise ValueError("id главы и префикс — только латиница, цифры, _ . -")
    chapter_pages(Chapter(**ch))  # бросит понятную ошибку, если страниц нет или маркер не найден
    data["chapters"] = [c for c in data["chapters"] if c["id"] != ch["id"]] + [ch]
    CHAPTERS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def store_upload(kind: str, chapter_id: str, src_name: str, content: bytes) -> str:
    """Сохранить лист опечаток или карточку-предпосылку главы; вернуть путь относительно корня проекта."""
    d = ERRATA_DIR if kind == "errata" else PREREQ_DIR
    d.mkdir(parents=True, exist_ok=True)
    ext = Path(src_name).suffix.lower() or ".md"
    p = d / f"{chapter_id}{ext}"
    p.write_bytes(content)
    return str(p.relative_to(ROOT)).replace("\\", "/")


def chapter_page_count(ch) -> int:
    return len(parse_pages(ch.pages))


def known_chapter_ids() -> list[str]:
    return list(load_chapters())
