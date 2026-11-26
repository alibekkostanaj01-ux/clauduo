"""Книга в md организаторов: страницы по меткам <!-- page N -->, оглавление, нормализация для сверки цитат."""
from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PAGE_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


@dataclass
class Book:
    path: Path
    pages: dict[int, str]  # страница PDF → текст страницы (без метки)

    def text(self, pages: list[int]) -> str:
        return "\n".join(self.pages.get(p, "") for p in pages)

    @property
    def max_page(self) -> int:
        return max(self.pages) if self.pages else 0


@lru_cache(maxsize=16)
def load_book(path: Path) -> Book:
    raw = Path(path).read_text(encoding="utf-8")
    pages: dict[int, str] = {}
    marks = list(PAGE_RE.finditer(raw))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(raw)
        n = int(m.group(1))
        # если метка страницы повторяется, склеиваем куски
        pages[n] = (pages.get(n, "") + "\n" + raw[start:end]).strip("\n")
    return Book(path=Path(path), pages=pages)


def toc(book: Book) -> list[tuple[int, int, str]]:
    """Оглавление: (страница, уровень, заголовок)."""
    out = []
    for p in sorted(book.pages):
        for line in book.pages[p].splitlines():
            m = HEADING_RE.match(line)
            if m:
                out.append((p, len(m.group(1)), m.group(2)))
    return out


# --- нормализация для сверки цитат ---------------------------------------
# Организаторы: «при сверке не учитываются пробелы, переносы строк и регистр».
_WS = re.compile(r"\s+")


def norm(s: str) -> str:
    return _WS.sub("", s).lower()


def norm_soft(s: str) -> str:
    """Мягкая нормализация для диагностики (не для решения о прохождении):
    ё→е, разные тире и кавычки, мягкие переносы, разметка LaTeX."""
    s = s.replace("ё", "е").replace("Ё", "Е").replace("­", "")
    s = re.sub(r"[‐‑‒–—―-]", "-", s)
    s = re.sub(r"[«»„“”\"']", "", s)
    s = re.sub(r"[\\$()]", "", s)
    return norm(s)


def quote_on_page(quote: str, book: Book, page: int) -> bool:
    return norm(quote) in norm(book.pages.get(page, ""))


def find_quote_pages(quote: str, book: Book, pages: list[int] | None = None, soft: bool = False) -> list[int]:
    f = norm_soft if soft else norm
    q = f(quote)
    if not q:
        return []
    cand = pages if pages is not None else sorted(book.pages)
    return [p for p in cand if q in f(book.pages.get(p, ""))]


# --- числа ----------------------------------------------------------------
NUM_RE = re.compile(r"(?<![\w.,])[-−]?\d+(?:[.,]\d+)?(?![\w])")


def numbers(s: str) -> set[str]:
    """Числа из текста в каноничном виде: запятая → точка, без минуса-знака."""
    out = set()
    for m in NUM_RE.finditer(s):
        v = m.group(0).replace("−", "-").lstrip("-").replace(",", ".")
        out.add(v)
    return out
