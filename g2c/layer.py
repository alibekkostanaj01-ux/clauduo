"""Учебный слой главы: 5 проходов LLM, после каждого — проверка кодом.

1. claims   — утверждения, примеры, задачи по порциям страниц; цитаты и числа проверяет код,
              ошибки (которые код не исправил сам) уходят модели на исправление, неисправленное отбрасывается.
2. plan     — план узлов: группировка утверждений, порядок, термины, предпосылки.
              Порядок «термин не раньше объяснения» досчитывает код (топологическая сортировка).
3. prereq   — узлы-предпосылки из короткого чтения для терминов, которые глава не объясняет.
4. fields   — поля узлов: механизм → практика → нюансы, данные, ИТ-аналогия, заблуждение, линза.
5. coverage — каждая единица главы → узел или причина.
Итог — work/<глава>/layer.json (node.schema.json), coverage.json (coverage.schema.json), issues.json.
"""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from string import Template
from typing import Callable

import networkx as nx

from .ingest import PREREQ_CARD_PAGE, Unit, prereq_card_text, prereq_unit_list
from .layer_models import (CoverageOut, NodeFieldsOut, NodePlan, Pass1Out, PrereqOut, QuoteFixOut)
from .llm import generate
from .mdbook import Book, find_quote_pages, load_book, norm, numbers, quote_on_page
from .paths import DOMAIN, ROOT, Chapter, parse_pages
from .settings import config, model_for
from .validate import (Issue, check_claims, check_coverage, check_field_numbers, check_links, check_schema,
                       check_terms_order)

PROMPTS = ROOT / "prompts"


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --- промпты --------------------------------------------------------------------
def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def make_prompt(name: str, ch: Chapter, **kw) -> str:
    dom = DOMAIN / ch.domain
    common = Template(_read(PROMPTS / "_common.md")).safe_substitute(domain_notes=_read(dom / "layer_notes.md"))
    return Template(_read(PROMPTS / f"{name}.md")).safe_substitute(common=common, chapter_title=ch.title, **kw)


def unit_line(u: Unit) -> str:
    kind = "задача" if u.kind == "task" else u.kind
    head = f"[{u.id} | стр. {u.page_pdf} | {kind}]"
    if u.ocr_suspect:
        return f"{head} [брак распознавания] {u.text}"
    out = f"{head} {u.text}" if u.text else head
    if u.model_description:
        out += f"\n[{u.id} | стр. {u.page_pdf} | описание рисунка моделью] {u.model_description}"
    return out


def batches_by_chars(units: list[Unit], limit: int) -> list[list[Unit]]:
    """Порции по целым страницам, не больше limit символов (страница целиком, даже если длиннее)."""
    pages: dict[int, list[Unit]] = {}
    for u in units:
        pages.setdefault(u.page_pdf, []).append(u)
    out, cur, size = [], [], 0
    for p in sorted(pages):
        s = sum(len(unit_line(u)) for u in pages[p])
        if cur and size + s > limit:
            out.append(cur)
            cur, size = [], 0
        cur.extend(pages[p])
        size += s
    if cur:
        out.append(cur)
    return out


# --- проверка и починка утверждений ---------------------------------------------------
class ClaimStore:
    """Все черновые утверждения главы с временными номерами k1, k2, …"""

    def __init__(self, ch: Chapter, units: list[Unit], allowed_pages: list[int]):
        self.ch = ch
        book = load_book(ch.md_path)
        card = prereq_card_text(ch)
        if card:  # карточка-предпосылка — «страница 0»: цитаты из неё сверяются так же строго
            book = Book(path=book.path, pages={**book.pages, PREREQ_CARD_PAGE: card})
            allowed_pages = allowed_pages + [PREREQ_CARD_PAGE]
        self.book = book
        self.units = {u.id: u for u in units}
        self.order = {u.id: i for i, u in enumerate(units)}
        self.allowed = list(allowed_pages)
        self.claims: dict[str, dict] = {}
        self.dropped: list[dict] = []
        self._n = 0

    def add(self, c: dict) -> str:
        self._n += 1
        ref = f"k{self._n}"
        self.claims[ref] = c
        return ref

    def _locate_unit(self, c: dict) -> None:
        """Поправить unit_id/source_kind по фактическому месту цитаты."""
        q = norm(c["quote"])
        cands = [u for u in self.units.values() if u.page_pdf == c["page_pdf"]]
        for u in cands:
            if q and q in norm(u.text):
                if c["unit_id"] != u.id:
                    c["unit_id"] = u.id
                if c["source_kind"] == "figure_description_model":
                    c["source_kind"] = "figure_caption" if u.kind == "figure" else ("formula" if u.kind == "formula" else "text")
                return
            if q and u.model_description and q in norm(u.model_description):
                c["unit_id"] = u.id
                c["source_kind"] = "figure_description_model"
                return

    def check(self, ref: str) -> list[str]:
        """Проверить утверждение; простые ошибки исправить на месте. Вернуть список оставшихся ошибок."""
        c = self.claims[ref]
        errs = []
        c["quote"] = (c.get("quote") or "").strip().strip("«»\"„“”'").strip()  # модели любят обрамлять цитату кавычками
        if len(c.get("quote", "")) < 8:
            errs.append("цитата короче 8 символов")
        elif not quote_on_page(c["quote"], self.book, c["page_pdf"]):
            found = find_quote_pages(c["quote"], self.book, self.allowed)
            if found:
                c["page_pdf"] = found[0]  # цитата верная, страница указана не та — исправляем сами
            else:
                errs.append(f"цитата не найдена на стр. {c['page_pdf']}")
        if not errs:
            self._locate_unit(c)
            page_text = self.book.pages.get(c["page_pdf"], "")
            src = numbers(page_text) | numbers(c["quote"])
            bad = [n for n in numbers(c["text"]) if n not in src]
            if bad:
                errs.append(f"в тексте утверждения числа {', '.join(bad)}, которых нет на стр. {c['page_pdf']}")
        return errs

    def section(self, c: dict) -> str:
        u = self.units.get(c["unit_id"])
        return (u.heading_path[-1] if u and u.heading_path else "")[:60]

    def sort_key(self, ref: str) -> tuple:
        c = self.claims[ref]
        return (self.order.get(c["unit_id"], 10 ** 6), int(ref[1:]))


def repair_claims(store: ClaimStore, refs: list[str], ch: Chapter, workers: int, stage: str) -> list[Issue]:
    """Проверить утверждения, ошибки отправить модели на исправление (одна попытка), остальное отбросить."""
    errors = {ref: store.check(ref) for ref in refs}
    errors = {r: e for r, e in errors.items() if e}
    log(f"  проверка: {len(refs) - len(errors)} из {len(refs)} верны сразу, на исправление {len(errors)}")
    if errors:
        items = sorted(errors, key=store.sort_key)
        chunks = [items[i:i + 25] for i in range(0, len(items), 25)]

        def fix(chunk: list[str]):
            pages = set()
            lines = []
            for r in chunk:
                c = store.claims[r]
                pages.update({c["page_pdf"] - 1, c["page_pdf"], c["page_pdf"] + 1})
                lines.append(f"{r} | стр. {c['page_pdf']} | ошибка: {'; '.join(errors[r])}\n  утверждение: {c['text']}\n  цитата: {c['quote']}")
            pages_text = "\n\n".join(f"=== стр. {p} ===\n{store.book.pages[p]}" for p in sorted(pages) if p in store.allowed)
            prompt = make_prompt("layer_quote_fix", ch, errors="\n".join(lines), pages_text=pages_text)
            out, _ = generate(prompt, role="structurer", schema=QuoteFixOut, stage="layer", chapter=ch.id, node=stage + ".quote_fix")
            return out

        with ThreadPoolExecutor(workers) as ex:
            for out in ex.map(fix, chunks):
                for f in out.fixes:
                    c = store.claims.get(f.ref)
                    if c is None:
                        continue
                    if f.action == "drop":
                        c["_drop"] = "модель: опоры в главе нет"
                        continue
                    if f.quote:
                        c["quote"] = f.quote
                    if f.page_pdf:
                        c["page_pdf"] = f.page_pdf
                    if f.text:
                        c["text"] = f.text
    issues = []
    for ref in refs:
        c = store.claims[ref]
        errs = [c["_drop"]] if c.get("_drop") else store.check(ref)
        if errs:
            store.dropped.append({"ref": ref, **c, "errors": errs})
            del store.claims[ref]
            issues.append(Issue(ref, "quote", "утверждение отброшено: " + "; ".join(errs), c["text"][:120]))
    log(f"  после исправления: отброшено {len(issues)}")
    return issues


# --- проход 1 ---------------------------------------------------------------------------
def pass_claims(ch: Chapter, units: list[Unit], store: ClaimStore, workers: int):
    batches = batches_by_chars([u for u in units], limit=9000)
    log(f"Проход 1 — утверждения: {len(units)} единиц, {len(batches)} порций")

    def run(b: list[Unit]):
        pages = f"{b[0].page_pdf}–{b[-1].page_pdf}"
        prompt = make_prompt("layer_claims", ch, pages=pages, units="\n\n".join(unit_line(u) for u in b))
        out, u = generate(prompt, role="structurer", schema=Pass1Out, stage="layer", chapter=ch.id, node=f"claims.{pages}")
        log(f"  стр. {pages}: {len(out.claims)} утв., {len(out.worked_examples)} примеров, {len(out.book_tasks)} задач ({u.seconds} с)")
        return out

    with ThreadPoolExecutor(workers) as ex:
        outs = list(ex.map(run, batches))
    refs, examples, tasks = [], [], []
    for out in outs:
        for c in out.claims:
            refs.append(store.add(c.model_dump()))
        examples += [e.model_dump() for e in out.worked_examples]
        tasks += [t.model_dump() for t in out.book_tasks]
    issues = repair_claims(store, refs, ch, workers, "claims")
    return examples, tasks, issues


# --- проход 2 ---------------------------------------------------------------------------
def _section_key(store: ClaimStore, c: dict) -> str:
    """Раздел книги для утверждения: ближайший заголовок с § или номером, иначе последний заголовок."""
    u = store.units.get(c["unit_id"])
    hp = (u.heading_path if u else []) or [""]
    return next((h for h in reversed(hp) if re.search(r"§|^\s*\d+(\.\d+)+\s", h)), hp[-1])[:80]


def plan_chunks(store: ClaimStore, refs: list[str], target: int = 70, max_size: int = 110) -> list[list[str]]:
    """Порции для плана: по разделам книги; маленькие разделы склеиваются, большие режутся.
    Один большой запрос на всю главу давал ответ в десятки тысяч токенов и упирался в таймауты."""
    sections: list[list[str]] = []
    last = None
    for r in refs:
        k = _section_key(store, store.claims[r])
        if k != last:
            sections.append([])
            last = k
        sections[-1].append(r)
    chunks: list[list[str]] = []
    for s in sections:
        while len(s) > max_size:
            chunks.append(s[:target])
            s = s[target:]
        if chunks and len(chunks[-1]) + len(s) <= target:
            chunks[-1].extend(s)
        else:
            chunks.append(list(s))
    return chunks


def pass_plan(ch: Chapter, store: ClaimStore, workers: int = 4) -> tuple[list[dict], list[Issue]]:
    refs = sorted(store.claims, key=store.sort_key)
    types = _read(DOMAIN / ch.domain / "difficulty_types.md")
    chunks = plan_chunks(store, refs)
    log(f"Проход 2 — план узлов из {len(refs)} утверждений, {len(chunks)} порций по разделам")

    def run(chunk: list[str]):
        lines = [f"{r} | стр. {store.claims[r]['page_pdf']} | {store.section(store.claims[r])} | {store.claims[r]['text']}" for r in chunk]
        # целевое число узлов: ~1 узел на 10–14 утверждений (иначе модель дробит главу на десятки мелких шагов)
        n = len(chunk)
        prompt = make_prompt("layer_plan", ch, claims="\n".join(lines), difficulty_types=types,
                             chapter_types=", ".join(ch.difficulty) or "любые",
                             n_claims=str(n), nodes_min=str(max(1, round(n / 14))), nodes_max=str(max(2, round(n / 10))))
        out, u = generate(prompt, role="structurer", schema=NodePlan, stage="layer", chapter=ch.id,
                          node=f"plan.{chunk[0]}-{chunk[-1]}")
        log(f"  {_section_key(store, store.claims[chunk[0]])[:50]}: {len(chunk)} утв. → {len(out.nodes)} узлов ({u.seconds} с)")
        return out

    with ThreadPoolExecutor(workers) as ex:
        plans = list(ex.map(run, chunks))
    all_nodes = [n for p in plans for n in p.nodes]
    log(f"  узлов: {len(all_nodes)}")

    issues = []
    nodes, seen_slugs, assigned = [], set(), {}
    for n in all_nodes:
        d = n.model_dump()
        slug = re.sub(r"[^a-z0-9_]+", "_", d["slug"].lower()).strip("_") or f"node_{len(nodes) + 1}"
        while slug in seen_slugs:
            slug += "_2"
        seen_slugs.add(slug)
        d["slug"] = slug
        d["claim_refs"] = [r for r in d["claim_refs"] if r in store.claims and r not in assigned]
        for r in d["claim_refs"]:
            assigned[r] = slug
        nodes.append(d)
    # потерянные утверждения — в узел соседнего по тексту утверждения
    lost = [r for r in refs if r not in assigned]
    for r in lost:
        i = refs.index(r)
        neighbour = next((assigned[refs[j]] for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(refs))) if refs[j] in assigned), None)
        if neighbour:
            node = next(n for n in nodes if n["slug"] == neighbour)
            node["claim_refs"].append(r)
            assigned[r] = neighbour
    if lost:
        issues.append(Issue("plan", "coverage", f"{len(lost)} утверждений не вошли в план — добавлены к соседним узлам"))
    nodes = [n for n in nodes if n["claim_refs"]]
    for n in nodes:
        n["claim_refs"].sort(key=store.sort_key)
    return nodes, issues


def order_nodes(nodes: list[dict], store: "ClaimStore | None" = None) -> list[dict]:
    """Порядок: предпосылки и «термин не раньше объяснения»; при прочих равных — порядок книги
    (по первому утверждению узла), чтобы разделы главы не перемешивались; без store — порядок модели."""
    if store is not None:
        first = {n["slug"]: min((store.sort_key(r) for r in n["claim_refs"] if r in store.claims), default=(10 ** 9, 0)) for n in nodes}
        ranked = sorted(nodes, key=lambda n: first[n["slug"]])
        pos = {n["slug"]: i for i, n in enumerate(ranked)}
    else:
        pos = {n["slug"]: i for i, n in enumerate(nodes)}
    intro = {}
    for n in nodes:
        for t in n["terms_introduced"]:
            intro.setdefault(norm(t), n["slug"])
    g = nx.DiGraph()
    g.add_nodes_from(pos)
    for n in nodes:
        for p in n.get("prerequisites", []):
            if p in pos and p != n["slug"]:
                g.add_edge(p, n["slug"])
    for n in nodes:  # рёбра по терминам — только если не создают цикл
        for t in n["terms_used"]:
            src = intro.get(norm(t))
            if src and src != n["slug"] and not g.has_edge(src, n["slug"]):
                g.add_edge(src, n["slug"])
                if not nx.is_directed_acyclic_graph(g):
                    g.remove_edge(src, n["slug"])
    while not nx.is_directed_acyclic_graph(g):  # циклы из предпосылок модели — рвём последнее ребро цикла
        cyc = nx.find_cycle(g)
        g.remove_edge(*cyc[-1])
    order = list(nx.lexicographical_topological_sort(g, key=lambda s: pos[s]))
    by = {n["slug"]: n for n in nodes}
    return [by[s] for s in order]


# --- проход 3: предпосылки ---------------------------------------------------------------
def _term_keys(term: str) -> list[str]:
    abbr = re.findall(r"\b[А-ЯЁA-Z]{2,5}\b", term)
    words = [w for w in re.findall(r"[а-яёa-z]{5,}", term.lower())]
    return abbr + [w[:6] for w in words[:2]]


def pass_prereq(ch: Chapter, nodes: list[dict], store: ClaimStore, workers: int) -> tuple[list[dict], list[Issue]]:
    if not (ch.prereq_pages or ch.prereq_file):
        return [], []
    introduced = {norm(t) for n in nodes for t in n["terms_introduced"]}
    counts: dict[str, int] = {}
    for n in nodes:
        for t in n["terms_used"]:
            if norm(t) not in introduced:
                counts[t] = counts.get(t, 0) + 1
    # в предпосылки — самые нужные: чаще всего используемые термины, не больше 10
    missing = sorted(counts, key=lambda t: -counts[t])[:10]
    if not missing:
        return [], []
    pre_units = prereq_unit_list(ch)
    store.units.update({u.id: u for u in pre_units})
    base = max(store.order.values(), default=0)
    for i, u in enumerate(pre_units):
        store.order[u.id] = -len(pre_units) + i  # предпосылки идут раньше главы
    # единицы короткого чтения, где встречаются термины
    picked: dict[str, Unit] = {}
    for t in missing:
        keys = _term_keys(t)
        scored = sorted(((sum(norm(k) in norm(u.text) for k in keys), u) for u in pre_units
                         if u.kind in ("paragraph", "formula", "table") and not u.ocr_suspect),
                        key=lambda x: -x[0])
        for s, u in scored[:4]:
            if s > 0:
                picked[u.id] = u
    if not picked:
        return [], [Issue("prereq", "terms", f"для терминов нет опоры в коротком чтении: {', '.join(missing)}")]
    units = sorted(picked.values(), key=lambda u: (u.page_pdf, u.id))[:70]
    log(f"Проход 3 — предпосылки: {len(missing)} терминов без объяснения, {len(units)} единиц короткого чтения")
    prompt = make_prompt("layer_prereq", ch, terms="\n".join(f"- {t}" for t in missing),
                         units="\n\n".join(unit_line(u) for u in units))
    out, u = generate(prompt, role="structurer", schema=PrereqOut, stage="layer", chapter=ch.id, node="prereq")
    log(f"  узлов-предпосылок: {len(out.nodes)} ({u.seconds} с)")
    pre_nodes, refs_all = [], []
    for pn in out.nodes:
        refs = [store.add(c.model_dump()) for c in pn.claims]
        refs_all += refs
        slug = re.sub(r"[^a-z0-9_]+", "_", pn.slug.lower()).strip("_")
        if not slug.startswith("pre_"):
            slug = "pre_" + slug
        pre_nodes.append({"slug": slug, "title": pn.title, "book_title": pn.term, "claim_refs": refs,
                          "why_here": f"Предпосылка: глава пользуется понятием «{pn.term}», объяснённым в предыдущих разделах книги.",
                          "difficulty_primary": "И", "difficulty_secondary": "", "threshold": False,
                          "terms_introduced": [pn.term], "terms_used": [], "prerequisites": [],
                          "_prefill": {"main": pn.main, "mechanism": pn.mechanism}})
    issues = repair_claims(store, refs_all, ch, workers, "prereq")
    for n in pre_nodes:
        n["claim_refs"] = [r for r in n["claim_refs"] if r in store.claims]
    pre_nodes = [n for n in pre_nodes if n["claim_refs"]]
    # узлы главы, которые пользуются термином, получают ссылку на предпосылку
    for pn in pre_nodes:
        t = norm(pn["terms_introduced"][0])
        for n in nodes:
            if any(norm(x) == t for x in n["terms_used"]):
                n.setdefault("prerequisites", []).append(pn["slug"])
    still = [t for t in missing if norm(t) not in {norm(p["terms_introduced"][0]) for p in pre_nodes}]
    if still:
        issues.append(Issue("prereq", "terms", f"термины без узла-предпосылки: {', '.join(still)}"))
    return pre_nodes, issues


# --- проход 4: поля -------------------------------------------------------------------
def pass_fields(ch: Chapter, nodes: list[dict], store: ClaimStore, workers: int, per_call: int = 0) -> dict[str, dict]:
    per_call = per_call or int(config()["llm"].get("per_call_fields", 6))
    misc = json.loads((DOMAIN / ch.domain / "misconceptions.json").read_text(encoding="utf-8"))
    ds = json.loads(_read(DOMAIN / ch.domain / "datasets.json") or "[]")
    groups = [nodes[i:i + per_call] for i in range(0, len(nodes), per_call)]
    log(f"Проход 4 — поля узлов: {len(nodes)} узлов, {len(groups)} вызовов")

    def block(n: dict) -> str:
        cl = "\n".join(f"  - [{store.claims[r]['kind']}, стр. {store.claims[r]['page_pdf']}] {store.claims[r]['text']}" for r in n["claim_refs"])
        return f"### {n['slug']} — {n['title']} ({n['book_title']}); тип {n['difficulty_primary']}\n{cl}"

    def run(g: list[dict]):
        prompt = make_prompt("layer_fields", ch, nodes="\n\n".join(block(n) for n in g),
                             misconceptions="\n".join(f"{m['id']}: {m['text']}" for m in misc),
                             datasets="; ".join(f"{d['id']} ({d['text']})" for d in ds))
        out, u = generate(prompt, role="writer", schema=NodeFieldsOut, stage="layer", chapter=ch.id,
                          node="fields." + ",".join(n["slug"] for n in g))
        log(f"  {', '.join(n['slug'] for n in g)} ({u.seconds} с)")
        return out

    fields: dict[str, dict] = {}
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(run, groups):
            for f in out.nodes:
                fields[f.slug] = f.model_dump()
    return fields


# --- проход 5: покрытие ------------------------------------------------------------------
def pass_coverage(ch: Chapter, units: list[Unit], nodes: list[dict], store: ClaimStore,
                  examples: list[dict], tasks: list[dict], use_llm: bool = True) -> tuple[dict[str, list[str]], dict[str, str]]:
    node_of_unit: dict[str, list[str]] = {}
    for n in nodes:
        for r in n["claim_refs"]:
            node_of_unit.setdefault(store.claims[r]["unit_id"], [])
            if n["slug"] not in node_of_unit[store.claims[r]["unit_id"]]:
                node_of_unit[store.claims[r]["unit_id"]].append(n["slug"])
    order = [u.id for u in units]

    def nearest(uid: str) -> str | None:
        i = order.index(uid)
        for j in list(range(i - 1, -1, -1)) + list(range(i + 1, len(order))):
            if node_of_unit.get(order[j]):
                return node_of_unit[order[j]][0]
        return None

    for e in examples:
        for uid in e["unit_ids"]:
            if uid in order and not node_of_unit.get(uid):
                s = nearest(uid)
                if s:
                    node_of_unit[uid] = [s]
    for t in tasks:
        if t["unit_id"] in order and not node_of_unit.get(t["unit_id"]):
            s = nearest(t["unit_id"])
            if s:
                node_of_unit[t["unit_id"]] = [s]
    # заголовок → первый узел своего раздела
    for i, u in enumerate(units):
        if u.kind == "heading" and not node_of_unit.get(u.id):
            nxt = next((node_of_unit[x.id][0] for x in units[i + 1:] if node_of_unit.get(x.id)), None)
            if nxt:
                node_of_unit[u.id] = [nxt]
    left = [u for u in units if not node_of_unit.get(u.id)]
    reasons: dict[str, str] = {}
    if left and use_llm:
        log(f"Проход 5 — покрытие: {len(left)} единиц без узла")
        prompt = make_prompt("layer_coverage", ch, nodes="\n".join(f"{n['slug']}: {n['title']}" for n in nodes),
                             units="\n\n".join(unit_line(u) for u in left))
        out, _ = generate(prompt, role="fast", schema=CoverageOut, stage="layer", chapter=ch.id, node="coverage")
        slugs = {n["slug"] for n in nodes}
        for d in out.decisions:
            if d.node_slug in slugs:
                node_of_unit[d.unit_id] = [d.node_slug]
            elif d.reason.strip():
                reasons[d.unit_id] = d.reason.strip()
    return node_of_unit, reasons


# --- сборка ------------------------------------------------------------------------------
def F(text: str, by_model: bool = True, hypothesis: bool | None = None) -> dict:
    d = {"text": text or "", "by_model": by_model}
    if hypothesis is not None:
        d["hypothesis"] = hypothesis
    return d


def assemble(ch: Chapter, nodes: list[dict], fields: dict[str, dict], store: ClaimStore,
             examples: list[dict], tasks: list[dict], node_of_unit: dict[str, list[str]]) -> dict:
    misc_ids = {m["id"] for m in json.loads((DOMAIN / ch.domain / "misconceptions.json").read_text(encoding="utf-8"))}
    slug2id = {n["slug"]: f"{ch.prefix}.{n['slug']}" for n in nodes}
    out_nodes = []
    for i, n in enumerate(nodes, 1):
        nid = slug2id[n["slug"]]
        f = fields.get(n["slug"], {})
        pre = n.get("_prefill", {})
        claims, pages, needs_scan = [], set(), []
        for k, r in enumerate(n["claim_refs"], 1):
            c = store.claims[r]
            u = store.units.get(c["unit_id"])
            claim = {"id": f"{nid}#c{k}", "text": c["text"], "kind": c["kind"], "page_pdf": c["page_pdf"],
                     "page_print": None, "quote": c["quote"], "source_kind": c["source_kind"],
                     "by_model": c["source_kind"] == "figure_description_model", "unit_id": c["unit_id"]}
            if u and u.kind == "figure":
                claim["needs_scan"] = True
                needs_scan.append({"page": c["page_pdf"], "what": (u.text or "рисунок")[:120]})
            claims.append(claim)
            pages.add(c["page_pdf"])
        unit_ids = [uid for uid, ss in node_of_unit.items() if n["slug"] in ss]
        for uid in unit_ids:
            u = store.units.get(uid)
            if u and u.ocr_suspect:
                needs_scan.append({"page": u.page_pdf, "what": f"брак распознавания в {uid}: сверить со сканом"})
        links = [{"type": "prerequisite", "target": slug2id[p]} for p in dict.fromkeys(n.get("prerequisites", [])) if p in slug2id]
        mref = f.get("misconception_ref") or None
        if mref and mref not in misc_ids:
            mref = None
        if mref:
            links.append({"type": "refutes_misconception", "target": mref})
        for d in f.get("to_data", []) or []:
            if d.startswith("data."):
                links.append({"type": "to_data", "target": d})
        ineff = f.get("inefficiency")
        dod = f.get("decision_on_data")
        node = {
            "id": nid, "order": i, "title": n["title"], "book_title": n.get("book_title", ""),
            "why_here": F(n.get("why_here", "")),
            "difficulty": {"primary": n["difficulty_primary"],
                           "secondary": n["difficulty_secondary"] if n.get("difficulty_secondary") in {"М", "В", "И", "Ф", "П", "Пр", "Н", "Р"} else None},
            "threshold": bool(n.get("threshold")),
            "pages": sorted(pages),
            "layers": {
                "topic": F(f.get("topic", n["title"])),
                "main": F(f.get("main") or pre.get("main", "")),
                "mechanism": F(f.get("mechanism") or pre.get("mechanism", "")),
                "practice": F(f.get("practice", "")),
                "nuances": F(f.get("nuances", "")),
                "deeper": [F(x) for x in f.get("deeper", []) if x],
            },
            "claims": claims,
            "links": links,
            "misconception": ({"ref": mref, "text": f.get("misconception_text", ""), "by_model": True}
                              if f.get("misconception_text") else None),
            "data_meaning": {"from_chapter": F(f.get("data_from_chapter", ""), hypothesis=False),
                             "breaks": F(f.get("data_breaks", ""), hypothesis=False),
                             "hypotheses": [F(h, hypothesis=True) for h in f.get("data_hypotheses", []) if h]},
            "it_analogy": {"text": f.get("it_analogy", ""), "breaks_where": f.get("it_breaks_where", ""), "by_model": True},
            "now_practice": F(f["now_practice"]) if f.get("now_practice") else None,
            "inefficiency_lens": ({**ineff, "by_model": True} if ineff and any(ineff.values()) else None),
            "terms_introduced": n["terms_introduced"],
            "terms_used": n["terms_used"],
            "needs_scan": needs_scan,
            "decision_on_data": ({"features": dod.get("features") or [], "rule": dod["rule"], "conclusion": dod["conclusion"],
                                  **({"counterexample": dod["counterexample"]} if dod.get("counterexample") else {})}
                                 if dod and dod.get("rule") and dod.get("conclusion") else None),
            "worked_examples": [],
            "book_tasks": [],
            "unit_ids": unit_ids,
        }
        out_nodes.append(node)
    by_slug = {n["slug"]: o for n, o in zip(nodes, out_nodes)}
    for e in examples:
        s = next((node_of_unit[u][0] for u in e["unit_ids"] if node_of_unit.get(u)), None)
        if s in by_slug:
            by_slug[s]["worked_examples"].append({"page_pdf": e["page_pdf"], "condition": e["condition"], "steps": e["steps"],
                                                  "result": e["result"], "figure": e["figure"] or None})
    for t in tasks:
        s = (node_of_unit.get(t["unit_id"]) or [None])[0]
        if s in by_slug:
            by_slug[s]["book_tasks"].append({"number": t["number"], "page_pdf": t["page_pdf"], "text": t["text"], "figure": t["figure"] or None})
    # пороговых идей мало по определению: оставляем не больше 5 — те, на которые опирается больше всего узлов
    thr = [o for o in out_nodes if o["threshold"]]
    if len(thr) > 5:
        dependents = {o["id"]: 0 for o in out_nodes}
        for o in out_nodes:
            for l in o["links"]:
                if l["type"] == "prerequisite" and l["target"] in dependents:
                    dependents[l["target"]] += 1
        keep = {o["id"] for o in sorted(thr, key=lambda o: (-dependents[o["id"]], o["order"]))[:5]}
        for o in thr:
            o["threshold"] = o["id"] in keep
    return {"chapter": ch.id, "source": ch.book_md, "pdf_pages": [ch.pages],
            "generated_by": f"g2c layer, модель {model_for('structurer')}", "nodes": out_nodes}


def build_coverage(ch: Chapter, units: list[Unit], node_of_unit: dict[str, list[str]], reasons: dict[str, str], slug2id: dict[str, str]) -> dict:
    rows = []
    for u in units:
        rows.append({"unit_id": u.id, "kind": u.kind, "page_pdf": u.page_pdf,
                     "excerpt": u.excerpt or "(описание рисунка)",
                     "node_ids": [slug2id[s] for s in node_of_unit.get(u.id, []) if s in slug2id],
                     "reason": None if node_of_unit.get(u.id) else reasons.get(u.id)})
    return {"chapter": ch.id, "rows": rows}


# --- запуск -----------------------------------------------------------------------------
def build_layer(ch: Chapter, workers: int = 6) -> dict:
    t0 = time.time()
    wd = ch.work_dir
    units = [Unit(**d) for d in json.loads((wd / "units.json").read_text(encoding="utf-8"))]
    allowed = parse_pages(ch.pages) + parse_pages(ch.prereq_pages)
    store = ClaimStore(ch, units, allowed)
    issues: list[Issue] = []

    examples, tasks, iss = pass_claims(ch, units, store, workers)
    issues += iss
    _dump(wd / "pass1_claims.json", {"claims": store.claims, "examples": examples, "tasks": tasks, "dropped": store.dropped})

    nodes, iss = pass_plan(ch, store, workers)
    issues += iss
    pre_nodes, iss = pass_prereq(ch, nodes, store, workers)
    issues += iss
    nodes = order_nodes(pre_nodes + nodes, store)
    _dump(wd / "pass2_plan.json", nodes)

    fields = pass_fields(ch, nodes, store, workers)
    missing_fields = [n["slug"] for n in nodes if n["slug"] not in fields]
    if missing_fields:  # повтор для узлов, которые модель пропустила
        fields.update(pass_fields(ch, [n for n in nodes if n["slug"] in missing_fields], store, workers, per_call=1))
    _dump(wd / "pass4_fields.json", fields)

    node_of_unit, reasons = pass_coverage(ch, units, nodes, store, examples, tasks)
    layer = assemble(ch, nodes, fields, store, examples, tasks, node_of_unit)
    slug2id = {n["slug"]: f"{ch.prefix}.{n['slug']}" for n in nodes}
    coverage = build_coverage(ch, units, node_of_unit, reasons, slug2id)
    _dump(wd / "layer.json", layer)
    _dump(wd / "coverage.json", coverage)

    # итоговая проверка (книга + карточка-предпосылка, если есть)
    book = store.book
    allowed = store.allowed
    misc_ids = {m["id"] for m in json.loads((DOMAIN / ch.domain / "misconceptions.json").read_text(encoding="utf-8"))}
    final = (check_schema(layer, "node") + check_schema(coverage, "coverage") + check_claims(layer, book, allowed)
             + check_field_numbers(layer, book) + check_terms_order(layer) + check_links(layer, misc_ids) + check_coverage(coverage, layer))
    issues += final
    _dump(wd / "issues.json", [i.as_dict() for i in issues])
    n_claims = sum(len(n["claims"]) for n in layer["nodes"])
    covered = sum(1 for r in coverage["rows"] if r["node_ids"] or r["reason"])
    summary = {"chapter": ch.id, "nodes": len(layer["nodes"]), "prereq_nodes": len(pre_nodes), "claims": n_claims,
               "dropped_claims": len(store.dropped), "worked_examples": sum(len(n["worked_examples"]) for n in layer["nodes"]),
               "book_tasks": sum(len(n["book_tasks"]) for n in layer["nodes"]),
               "coverage": f"{covered}/{len(coverage['rows'])}", "final_issues": len(final),
               "minutes": round((time.time() - t0) / 60, 1)}
    _dump(wd / "layer_summary.json", summary)
    log(f"Готово: {summary}")
    return summary


def build_partial_layer(ch: Chapter) -> dict:
    """Собрать слой из того, что уже посчитано (утверждения, план, поля из кэша), без запросов к модели.
    Нужен, чтобы посмотреть результат, когда прогон прервался (например, кончилась квота)."""
    from .llm import CACHE_DIR

    wd = ch.work_dir
    units = [Unit(**d) for d in json.loads((wd / "units.json").read_text(encoding="utf-8"))]
    allowed = parse_pages(ch.pages) + parse_pages(ch.prereq_pages)
    store = ClaimStore(ch, units, allowed)
    p1 = json.loads((wd / "pass1_claims.json").read_text(encoding="utf-8"))
    store.claims.update(p1["claims"])
    store.units.update({u.id: u for u in prereq_unit_list(ch)})
    nodes = [n for n in json.loads((wd / "pass2_plan.json").read_text(encoding="utf-8"))]
    for n in nodes:
        n["claim_refs"] = [r for r in n["claim_refs"] if r in store.claims]
    nodes = order_nodes([n for n in nodes if n["claim_refs"]], store)
    slugs = {n["slug"] for n in nodes}
    fields: dict[str, dict] = {}
    for f in CACHE_DIR.glob("*.json"):  # поля узлов из кэша ответов
        try:
            data = json.loads(json.loads(f.read_text(encoding="utf-8"))["text"])
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("nodes"), list):
            for x in data["nodes"]:
                if isinstance(x, dict) and x.get("slug") in slugs and "mechanism" in x:
                    fields[x["slug"]] = x
    node_of_unit, reasons = pass_coverage(ch, units, nodes, store, p1["examples"], p1["tasks"], use_llm=False)
    layer = assemble(ch, nodes, fields, store, p1["examples"], p1["tasks"], node_of_unit)
    layer["generated_by"] += f" — ЧАСТИЧНЫЙ: поля есть у {len(fields)} из {len(nodes)} узлов"
    slug2id = {n["slug"]: f"{ch.prefix}.{n['slug']}" for n in nodes}
    coverage = build_coverage(ch, units, node_of_unit, reasons, slug2id)
    _dump(wd / "layer.json", layer)
    _dump(wd / "coverage.json", coverage)
    covered = sum(1 for r in coverage["rows"] if r["node_ids"] or r["reason"])
    return {"nodes": len(layer["nodes"]), "with_fields": len(fields), "claims": sum(len(n["claims"]) for n in layer["nodes"]),
            "coverage": f"{covered}/{len(coverage['rows'])}",
            "worked_examples": sum(len(n["worked_examples"]) for n in layer["nodes"]),
            "book_tasks": sum(len(n["book_tasks"]) for n in layer["nodes"])}


def _dump(p: Path, obj) -> None:
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1, default=lambda o: asdict(o) if hasattr(o, "__dataclass_fields__") else str(o)), encoding="utf-8")
