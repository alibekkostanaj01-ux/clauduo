"""Тесты ядра без обращения к LLM: разбор md, границы глав, единицы, валидаторы."""
import json

import pytest

from g2c.ingest import _segments, chapter_pages, split_units
from g2c.mdbook import load_book, norm, numbers, quote_on_page
from g2c.paths import BOOKS_MD, DOMAIN, SCHEMAS, get_chapter, load_chapters, parse_pages
from conftest import needs_domain, needs_schemas
from g2c.validate import check_claims, check_coverage, check_schema, check_terms_order

HAS_BOOKS = (BOOKS_MD / "train_latyshova.md").exists()
needs_books = pytest.mark.skipif(not HAS_BOOKS, reason="нет inputs/books_md (материалы организаторов не в git)")


def test_parse_pages():
    assert parse_pages("9-11,20") == [9, 10, 11, 20]
    assert parse_pages("") == []


def test_norm_ignores_space_newline_case():
    assert norm("Выделение  КОЛЛЕКТОРА\nможно") == norm("выделение коллектора можно")


def test_numbers_canonical():
    assert numbers("Kп = 16 %, 0,5 и 3–5 мД; −2") == {"16", "0.5", "3", "5", "2"}


def test_segments_model_description():
    page = "Текст автора.\n\n----\n━━━ FORMAT 6: XY PLOT ━━━\nОси...\nKEYWORDS: x\n----\n\nЕщё текст."
    segs, in_desc = _segments(page, False)
    assert [s[0] for s in segs] == [False, True, False]
    assert not in_desc


def test_description_without_closing_fence_ends_at_keywords():
    page = "Рис. 72. Подпись.\n\n----\n━━━ FORMAT 7: NOMOGRAM ━━━\nописание\nKEYWORDS: a, b\n\n#### Задачи\n\n96. Привести примеры?"
    units = split_units({102: page})
    assert [u.task_number for u in units if u.kind == "task"] == ["96"]
    assert "KEYWORDS" in units[0].model_description and "96." not in units[0].model_description


def test_description_attaches_to_caption_after_it():
    units = split_units({5: "Абзац.\n\n----\n━━━ FORMAT 2: WELL LOG ━━━\nописание\n----\n\nРис. 3. Подпись автора."})
    figs = [u for u in units if u.kind == "figure"]
    assert len(figs) == 1 and figs[0].text.startswith("Рис. 3") and "FORMAT 2" in figs[0].model_description


def test_tasks_split():
    units = split_units({7: "# Задачи\n\n86. Первая задача?\n\n87. Вторая задача?"})
    tasks = [u for u in units if u.kind == "task"]
    assert [t.task_number for t in tasks] == ["86", "87"]


def test_tasks_glued_and_numbered_list_after_tasks():
    units = split_units({9: "# Задачи\n\n90. Первая?\n91. Вторая?\n\nОбычный текст главы.\n\n1. Пункт перечня."})
    kinds = [(u.kind, u.task_number) for u in units]
    assert ("task", "90") in kinds and ("task", "91") in kinds
    assert ("task", "1") not in kinds


def test_ocr_suspect():
    from g2c.ingest import ocr_suspect
    assert ocr_suspect("101. OchoBbAcb Ha pe3yIbTaTax peHHeHH npeIbIyIeH 3aJaH, BbUHcJIHTb, KaKa OIIb6a")
    assert not ocr_suspect("Сопротивление \\( \\rho_{n} \\) пласта и Kп = 16 %, метод SP и GR")


def test_registry_loads():
    ch = load_chapters()
    assert {"latyshova_gl5_par17-20", "basniev_gl23_gl24par1", "kuznetsov_gl10_par1-4"} <= set(ch)


@needs_books
@pytest.mark.parametrize("cid", list(load_chapters()))
def test_chapter_boundaries(cid):
    ch = get_chapter(cid)
    pages = chapter_pages(ch)
    assert list(pages) == parse_pages(ch.pages)
    if ch.start_at:
        assert norm(ch.start_at) in norm(pages[min(pages)])[: len(norm(ch.start_at)) + 40]
    if ch.end_before:
        assert norm(ch.end_before) not in norm(pages[max(pages)])


@needs_books
@needs_schemas
def test_example_layer_from_organizers_passes():
    layer = json.loads((SCHEMAS / "example_layer.json").read_text(encoding="utf-8"))
    assert check_schema(layer, "node") == []
    book = load_book(BOOKS_MD / "train_latyshova.md")
    assert check_claims(layer, book) == []
    assert check_terms_order(layer) == []


@needs_books
def test_quote_check_catches_wrong_page_and_paraphrase():
    book = load_book(BOOKS_MD / "train_latyshova.md")
    q = "построение разреза и особенно выделение коллектора можно осуществить лишь на основании количественной интерпретации геофизических данных"
    assert quote_on_page(q, book, 95)
    layer = {"nodes": [{"id": "x.n", "order": 1, "claims": [
        {"id": "x.n#c1", "text": "т", "page_pdf": 96, "quote": q},
        {"id": "x.n#c2", "text": "т", "page_pdf": 95, "quote": "выделение коллектора возможно только по количественной интерпретации"},
        {"id": "x.n#c3", "text": "порог 77,7 Ом·м", "page_pdf": 95, "quote": q},
    ]}]}
    issues = check_claims(layer, book)
    kinds = [(i.where, i.check) for i in issues]
    assert ("x.n#c1", "quote") in kinds and "стр. [95]" in issues[0].hint
    assert ("x.n#c2", "quote") in kinds
    assert ("x.n#c3", "numbers") in kinds


def test_coverage_rejects_unshowable():
    cov = {"rows": [{"unit_id": "p001.01", "node_ids": [], "reason": "непоказуемо"},
                    {"unit_id": "p001.02", "node_ids": [], "reason": None},
                    {"unit_id": "p001.03", "node_ids": [], "reason": "колонтитул страницы"}]}
    assert [i.where for i in check_coverage(cov)] == ["p001.01", "p001.02"]


def test_terms_order():
    layer = {"nodes": [
        {"id": "a", "order": 1, "terms_introduced": [], "terms_used": ["ПС"]},
        {"id": "b", "order": 2, "terms_introduced": ["ПС"], "terms_used": []},
    ]}
    assert [i.where for i in check_terms_order(layer)] == ["a"]


@needs_domain
def test_domain_starters_present():
    misc = json.loads((DOMAIN / "geology" / "misconceptions.json").read_text(encoding="utf-8"))
    assert len(misc) == 20 and all(m["id"].startswith("misc.") for m in misc)
