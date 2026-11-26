"""Доменный пакет из учебного слоя: глоссарий (glossary.json + glossary.md) и ИТ-мосты (it_bridges.md).

Глоссарий делает модель, человек только проверяет: статьи с "reviewed": true при пересборке не трогаются.
Определение — только по утверждениям книги (с id утверждений); en, обозначения, ударения — знание модели (by_model).
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

from pydantic import BaseModel, Field

import re

from .layer import _read, log, make_prompt
from .llm import generate
from .mdbook import norm, numbers
from .paths import DOMAIN, Chapter, parse_pages


class GlossaryEntryDraft(BaseModel):
    term: str
    abbr: str = ""
    aliases: list[str] = Field(default_factory=list)
    definition: str
    claim_ids: list[str]
    en: str = ""
    symbol: str = ""
    symbol_en: str = ""
    stress: str = ""
    say: str = ""


class GlossaryOut(BaseModel):
    entries: list[GlossaryEntryDraft]


def _key_words(term: str) -> list[str]:
    abbr = re.findall(r"\b[А-ЯЁA-Z]{2,5}\b", term)
    words = [w[:6] for w in re.findall(r"[а-яёa-z]{5,}", term.lower())][:3]
    return abbr + words


def _term_claims(term: str, node: dict, limit: int = 6) -> list[dict]:
    keys = [norm(k) for k in _key_words(term)]
    scored = sorted(node["claims"], key=lambda c: -sum(k in norm(c["text"]) for k in keys))
    return scored[:limit]


def build_glossary(chapters: list[Chapter], workers: int = 6) -> dict:
    domain = chapters[0].domain
    gpath = DOMAIN / domain / "glossary.json"
    existing = json.loads(gpath.read_text(encoding="utf-8")) if gpath.exists() else []
    reviewed = {norm(e["term"]): e for e in existing if e.get("reviewed")}

    items: list[tuple[str, dict, Chapter]] = []
    seen = set(reviewed)
    # черновые статьи «без модели» модель переписывает (не считаются готовыми)
    seen = {norm(e["term"]) for e in existing if e.get("reviewed") or e.get("by_model")}
    for ch in chapters:
        layer = json.loads((ch.work_dir / "layer.json").read_text(encoding="utf-8"))
        for n in layer["nodes"]:
            for t in n.get("terms_introduced", []):
                if norm(t) not in seen:
                    seen.add(norm(t))
                    items.append((t, n, ch))
    log(f"Глоссарий: {len(items)} новых терминов, {len(reviewed)} проверенных статей сохраняются")
    if not items:
        return {"terms": len(existing)}

    pron = _read(DOMAIN / domain / "pronunciation.md")[:3000]
    claim_index = {}
    groups = [items[i:i + 25] for i in range(0, len(items), 25)]

    def run(group):
        blocks = []
        for t, n, ch in group:
            cl = _term_claims(t, n)
            for c in cl:
                claim_index[c["id"]] = (c, ch)
            blocks.append(f"### {t}  (узел {n['id']})\n" + "\n".join(f"  - {c['id']} [стр. {c['page_pdf']}]: {c['text']}" for c in cl))
        prompt = make_prompt("glossary", group[0][2], pronunciation=pron, terms="\n\n".join(blocks))
        out, u = generate(prompt, role="fast", schema=GlossaryOut, stage="layer", chapter=group[0][2].id, node="glossary")
        log(f"  статей: {len(out.entries)} ({u.seconds} с)")
        return out

    new_entries = []
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(run, groups):
            for e in out.entries:
                d = e.model_dump()
                ok_ids = [cid for cid in d["claim_ids"] if cid in claim_index]
                src_nums = set()
                for cid in ok_ids:
                    c, _ = claim_index[cid]
                    src_nums |= numbers(c["text"]) | numbers(c["quote"])
                bad = sorted(x for x in numbers(d["definition"]) if x not in src_nums)
                pages = sorted({claim_index[cid][0]["page_pdf"] for cid in ok_ids})
                chs = sorted({claim_index[cid][1].id for cid in ok_ids})
                new_entries.append({
                    "term": d["term"], "abbr": d["abbr"], "aliases": d["aliases"],
                    "definition": d["definition"], "source": {"chapters": chs, "claim_ids": ok_ids, "pages_pdf": pages,
                                                              "node_id": ok_ids[0].split("#")[0] if ok_ids else None},
                    "en": d["en"], "symbol": d["symbol"], "symbol_en": d["symbol_en"], "stress": d["stress"], "say": d["say"],
                    "by_model": ["definition_wording", "en", "symbol_en", "stress", "say"],
                    "check": ("нет опоры в утверждениях" if not ok_ids else (f"числа не из книги: {', '.join(bad)}" if bad else "ok")),
                    "reviewed": False,
                })
    # статьи от модели заменяют черновые «без модели» с тем же термином
    merged = {norm(e["term"]): e for e in existing if not e.get("reviewed")}
    merged.update({norm(e["term"]): e for e in new_entries})
    merged.update(reviewed)  # проверенное человеком всегда главнее
    entries = sorted(merged.values(), key=lambda e: e["term"].lower())
    gpath.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_glossary_md(entries, DOMAIN / domain / "glossary.md")
    return {"terms": len(entries), "new": len(new_entries), "need_check": sum(e["check"] != "ok" for e in new_entries)}


SENT_RE = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z«(])")


def _term_pattern(term: str) -> str:
    """Регэксп термина с любыми окончаниями: «водонефтяной контакт» → водонефтян\\w*\\s+контакт\\w*."""
    abbr = re.findall(r"[А-ЯЁA-Z]{2,6}(?:-\d)?", term)
    words = [w for w in re.findall(r"[а-яёa-z]+", term.lower()) if len(w) >= 3]
    stems = [w if len(w) <= 5 else w[: len(w) - 2] for w in words[:3]]  # короткая основа ловит чужие слова (зонд ≠ зона)
    parts = [re.escape(s) + r"\w*" for s in stems] + [re.escape(a.lower()) for a in abbr[:1]]
    return r"[\s\-]+".join(parts) if parts else re.escape(term.lower())


def find_definition(term: str, texts: list[tuple[int, str]]) -> tuple[str, int] | None:
    """Найти в книге предложение-определение термина: «X — это…», «X называют…», «под X понимают…»,
    «X представляет собой…», «X — <существительное>…». Возвращает (предложение дословно, страница) или None."""
    from .ingest import _segments

    tp = _term_pattern(term)
    head = r"^\W{0,3}"  # термин — в начале предложения, как подлежащее
    pats = [
        rf"{head}{tp}\s*(\([^)]{{1,30}}\))?(\s*[—–]|\s+-)\s+(это\s+)?[а-яёa-z]",                   # X — это… / X - показатель…
        rf"{head}{tp}\s*(\([^)]{{1,30}}\))?\s+(называ\w+|представля\w+\s+собой|является|определя\w+\s+как|-\s+это)\b",
        rf"{head}под\s+{tp}[^.]{{0,40}}\s+понима\w+",                                               # под X понимают…
        rf"{head}[^.]{{0,40}}\s+называ\w+\s+{tp}\b",                                                # …называют X
        rf"{head}{tp}\s*(\([^)]{{1,30}}\))?\s*,\s*(т\.\s*е\.|то\s+есть)\s",                          # X, т. е. …
    ]
    best = None
    for page, text in texts:
        # только текст автора: описания рисунков, сделанные моделью при извлечении, не участвуют
        author = " ".join(seg for is_desc, seg in _segments(text, False)[0] if not is_desc)
        for s in SENT_RE.split(re.sub(r"\s+", " ", author)):
            s = s.strip()
            if not (25 <= len(s) <= 350) or "[нрзб]" in s or s.startswith(("|", ">", "Рис", "Таблица")) or s[:1].isdigit():
                continue
            low = s.lower()
            for rank, p in enumerate(pats):
                if re.search(p, low):
                    cand = (rank, len(s), s.strip(), page)
                    if best is None or cand[:2] < best[:2]:
                        best = cand
                    break
    return (best[2], best[3]) if best else None


def build_glossary_offline(chapters: list[Chapter]) -> dict:
    """Глоссарий без модели: термины узлов (terms_introduced), определение — мысль автора из узла, где термин введён
    (утверждение, в котором термин упомянут), со страницей. Существующие статьи (от модели или проверенные) не трогаются.
    EN, обозначения и ударения потом добавит `g2c glossary` (модель)."""
    from .mdbook import load_book

    domain = chapters[0].domain
    gpath = DOMAIN / domain / "glossary.json"
    existing = json.loads(gpath.read_text(encoding="utf-8")) if gpath.exists() else []
    # черновые статьи прошлых запусков «без модели» пересобираем; статьи от модели и проверенные человеком — не трогаем
    existing = [e for e in existing if e.get("reviewed") or e.get("by_model")]
    have = {norm(e["term"]) for e in existing} | {norm(a) for e in existing for a in e.get("aliases", [])}
    added = []
    for ch in chapters:
        lp = ch.work_dir / "layer.json"
        if not lp.exists():
            continue
        book = load_book(ch.md_path)
        own = [(p, book.pages[p]) for p in parse_pages(ch.pages) + parse_pages(ch.prereq_pages) if p in book.pages]
        rest = [(p, t) for p, t in sorted(book.pages.items()) if p not in {q for q, _ in own}]
        for n in json.loads(lp.read_text(encoding="utf-8"))["nodes"]:
            for t in n.get("terms_introduced", []):
                term = re.sub(r"\s*\([^)]*\)\s*$", "", t).strip() or t
                if norm(term) in have or norm(t) in have or len(term) < 2:
                    continue
                # сокращение в скобках: кириллица — русское сокращение, латиница — западное обозначение
                par = re.findall(r"\(([^()]{1,12})\)", t)
                abbr = next((p for p in par if re.fullmatch(r"[А-ЯЁ][А-ЯЁа-яё\-\d]{0,6}", p)), "")
                west = next((p for p in par if re.fullmatch(r"[A-Za-z][A-Za-z\-\d]{0,6}", p)), "")
                if not abbr and re.fullmatch(r"[А-ЯЁ]{2,6}(-\d)?", term):  # сам термин — русское сокращение
                    abbr = term
                have.add(norm(term))
                # 1) определение из самой книги: сначала глава и короткое чтение, потом вся книга
                found = find_definition(term, own) or find_definition(term, rest)
                # 2) связанная мысль автора из узла — показываем отдельно, НЕ как определение
                cl = _term_claims(term, n, limit=2)
                keys = [norm(k) for k in _key_words(term)]
                rel = ([c for c in cl if any(k in norm(c["text"]) for k in keys)] or cl[:1])[:1]
                entry = {
                    "term": term, "abbr": abbr, "aliases": [t] if t != term else [],
                    "definition": found[0] if found else "",
                    "definition_kind": "книга" if found else "нет",
                    "related": rel[0]["text"] if rel else "",
                    "source": {"chapters": [ch.id], "claim_ids": [c["id"] for c in rel],
                               "pages_pdf": [found[1]] if found else sorted({c["page_pdf"] for c in rel}),
                               "node_id": n["id"]},
                    "en": "", "symbol": "", "symbol_en": west, "stress": "", "say": "",
                    "by_model": [], "reviewed": False,
                    "check": ("определение — дословно из книги" if found else
                              "в книге прямого определения не найдено — его напишет g2c glossary (модель)"),
                }
                added.append(entry)
    entries = sorted(existing + added, key=lambda e: e["term"].lower())
    gpath.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_glossary_md(entries, DOMAIN / domain / "glossary.md")
    notation = apply_notation_and_english(chapters)
    return {"added": len(added), "total": len(entries), **notation,
            "with_book_definition": sum(1 for e in added if e["definition_kind"] == "книга"),
            "without_definition": sum(1 for e in added if e["definition_kind"] == "нет")}


BRACKET_EN_RE = re.compile(r"((?:[А-ЯЁа-яё][А-ЯЁа-яё\-]*\s+){0,4}[А-ЯЁа-яё][А-ЯЁа-яё\-]{2,})\s*[\[(]\s*([A-Z][A-Za-z\-']+(?:[ \-][A-Za-z\-']+){0,5})\s*[\])]")


def extract_bilingual(book, pages: list[int] | None = None) -> list[dict]:
    """Пары «русский термин [English term]» из текста автора (как у Барабошкина: «Дельты [Deltas]»)."""
    from .ingest import _segments

    out, seen = [], set()
    for p in pages or sorted(book.pages):
        text = " ".join(seg for is_desc, seg in _segments(book.pages.get(p, ""), False)[0] if not is_desc)
        for m in BRACKET_EN_RE.finditer(text):
            ru, en = m.group(1).strip(), m.group(2).strip()
            if len(en) < 4 or en.isupper() or norm(ru) in seen:
                continue
            seen.add(norm(ru))
            out.append({"ru": ru, "en": en, "page": p})
    return out


def apply_notation_and_english(chapters: list[Chapter]) -> dict:
    """Влить в глоссарий: (1) таблицу обозначений RU ↔ West ↔ LAS (domain/<домен>/notation.json — стандартная нотация,
    проверяет геолог); (2) английские термины, которые автор сам дал в скобках, — дословно, со страницей."""
    from .mdbook import load_book

    domain = chapters[0].domain
    gpath = DOMAIN / domain / "glossary.json"
    entries = json.loads(gpath.read_text(encoding="utf-8")) if gpath.exists() else []
    npath = DOMAIN / domain / "notation.json"
    notation = json.loads(npath.read_text(encoding="utf-8"))["entries"] if npath.exists() else []

    def key(s: str) -> str:
        return norm(re.sub(r"\s*\([^)]*\)", "", s))

    index: dict[str, list[dict]] = {}
    for e in entries:
        for k in {key(x) for x in [e["term"], e.get("abbr", ""), *e.get("aliases", [])] if x}:
            index.setdefault(k, []).append(e)
    n_sym, n_new = 0, 0
    for nt in notation:
        ru_keys = [key(x) for x in re.split(r",\s*", nt["ru"])] + [key(nt["ru_name"])]
        matched = []
        for k in ru_keys:  # обогащаем ВСЕ статьи с этим обозначением («ВНК» и «водонефтяной контакт»)
            for e in index.get(k, []):
                if e not in matched:
                    matched.append(e)
        if not matched:  # обозначения нет в глоссарии — добавляем статью-обозначение
            is_abbr = bool(re.fullmatch(r"[А-ЯЁ\-]{2,6}(-[а-я])?(\s*\(.*\))?", nt["ru"]))
            e = {"term": nt["ru_name"], "abbr": nt["ru"] if is_abbr else "", "aliases": [] if is_abbr else [nt["ru"]],
                 "definition": "", "definition_kind": "нет", "related": "", "source": {"chapters": [], "pages_pdf": []},
                 "by_model": [], "reviewed": False, "check": "статья из таблицы обозначений"}
            entries.append(e)
            for k in ru_keys:
                index.setdefault(k, []).append(e)
            matched = [e]
            n_new += 1
        for e in matched:
            is_abbr_entry = bool(e.get("abbr")) and key(e["abbr"]) in ru_keys
            e["symbol"] = e.get("symbol") or ("" if is_abbr_entry else nt["ru"])
            e["symbol_en"] = nt["west"]
            e["en"] = e.get("en") if e.get("en_source", "").startswith("из книги") else nt["west_name"]
            e["las"] = nt.get("las", "")
            e["unit"] = nt.get("unit", "")
            e["notation_note"] = nt.get("note", "")
            e["notation_group"] = nt["group"]
            if not e.get("en_source", "").startswith("из книги"):
                e["en_source"] = "стандартная нотация SPE/SPWLA — проверить геологу"
        n_sym += 1
    n_book = 0
    for ch in chapters:  # английские термины, которые дал сам автор
        for pair in extract_bilingual(load_book(ch.md_path)):
            for e in index.get(key(pair["ru"]), []):
                e["en"] = pair["en"]
                e["en_source"] = f"из книги, стр. {pair['page']}"
                n_book += 1
    entries = _merge_abbr_duplicates(entries)
    entries.sort(key=lambda e: e["term"].lower())
    gpath.write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_glossary_md(entries, DOMAIN / domain / "glossary.md")
    return {"notation_applied": n_sym, "notation_new_entries": n_new, "english_from_books": n_book, "total": len(entries)}


def _merge_abbr_duplicates(entries: list[dict]) -> list[dict]:
    """Статья «ВНК» и статья «водонефтяной контакт (ВНК)» — одно понятие: оставляем полную, сокращение — в aliases.
    Проверенные человеком статьи не сливаются."""
    full_by_abbr = {e["abbr"]: e for e in entries if e.get("abbr") and norm(e["term"]) != norm(e["abbr"])}
    out = []
    for e in entries:
        target = full_by_abbr.get(e.get("abbr") or "")
        if target is not None and target is not e and norm(e["term"]) == norm(e.get("abbr", "")) and not e.get("reviewed"):
            if not target.get("definition") and e.get("definition"):
                target["definition"], target["definition_kind"] = e["definition"], e.get("definition_kind")
            target.setdefault("aliases", [])
            if e["term"] not in target["aliases"]:
                target["aliases"].append(e["term"])
            continue
        out.append(e)
    return out


def _write_glossary_md(entries: list[dict], path) -> None:
    lines = ["# Глоссарий (RU/EN)", "",
             "Собирается командой `g2c glossary` из учебного слоя. Определения — по утверждениям книги (страницы в «Источник»);",
             "EN, западные обозначения, ударения и чтение предлагает модель. Проверенные статьи помечаются `\"reviewed\": true` в glossary.json.", "",
             "| Термин | Сокр. | Определение | EN (западный аналог) | Обозначение RU / West | Кривая в LAS | Единицы | Читать | Источник | Проверка |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for e in entries:
        sym = " / ".join(x for x in (e.get("symbol") or e.get("abbr"), e.get("symbol_en")) if x)
        src = ", ".join(f"стр. {p}" for p in (e.get("source") or {}).get("pages_pdf", []))
        say = e.get("say") or e.get("stress") or ""
        flag = "✓ проверено" if e.get("reviewed") else e.get("check", "")
        en = e.get("en", "") + (f" ({e['en_source']})" if e.get("en_source") else "")
        cells = [e["term"], e.get("abbr", ""), e.get("definition", ""), en, sym, e.get("las", ""), e.get("unit", ""), say, src, flag]
        lines.append("| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_it_bridges(chapters: list[Chapter]) -> int:
    """ИТ-мосты собираются из узлов без вызова модели: аналогия, где ломается, данные и гипотезы."""
    domain = chapters[0].domain
    lines = ["# ИТ-мосты", "",
             "Собирается командой `g2c glossary` из учебного слоя: для каждого узла — ИТ-аналогия и где она ломается,",
             "что знание значит для данных (по главе) и гипотезы для ИТ (сверх главы). Всё написано моделью.", ""]
    n_total = 0
    for ch in chapters:
        layer = json.loads((ch.work_dir / "layer.json").read_text(encoding="utf-8"))
        lines += [f"## {ch.title}", ""]
        for n in layer["nodes"]:
            a = n.get("it_analogy") or {}
            if not a.get("text"):
                continue
            n_total += 1
            dm = n.get("data_meaning") or {}
            lines.append(f"### {n['title']} (`{n['id']}`)")
            lines.append(f"- **Аналогия:** {a['text']}")
            lines.append(f"- **Где ломается:** {a.get('breaks_where', '')}")
            if (dm.get("from_chapter") or {}).get("text"):
                lines.append(f"- **В данных (по главе):** {dm['from_chapter']['text']}")
            if (dm.get("breaks") or {}).get("text"):
                lines.append(f"- **Что ломается в данных:** {dm['breaks']['text']}")
            for h in dm.get("hypotheses", []):
                lines.append(f"- *Гипотеза:* {h['text']}")
            lines.append("")
    (DOMAIN / domain / "it_bridges.md").write_text("\n".join(lines), encoding="utf-8")
    return n_total
