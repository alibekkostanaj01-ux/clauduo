"""Загрузка главы: вырезка страниц из md книги и разбиение на единицы главы.

Единицы — как в coverage.schema.json: paragraph | figure | formula | table | worked_example | task | heading.
ID единицы — p095.03 (третья единица на стр. 95), устойчив между прогонами при неизменном md.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from .mdbook import HEADING_RE, load_book, norm
from .paths import Chapter, parse_pages


@dataclass
class Unit:
    id: str
    kind: str
    page_pdf: int
    text: str
    by_model: bool = False          # вся единица написана моделью при извлечении (описание без подписи автора)
    heading_path: list[str] = field(default_factory=list)  # заголовки, под которыми лежит единица
    task_number: str | None = None
    # описание рисунка, сделанное моделью при извлечении (блок «----  ━━━ FORMAT N … KEYWORDS  ----»).
    # Это НЕ текст автора: утверждения из него — source_kind=figure_description_model, by_model=true.
    model_description: str | None = None
    # текст похож на брак распознавания (кириллица, распознанная латиницей и т. п.) — сверять со сканом
    ocr_suspect: bool = False
    # лист опечаток: исправленный текст для курса (цитаты сверяются с исходным text)
    text_fixed: str | None = None
    typo_fixes: list[dict] = field(default_factory=list)

    @property
    def excerpt(self) -> str:
        words = re.sub(r"\s+", " ", self.text).strip().split(" ")
        return " ".join(words[:15])


def _cut(text: str, marker: str | None, keep_after: bool) -> str:
    """Обрезать страницу по текстовому маркеру. keep_after=True — оставить текст начиная с маркера."""
    if not marker:
        return text
    i = text.find(marker)
    if i < 0:
        # поиск без учёта пробелов и регистра — по строкам
        nm = norm(marker)
        lines = text.splitlines(keepends=True)
        pos = 0
        for line in lines:
            if nm and nm in norm(line):
                i = pos
                break
            pos += len(line)
    if i < 0:
        raise ValueError(f"Маркер границы главы не найден на странице: «{marker}»")
    return text[i:] if keep_after else text[:i]


def chapter_pages(ch: Chapter, prereq: bool = False) -> dict[int, str]:
    book = load_book(ch.md_path)
    spec = ch.prereq_pages if prereq else ch.pages
    pages = parse_pages(spec)
    missing = [p for p in pages if p not in book.pages]
    if missing:
        raise ValueError(f"В md нет страниц {missing} (глава {ch.id})")
    out = {p: book.pages[p] for p in pages}
    if not prereq and pages:
        out[pages[0]] = _cut(out[pages[0]], ch.start_at, keep_after=True)
        out[pages[-1]] = _cut(out[pages[-1]], ch.end_before, keep_after=False)
    return out


# --- классификация блоков -----------------------------------------------------
FIG_RE = re.compile(r"^\s*(\*\*)?\s*(Рис\.|Рисунок|Fig\.|Figure)\s*\d", re.I)
TABLE_CAPTION_RE = re.compile(r"^\s*(\*\*)?\s*(Таблица|Table)\s*\d", re.I)
EXAMPLE_RE = re.compile(r"^\s*(\*\*)?\s*(Пример|Example)\b", re.I)
TASK_ITEM_RE = re.compile(r"^\s*(\d{1,3})\s*[.)]\s+\S")
TASKS_HEADING_RE = re.compile(r"^(задачи|упражнения|вопросы|контрольные вопросы|problems|exercises)\b", re.I)
FORMULA_ONLY_RE = re.compile(r"^\s*(\$\$.*\$\$|\\\[.*\\\]|\$[^$]+\$|\\\(.*\\\))\s*(\(\s*[\w.IVXLC-]+\s*\))?\s*[.,;]?\s*$", re.S)


def _is_table(block: str) -> bool:
    lines = [l for l in block.splitlines() if l.strip()]
    if "<table" in block.lower():
        return True
    pipes = sum(1 for l in lines if l.strip().startswith("|") or l.count("|") >= 2)
    return len(lines) >= 2 and pipes >= max(2, len(lines) // 2)


def _is_model_description(block: str) -> bool:
    """Описание рисунка, добавленное при извлечении: цитатные блоки «> …» и списки «- Ось …»."""
    s = block.lstrip()
    return s.startswith(">") or s.startswith("- ") or s.startswith("* ")


def _blocks(text: str) -> list[str]:
    """Блоки — куски между пустыми строками; заголовок всегда отдельный блок."""
    blocks: list[str] = []
    cur: list[str] = []
    for line in text.splitlines():
        if HEADING_RE.match(line):
            if cur:
                blocks.append("\n".join(cur))
                cur = []
            blocks.append(line)
        elif not line.strip():
            if cur:
                blocks.append("\n".join(cur))
                cur = []
        else:
            cur.append(line)
    if cur:
        blocks.append("\n".join(cur))
    return [b for b in blocks if b.strip()]


DESC_FENCE_RE = re.compile(r"^\s*-{4,}\s*$")
_LATEX_RE = re.compile(r"\\\(.*?\\\)|\$[^$]*\$|\\\[.*?\\\]", re.S)


def ocr_suspect(text: str) -> bool:
    """Русский текст, распознанный латиницей («OchoBbAcb Ha pe3yIbTaTax»): много латиницы вне формул."""
    plain = _LATEX_RE.sub(" ", text)
    lat = len(re.findall(r"[A-Za-z]", plain))
    cyr = len(re.findall(r"[А-Яа-яЁё]", plain))
    letters = lat + cyr
    return letters >= 20 and lat / letters > 0.4 and re.search(r"[A-Za-z]{3,}\d|\d[A-Za-z]{2,}|[A-Z][a-z]+[A-Z]", plain) is not None


BOOK_TASK_LINE_RE = re.compile(r"^\s*\d{2,3}\s*[.)]\s+[А-ЯЁA-Z]")


def _split_task_block(block: str) -> list[str]:
    """Блок с задачами без пустых строк между ними → по задаче на блок (текст до первой задачи — отдельно)."""
    parts: list[list[str]] = []
    for line in block.splitlines():
        if TASK_ITEM_RE.match(line) or not parts:
            parts.append([line])
        else:
            parts[-1].append(line)
    return ["\n".join(p) for p in parts]


def _segments(text: str, in_desc: bool) -> tuple[list[tuple[bool, str]], bool]:
    """Разделить страницу на куски (это_описание_модели, текст).

    Описание рисунка от модели: строка «----», затем «━━━ FORMAT N: …», …, «KEYWORDS: …», строка «----».
    Описание может переходить на следующую страницу — поэтому состояние in_desc передаётся между страницами.
    """
    lines = text.splitlines()
    segs: list[tuple[bool, str]] = []
    cur: list[str] = []

    def flush(flag: bool):
        if cur and "\n".join(cur).strip():
            segs.append((flag, "\n".join(cur)))
        cur.clear()

    closed_by_keywords = False
    for i, line in enumerate(lines):
        if "━━━" in line and not in_desc:
            # начало описания (даже если «----» осталась на прошлой странице или общая с предыдущим описанием)
            flush(False)
            in_desc = True
            cur.append(line)
            continue
        if in_desc and line.strip().upper().startswith("KEYWORDS"):
            # описание заканчивается строкой KEYWORDS, даже если закрывающей «----» нет
            cur.append(line)
            flush(True)
            in_desc = False
            closed_by_keywords = True
            continue
        if DESC_FENCE_RE.match(line):
            if in_desc:
                flush(True)
                in_desc = False
                continue
            if closed_by_keywords:
                closed_by_keywords = False
                continue  # закрывающая ограда после KEYWORDS
            nxt = next((l for l in lines[i + 1:] if l.strip()), None)
            if nxt is None or "━━━" in nxt:
                continue  # открывающая ограда — выбрасываем
        if line.strip():
            closed_by_keywords = False
        cur.append(line)
    flush(in_desc)
    return segs, in_desc


def _attach_descriptions(units: list[Unit]) -> list[Unit]:
    """Описание без подписи приклеить к рисунку: к следующему, если подпись идёт сразу за описанием,
    иначе к ближайшему предыдущему (в пределах 4 единиц). Осиротевшие описания остаются рисунком by_model."""
    out: list[Unit] = []
    i = 0
    while i < len(units):
        u = units[i]
        if u.kind == "figure" and u.by_model and not u.text:
            nxt = units[i + 1] if i + 1 < len(units) else None
            target = None
            if nxt is not None and nxt.kind == "figure" and nxt.text and not nxt.model_description:
                target = nxt
            else:
                target = next((x for x in reversed(out[-4:]) if x.kind == "figure" and x.text), None)
            if target is not None:
                target.model_description = ((target.model_description + "\n\n") if target.model_description else "") + (u.model_description or "")
                i += 1
                continue
        out.append(u)
        i += 1
    return out


def split_units(pages: dict[int, str]) -> list[Unit]:
    units: list[Unit] = []
    headings: list[str] = []
    in_tasks = False
    in_desc = False
    last_task_no: int | None = None
    for page in sorted(pages):
        k = 0
        prev_kind = None
        segs, in_desc = _segments(pages[page], in_desc)
        stream: list[tuple[bool, str]] = []
        for is_desc, seg in segs:
            if is_desc:
                stream.append((True, seg.strip()))
            else:
                for b in _blocks(seg):
                    # задачи книги идут сквозной нумерацией (86., 87., …, 103.) и часто без пустых строк
                    if any(BOOK_TASK_LINE_RE.match(l) for l in b.splitlines()[1:]) or \
                            sum(1 for l in b.splitlines() if TASK_ITEM_RE.match(l)) >= 2:
                        stream.extend((False, t) for t in _split_task_block(b))
                    else:
                        stream.append((False, b))
        for is_desc, block in stream:
            if is_desc:
                # пока отдельной единицей; к рисунку приклеит _attach_descriptions
                k += 1
                units.append(Unit(id=f"p{page:03d}.{k:02d}", kind="figure", page_pdf=page, text="",
                                  by_model=True, heading_path=list(headings), model_description=block))
                prev_kind = "figure"
                continue
            kind = "paragraph"
            by_model = False
            task_no = None
            hm = HEADING_RE.match(block)
            if hm:
                kind = "heading"
                title = hm.group(2).strip("* ")
                level = len(hm.group(1))
                headings = headings[: level - 1] + [title]
                in_tasks = bool(TASKS_HEADING_RE.match(title))
            elif FIG_RE.match(block):
                kind = "figure"
            elif _is_table(block) or TABLE_CAPTION_RE.match(block):
                kind = "table"
            elif FORMULA_ONLY_RE.match(block):
                kind = "formula"
            elif TASKS_HEADING_RE.match(block.strip("*_ \n")) and len(block) < 40:
                kind = "heading"  # «Задачи» курсивом, а не заголовком md
                in_tasks = True
            elif TASK_ITEM_RE.match(block) and (
                    # под заголовком «Задачи» — с растущим номером
                    (in_tasks and (last_task_no is None or int(TASK_ITEM_RE.match(block).group(1)) > last_task_no))
                    # или продолжение сквозной нумерации задач книги после вставки текста/рисунка
                    or (last_task_no is not None and last_task_no < int(TASK_ITEM_RE.match(block).group(1)) <= last_task_no + 3)):
                kind = "task"
                task_no = TASK_ITEM_RE.match(block).group(1)
                last_task_no = int(task_no)
            elif EXAMPLE_RE.match(block):
                kind = "worked_example"
            k += 1
            units.append(Unit(
                id=f"p{page:03d}.{k:02d}", kind=kind, page_pdf=page, text=block,
                by_model=by_model, heading_path=list(headings), task_number=task_no,
                ocr_suspect=ocr_suspect(block) if kind not in ("formula", "table") else False,
            ))
            prev_kind = kind
    return _attach_descriptions(units)


def ingest(ch: Chapter) -> dict:
    """Разобрать главу и записать work/<глава>/units.json, prereq_units.json, coverage.draft.json."""
    from .errata import apply_to_units, load_errata

    pages = chapter_pages(ch)
    units = split_units(pages)
    fixes = load_errata(ch.errata_file)
    applied = apply_to_units(units, fixes)
    wd = ch.work_dir
    (wd / "pages.json").write_text(json.dumps({str(p): t for p, t in pages.items()}, ensure_ascii=False, indent=1), encoding="utf-8")
    (wd / "units.json").write_text(json.dumps([asdict(u) for u in units], ensure_ascii=False, indent=1), encoding="utf-8")

    prereq_units = []
    if ch.prereq_pages or ch.prereq_file:
        prereq_units = prereq_unit_list(ch)
        (wd / "prereq_units.json").write_text(json.dumps([asdict(u) for u in prereq_units], ensure_ascii=False, indent=1), encoding="utf-8")

    coverage = {
        "chapter": ch.id,
        "rows": [
            {"unit_id": u.id, "kind": u.kind, "page_pdf": u.page_pdf, "excerpt": u.excerpt, "node_ids": [], "reason": None}
            for u in units
        ],
    }
    (wd / "coverage.draft.json").write_text(json.dumps(coverage, ensure_ascii=False, indent=1), encoding="utf-8")

    stats: dict[str, int] = {}
    for u in units:
        stats[u.kind] = stats.get(u.kind, 0) + 1
    return {"chapter": ch.id, "pages": len(pages), "units": len(units), "kinds": stats,
            "figures_with_model_description": sum(1 for u in units if u.model_description),
            "figures_without_description": sum(1 for u in units if u.kind == "figure" and not u.model_description),
            "ocr_suspect": [u.id for u in units if u.ocr_suspect],
            "errata": f"{applied} из {len(fixes)}" if fixes else "нет",
            "prereq_units": len(prereq_units)}


PREREQ_CARD_PAGE = 0  # карточка-предпосылка не из книги: её «страница» — 0


def prereq_card_text(ch: Chapter) -> str:
    from .paths import ROOT

    if not ch.prereq_file:
        return ""
    p = ROOT / ch.prereq_file
    return p.read_text(encoding="utf-8") if p.exists() else ""


def prereq_unit_list(ch: Chapter) -> list[Unit]:
    """Единицы короткого чтения: страницы той же книги (prereq_pages) и/или отдельная карточка (prereq_file)."""
    units: list[Unit] = []
    if ch.prereq_pages:
        units += split_units(chapter_pages(ch, prereq=True))
    card = prereq_card_text(ch)
    if card:
        units += split_units({PREREQ_CARD_PAGE: card})
    for u in units:
        u.id = "pre." + u.id
    return units


def load_units(ch: Chapter) -> list[Unit]:
    data = json.loads((ch.work_dir / "units.json").read_text(encoding="utf-8"))
    return [Unit(**d) for d in data]
