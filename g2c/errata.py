"""Лист опечаток распознавания.

Формат организаторов ещё не выдан, поэтому читаются строки любого из видов:
    стр. 95: неверно → верно
    95 | неверно | верно
    95;неверно;верно            (CSV, первая строка-заголовок пропускается)
Исправление НЕ меняет текст для сверки цитат (организаторы сверяют с выданным md): единица получает
text_fixed и список typo_fixes, а курс показывает исправление с пометкой (ТЗ: «исправления распознавания — с пометкой»).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .paths import ROOT

ARROW_RE = re.compile(r"^\s*(?:стр\.?|с\.|page|p\.)?\s*(\d+)\s*[:.)]?\s*(.+?)\s*(?:→|->|=>|⇒)\s*(.+?)\s*$", re.I)
SEP_RE = re.compile(r"^\s*(?:стр\.?\s*)?(\d+)\s*[|;\t]\s*(.+?)\s*[|;\t]\s*(.+?)\s*[|;]?\s*$", re.I)


@dataclass
class Fix:
    page: int
    wrong: str
    right: str


def parse_errata(text: str) -> list[Fix]:
    fixes = []
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        m = ARROW_RE.match(line) or SEP_RE.match(line)
        if m and m.group(1).isdigit():
            wrong, right = m.group(2).strip().strip("«»\""), m.group(3).strip().strip("«»\"")
            if wrong and wrong != right:
                fixes.append(Fix(int(m.group(1)), wrong, right))
    return fixes


def load_errata(rel_path: str | None) -> list[Fix]:
    if not rel_path:
        return []
    p = ROOT / rel_path
    return parse_errata(p.read_text(encoding="utf-8")) if p.exists() else []


def apply_to_units(units, fixes: list[Fix]) -> int:
    """Проставить text_fixed и typo_fixes. Вернуть число применённых исправлений."""
    n = 0
    for f in fixes:
        for u in units:
            if u.page_pdf == f.page and f.wrong in (u.text_fixed or u.text):
                u.text_fixed = (u.text_fixed or u.text).replace(f.wrong, f.right)
                u.typo_fixes.append({"wrong": f.wrong, "right": f.right})
                n += 1
                break
    return n
