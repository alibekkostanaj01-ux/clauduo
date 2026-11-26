"""Пути проекта. Все модули берут пути отсюда, а не собирают их сами."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"
BOOKS_MD = INPUTS / "books_md"
BOOKS_PDF = INPUTS / "books_pdf"
ANALYSIS = INPUTS / "analysis"
SCHEMAS = ROOT / "schemas"
DOMAIN = ROOT / "domain"
WORK = ROOT / "work"
OUT = ROOT / "out"
CHAPTERS_FILE = ROOT / "chapters.json"
CONFIG_FILE = ROOT / "config.yaml"


@dataclass
class Chapter:
    """Глава из реестра chapters.json."""

    id: str                 # идентификатор главы, напр. latyshova_gl5_par17-20
    prefix: str             # префикс id узлов, напр. latyshova
    split: str              # train | ctrl | test
    book_md: str            # имя md-файла книги в inputs/books_md
    book_pdf: str | None    # имя PDF в inputs/books_pdf (для сканов)
    pages: str              # страницы PDF главы, напр. "90-108" или "9-14,20-30"
    title: str = ""
    start_at: str | None = None     # текст начала главы на первой странице
    end_before: str | None = None   # текст, перед которым глава заканчивается на последней странице
    difficulty: list[str] = field(default_factory=list)
    prereq_pages: str = ""  # страницы короткого чтения из той же книги
    prereq_file: str | None = None  # отдельная карточка-предпосылка (контроль, тест)
    errata_file: str | None = None  # лист опечаток
    domain: str = "geology"
    note: str = ""

    @property
    def md_path(self) -> Path:
        return BOOKS_MD / self.book_md

    @property
    def pdf_path(self) -> Path | None:
        return BOOKS_PDF / self.book_pdf if self.book_pdf else None

    @property
    def work_dir(self) -> Path:
        d = WORK / self.id
        d.mkdir(parents=True, exist_ok=True)
        return d


def load_chapters() -> dict[str, Chapter]:
    data = json.loads(CHAPTERS_FILE.read_text(encoding="utf-8"))
    return {c["id"]: Chapter(**c) for c in data["chapters"]}


def get_chapter(chapter_id: str) -> Chapter:
    chapters = load_chapters()
    if chapter_id not in chapters:
        known = ", ".join(chapters)
        raise KeyError(f"Глава «{chapter_id}» не найдена в chapters.json. Есть: {known}")
    return chapters[chapter_id]


def parse_pages(spec: str) -> list[int]:
    """'9-14,20-30' → [9, 10, ..., 14, 20, ..., 30]."""
    pages: list[int] = []
    for part in (spec or "").replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            pages.extend(range(int(a), int(b) + 1))
        else:
            pages.append(int(part))
    return pages
