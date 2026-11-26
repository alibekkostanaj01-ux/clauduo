"""Машинные проверки. Факты проверяет код, а не модель.

- схема: файлы проверяются по JSON-схемам организаторов (schemas/*.schema.json);
- цитата: дословно на указанной странице, без учёта пробелов, переносов и регистра (правило организаторов);
- числа: каждое число в утверждении есть на его странице (или в цитате);
- порядок терминов: термин не используется раньше узла, где он введён;
- покрытие: каждая единица главы → узел или причина («непоказуемо» — не причина).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import jsonschema

from .mdbook import Book, find_quote_pages, load_book, norm, numbers, quote_on_page
from .paths import SCHEMAS

SCHEMA_FILES = {
    "node": "node.schema.json",
    "coverage": "coverage.schema.json",
    "routing": "routing.schema.json",
    "task": "task.schema.json",
    "illustration": "illustration.schema.json",
    "passport": "passport.schema.json",
    "review_package": "review_package.schema.json",
    "review_output": "review_output.schema.json",
}


@dataclass
class Issue:
    where: str      # id узла / утверждения / единицы
    check: str      # schema | quote | numbers | terms | coverage | links
    message: str
    hint: str = ""

    def as_dict(self) -> dict:
        return {"where": self.where, "check": self.check, "message": self.message, "hint": self.hint}


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS / SCHEMA_FILES[name]).read_text(encoding="utf-8"))


def check_schema(obj: dict, name: str) -> list[Issue]:
    validator = jsonschema.Draft202012Validator(load_schema(name))
    issues = []
    for e in sorted(validator.iter_errors(obj), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in e.path) or "(корень)"
        issues.append(Issue(path, "schema", e.message[:300]))
    return issues


def check_claims(layer: dict, book: Book, chapter_pages: list[int] | None = None) -> list[Issue]:
    """Цитаты и числа во всех утверждениях слоя."""
    issues: list[Issue] = []
    for node in layer.get("nodes", []):
        for c in node.get("claims", []):
            cid, page, quote = c.get("id", "?"), c.get("page_pdf"), c.get("quote", "")
            if not c.get("id", "").startswith(node.get("id", "") + "#c"):
                issues.append(Issue(cid, "schema", f"id утверждения должен начинаться с «{node.get('id')}#c»"))
            if not quote_on_page(quote, book, page):
                found = find_quote_pages(quote, book, chapter_pages)
                if found:
                    hint = f"цитата есть на стр. {found}"
                else:
                    soft = find_quote_pages(quote, book, chapter_pages, soft=True)
                    hint = (f"почти совпадает на стр. {soft} (ё/тире/кавычки/LaTeX) — взять точный текст из md"
                            if soft else "цитаты нет в главе — модель перефразировала или выдумала")
                issues.append(Issue(cid, "quote", f"цитата не найдена на стр. {page}", hint))
            # числа утверждения должны быть на странице утверждения или в его цитате
            src = norm(book.pages.get(page, "")) + norm(quote)
            src_nums = numbers(book.pages.get(page, "")) | numbers(quote)
            for n in numbers(c.get("text", "")):
                if n not in src_nums and n.replace(".", ",") not in src:
                    issues.append(Issue(cid, "numbers", f"числа «{n}» нет на стр. {page}"))
    return issues


def check_field_numbers(layer: dict, book: Book) -> list[Issue]:
    """ТЗ: каждое число в тексте узла и курса есть в исходной единице главы.
    Источник узла — его страницы и его утверждения (с цитатами). Гипотезы и ИТ-аналогия не проверяются."""
    issues = []
    for n in layer.get("nodes", []):
        src: set[str] = set()
        for p in n.get("pages", []):
            src |= numbers(book.pages.get(p, ""))
        for c in n.get("claims", []):
            src |= numbers(c.get("text", "")) | numbers(c.get("quote", ""))
        texts = {f"layers.{k}": v.get("text", "") for k, v in n.get("layers", {}).items() if isinstance(v, dict)}
        for i, d in enumerate(n.get("layers", {}).get("deeper", [])):
            texts[f"layers.deeper[{i}]"] = d.get("text", "")
        dm = n.get("data_meaning") or {}
        for k in ("from_chapter", "breaks"):
            if dm.get(k):
                texts[f"data_meaning.{k}"] = dm[k].get("text", "")
        for i, e in enumerate(n.get("worked_examples", [])):
            texts[f"worked_examples[{i}]"] = " ".join([e.get("condition", ""), " ".join(e.get("steps", [])), e.get("result", "")])
        for where, t in texts.items():
            bad = sorted(x for x in numbers(t) if x not in src)
            if bad:
                issues.append(Issue(f"{n.get('id')}:{where}", "numbers", f"числа {', '.join(bad)} нет в источнике узла"))
    return issues


def check_terms_order(layer: dict) -> list[Issue]:
    """Термин из terms_used не должен встречаться раньше узла, который его вводит (terms_introduced)."""
    nodes = sorted(layer.get("nodes", []), key=lambda n: n.get("order", 0))
    introduced_at: dict[str, int] = {}
    for n in nodes:
        for t in n.get("terms_introduced", []):
            introduced_at.setdefault(norm(t), n.get("order", 0))
    issues = []
    for n in nodes:
        for t in n.get("terms_used", []):
            at = introduced_at.get(norm(t))
            if at is not None and at > n.get("order", 0):
                issues.append(Issue(n["id"], "terms", f"термин «{t}» используется раньше узла, где он вводится (order {at})"))
    return issues


def check_links(layer: dict, misconception_ids: set[str]) -> list[Issue]:
    ids = {n["id"] for n in layer.get("nodes", [])}
    issues = []
    for n in layer.get("nodes", []):
        for l in n.get("links", []):
            t = l.get("target", "")
            if t.startswith("misc."):
                if t not in misconception_ids:
                    issues.append(Issue(n["id"], "links", f"неизвестное заблуждение {t}"))
            elif t.startswith("data."):
                continue
            elif t not in ids:
                issues.append(Issue(n["id"], "links", f"ссылка на несуществующий узел {t}"))
        m = n.get("misconception")
        if m and m.get("ref") and m["ref"] not in misconception_ids:
            issues.append(Issue(n["id"], "links", f"неизвестное заблуждение {m['ref']}"))
    return issues


BAD_REASONS = {"непоказуемо", "непоказуемое", "не показуемо", "невозможно показать"}


def check_coverage(coverage: dict, layer: dict | None = None) -> list[Issue]:
    node_ids = {n["id"] for n in (layer or {}).get("nodes", [])}
    issues = []
    for r in coverage.get("rows", []):
        if not r.get("node_ids"):
            reason = (r.get("reason") or "").strip().lower()
            if not reason:
                issues.append(Issue(r["unit_id"], "coverage", "единица не покрыта и нет причины"))
            elif reason in BAD_REASONS:
                issues.append(Issue(r["unit_id"], "coverage", "«непоказуемо» — не причина (ТЗ)"))
        elif node_ids:
            for nid in r["node_ids"]:
                if nid not in node_ids:
                    issues.append(Issue(r["unit_id"], "coverage", f"ссылка на несуществующий узел {nid}"))
    return issues


def coverage_share(coverage: dict) -> float:
    rows = coverage.get("rows", [])
    if not rows:
        return 0.0
    ok = sum(1 for r in rows if r.get("node_ids") or (r.get("reason") or "").strip().lower() not in ("", *BAD_REASONS))
    return ok / len(rows)


def validate_layer(layer_path: Path, book_md: Path, misconceptions_path: Path, chapter_pages: list[int] | None = None) -> list[Issue]:
    layer = json.loads(Path(layer_path).read_text(encoding="utf-8"))
    misc = {m["id"] for m in json.loads(Path(misconceptions_path).read_text(encoding="utf-8"))}
    book = load_book(Path(book_md))
    return (check_schema(layer, "node") + check_claims(layer, book, chapter_pages)
            + check_field_numbers(layer, book) + check_terms_order(layer) + check_links(layer, misc))
