"""Сборщик демо-курса: учебный слой → офлайн-сайт в out/<глава>/ (открывается двойным кликом, без сервера и логина).

Курс собирается только из слоя (единственный источник правды) и сопутствующих файлов главы:
    work/<глава>/layer.json          узлы → шаги курса
    work/<глава>/tasks.json          задания (task.schema.json), если уже есть
    work/<глава>/illustrations.json  иллюстрации (illustration.schema.json), если уже есть
    work/<глава>/units.json          таблицы, формулы и рисунки книги, разделы для частей
    domain/<домен>/glossary.json     определения терминов для блока «Новые понятия»
Формулы переводятся в MathML при сборке (браузеры показывают его без библиотек и интернета).
Данные кладутся в data.js (window.COURSE = …), а не в .json: fetch локальных файлов из file:// не работает.
"""
from __future__ import annotations

import html
import json
import re
import shutil
import time
from collections import Counter
from pathlib import Path

from .paths import DOMAIN, OUT, ROOT, Chapter

TEMPLATE = ROOT / "course_template"
MATH_RE = re.compile(r"(\\\[.+?\\\]|\$\$.+?\$\$|\\\(.+?\\\)|(?<![\\\w])\$[^$\n]{1,200}?\$)", re.S)


# --- текст → HTML -----------------------------------------------------------------
TEX_NORMALIZE = [  # команды, которые конвертер понимает хуже, → равносильные, которые понимает
    (r"\\[dt]frac\b", r"\\frac"), (r"\\text(?:rm|normal)?\s*\{", r"\\mathrm{"), (r"\\operatorname\*?\s*\{", r"\\mathrm{"),
    (r"\\(?:bm|boldsymbol)\s*\{", r"\\mathbf{"), (r"\\mbox\s*\{", r"\\mathrm{"), (r"\\displaystyle|\\textstyle|\\limits|\\nolimits", ""),
    (r"\\(?:big|Big|bigg|Bigg)[lr]?(?=[()\[\]|.])", ""), (r"\\(?:qquad|quad|,|;|:|!)", " "), (r"~", " "),
    (r"\\begin\{(?:aligned|align\*?|gathered|split|eqnarray\*?)\}", r"\\begin{matrix}"),
    (r"\\end\{(?:aligned|align\*?|gathered|split|eqnarray\*?)\}", r"\\end{matrix}"),
    (r"\\label\{[^}]*\}", ""), (r"\\nonumber|\\notag", ""),
]


def mathml(tex: str, display: bool) -> str:
    """LaTeX → MathML: дроби, корни, индексы, суммы, интегралы, матрицы (matrix/pmatrix/bmatrix/vmatrix),
    системы (cases), выравнивание (aligned → matrix). Номер формулы (\\tag) выносится справа.
    Если перевести нельзя — показываем исходник моноширинным, а не теряем формулу."""
    from latex2mathml.converter import convert

    tag = re.search(r"\\tag\{([^}]*)\}", tex)
    aligned = bool(re.search(r"\\begin\{(?:aligned|align\*?|split|eqnarray\*?)\}", tex))
    t = re.sub(r"\\tag\{[^}]*\}", "", tex).strip()
    for a, b in TEX_NORMALIZE:
        t = re.sub(a, b, t)
    try:
        out = convert(t, display="block" if display else "inline")
    except Exception:
        try:  # вторая попытка: без \left \right (частая причина сбоя)
            out = convert(re.sub(r"\\(left|right)\s*", "", t), display="block" if display else "inline")
        except Exception:
            return f'<code class="tex">{html.escape(tex)}</code>'
    if aligned:  # как в учебнике: левая часть — по правому краю, «= …» — по левому (Chrome понимает только CSS)
        def row(m):
            k = [0]

            def cell(c):
                k[0] += 1
                side = "right" if k[0] % 2 else "left"
                pad = "padding-right:0" if side == "right" else "padding-left:0"
                return f'<mtd style="text-align:{side};{pad}"'
            return re.sub(r"<mtd\b[^>]*?(?=>)", cell, m.group(0))
        out = re.sub(r"<mtr\b.*?</mtr>", row, out, flags=re.S)
        out = out.replace("<mtable", '<mtable columnalign="right left right left" columnspacing="0"', 1)
    if display:
        num = f"<span class='eqno'>({html.escape(tag.group(1))})</span>" if tag else ""
        return f"<div class='eq'>{out}{num}</div>"
    return out


_L = "A-Za-zА-Яа-яЁёα-ωΑ-Ωϰϱϕ"
# голый LaTeX без обрамления: \rho_{п}, \alpha_{сп}^{*}, \Delta U, \frac{a}{b}
TEX_TOKEN_RE = re.compile(r"\\[a-zA-Z]+(?:\s*(?:\{[^{}]*\}|[_^](?:\{[^{}]*\}|[^\s{}\\,;.])|\\[a-zA-Z]+))*")
TEXT_REPL = [("<->", "↔"), ("->", "→"), ("=>", "⇒"), ("<=", "≤"), (">=", "≥"), ("!=", "≠"), ("+-", "±"), ("+/-", "±"),
             ("~=", "≈"), ("<<", "≪"), (">>", "≫")]


def pretty_math(t: str) -> str:
    """Формулы, записанные простым текстом (уже экранированным для HTML), → математические символы:
    ρ_п → ρ<sub>п</sub>, δ_{сп} → δ<sub>сп</sub>, K*пр → K<sup>*</sup><sub>пр</sub>, 10^3 → 10<sup>3</sup>, a * b → a · b.
    Индекс ставится только у одиночной буквы (не трогает идентификаторы вроде LFP_GR или data.volve_las)."""
    L = _L
    t = re.sub(rf"(?<![{L}\d])([{L}])\*([{L}]{{1,4}})(?![{L}])", r"\1<sup>*</sup><sub>\2</sub>", t)   # K*пр
    t = re.sub(rf"(?<![{L}\d*])([{L}])\*(?=[\s,;.:)=<>]|$)", r"\1<sup>*</sup>", t)                         # K*
    t = re.sub(rf"(?<![{L}\d_])([{L}](?:<sup>\*</sup>)?)_\{{([^{{}}]{{1,14}})\}}", r"\1<sub>\2</sub>", t)     # ρ_{пп}
    t = re.sub(rf"(?<![{L}\d_])([{L}](?:<sup>\*</sup>)?)_([{L}\d]{{1,5}})(?![{L}\d_])", r"\1<sub>\2</sub>", t)  # ρ_п
    t = re.sub(r"([\w)\]>])\^\{([^{}]{1,14})\}", r"\1<sup>\2</sup>", t)                                  # x^{-3}, k_n^{*}
    t = re.sub(r"([\w)\]>])\^(-?[\w.*]{1,5})", r"\1<sup>\2</sup>", t)                                     # 10^3, 10^-3, k_n^*
    t = re.sub(r"(?<=[\w)\]]) \* (?=[\w(])", " · ", t)                                                    # a * b
    return t


# дроби и корни, записанные простым текстом: (a+b)/(c+d), (x)/2, sqrt(x), √(x). Единицы (см³/ч, мкс/м) не трогаем:
# дробью становится только запись, где хотя бы одна сторона — выражение в скобках.
_P = r"\((?:[^()]|\([^()]*\))+\)"
FRAC_RE = re.compile(rf"({_P}|[\w.]+)\s*/\s*({_P}|[\w.]+)")
SQRT_RE = re.compile(rf"(?:sqrt|√)\s*({_P})")


def _plain_to_tex(s: str) -> str:
    s = s.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]
    s = s.replace("*", r"\cdot ").replace("·", r"\cdot ")
    s = re.sub(r"_([^\s{}()+\-*/=<>]{2,})", r"_{\1}", s)
    return s


def _plain_math(part: str) -> list[tuple[bool, str]]:
    """Разбить простой текст на куски: (это_формула, текст/TeX)."""
    spans = []
    for m in SQRT_RE.finditer(part):
        spans.append((m.start(), m.end(), rf"\sqrt{{{_plain_to_tex(m.group(1))}}}"))
    for m in FRAC_RE.finditer(part):
        a, b = m.group(1), m.group(2)
        if not (a.startswith("(") or b.startswith("(")):
            continue
        if any(s <= m.start() < e for s, e, _ in spans):
            continue
        spans.append((m.start(), m.end(), rf"\frac{{{_plain_to_tex(a)}}}{{{_plain_to_tex(b)}}}"))
    spans.sort()
    out, pos = [], 0
    for s, e, tex in spans:
        if s < pos:
            continue
        out.append((False, part[pos:s]))
        out.append((True, tex))
        pos = e
    out.append((False, part[pos:]))
    return out


def _plain(part: str) -> str:
    out = []
    for is_math, chunk in _plain_math(part):
        if is_math:
            out.append(mathml(chunk, False))
        else:
            out.append(_plain_tex_commands(chunk))
    return "".join(out)


def _plain_tex_commands(part: str) -> str:
    out = []
    pos = 0
    for m in TEX_TOKEN_RE.finditer(part):  # голые команды LaTeX — в MathML
        out.append(_plain_text(part[pos:m.start()]))
        out.append(mathml(m.group(0), False))
        pos = m.end()
    out.append(_plain_text(part[pos:]))
    return "".join(out)


def _plain_text(s: str) -> str:
    for a, b in TEXT_REPL:
        s = s.replace(a, b)
    t = html.escape(s)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = pretty_math(t)
    return t.replace("\n", "<br>")


def rich(s: str | None) -> str:
    """Текст с формулами и **жирным** → безопасный HTML. Формулы в LaTeX (\\( \\), $ $, \\[ \\], голые \\команды)
    → MathML; формулы простым текстом (ρ_п, K*пр, 10^3, ->) → настоящие индексы и символы."""
    if not s:
        return ""
    out = []
    for part in MATH_RE.split(s):
        if not part:
            continue
        if MATH_RE.fullmatch(part):
            if part.startswith(r"\[") or part.startswith("$$"):
                out.append(mathml(part[2:-2], True))
            elif part.startswith(r"\("):
                out.append(mathml(part[2:-2], False))
            else:
                out.append(mathml(part[1:-1], False))
        else:
            out.append(_plain(part))
    return "".join(out)


def table_html(md: str) -> str:
    """Таблица книги: markdown-таблица или HTML-таблица из md → чистый HTML (без атрибутов, кроме объединения ячеек)."""
    if "<table" in md.lower():
        t = re.sub(r"<(/?)(table|thead|tbody|tr|td|th)\b([^>]*)>",
                   lambda m: f"<{m.group(1)}{m.group(2).lower()}{_span_attrs(m.group(3))}>", md, flags=re.I)
        t = re.sub(r"<(?!/?(table|thead|tbody|tr|td|th)\b)[^>]+>", "", t)  # прочие теги — вон
        return t
    lines = [l.strip() for l in md.splitlines() if l.strip()]
    caption = [l for l in lines if not l.startswith("|")]
    rows = [l for l in lines if l.startswith("|") and not re.fullmatch(r"\|[\s:|-]+\|?", l)]
    cells = [[rich(c.strip()) for c in r.strip("|").split("|")] for r in rows]
    if not cells:
        return f"<p>{rich(md)}</p>"
    head, body = cells[0], cells[1:]
    h = "".join(f"<th>{c}</th>" for c in head)
    b = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in body)
    cap = f"<caption>{rich(' '.join(caption))}</caption>" if caption else ""
    return f"<table>{cap}<thead><tr>{h}</tr></thead><tbody>{b}</tbody></table>"


def _span_attrs(attrs: str) -> str:
    keep = re.findall(r'\b(colspan|rowspan)\s*=\s*"?(\d+)"?', attrs, flags=re.I)
    return "".join(f' {k.lower()}="{v}"' for k, v in keep)


def pages_label(pages: list[int]) -> str:
    if not pages:
        return ""
    pages = sorted(set(pages))
    if len(pages) == 1:
        return f"стр. {pages[0]}"
    return f"стр. {pages[0]}–{pages[-1]}" if pages[-1] - pages[0] == len(pages) - 1 else "стр. " + ", ".join(map(str, pages))


# --- части курса ----------------------------------------------------------------------
SECTION_RE = re.compile(r"§|^\s*\d+(\.\d+)+\s|^глава", re.I)


def _nice(title: str) -> str:
    t = title.strip("#* ").strip()
    if t.isupper():
        t = t.capitalize()
    return t[:90]


def _unit_sections(units: dict[str, dict]) -> dict[str, str]:
    """Раздел каждой единицы, идя по главе подряд: последний встреченный § (или 1.2.3) + последний подраздел.
    В md многих книг все заголовки одного уровня, поэтому heading_path не знает, какому § принадлежит подраздел."""
    out, sec, sub = {}, None, None
    for uid, u in units.items():
        if u.get("kind") == "heading":
            h = u["text"].strip("#* \n")
            if re.match(r"(?i)\s*(рис\.?|рисунок|таблица|fig)\s*\d", h) or re.fullmatch(r"(?i)(задачи|глава\s+[ivxlc\d]+)", h):
                pass
            elif SECTION_RE.search(h) and not h.lower().startswith("глава"):
                sec, sub = h, None
            else:
                sub = h
        if sec or sub:
            m = re.match(r"\s*(§\s*\d+|\d+(\.\d+)+)", sec or "")
            out[uid] = f"{m.group(1)} — {_nice(sub)}" if (m and sub) else _nice(sec or sub)
    return out


def build_parts(nodes: list[dict], units: dict[str, dict]) -> list[dict]:
    parts: list[dict] = []
    sec_of = _unit_sections(units)
    for n in nodes:
        if ".pre_" in n["id"]:
            key = "Часть 0. Что нужно знать заранее"
        else:
            heads = [sec_of[u] for u in n.get("unit_ids", []) if u in sec_of]
            key = _nice(Counter(heads).most_common(1)[0][0]) if heads else (parts[-1]["title"] if parts else "Глава")
        if parts and parts[-1]["title"] == key:
            parts[-1]["steps"].append(n["id"])
        else:
            parts.append({"title": key, "steps": [n["id"]]})
    # части из 1 шага — к соседней части (иначе путь рвётся на десятки мелких кусков)
    merged: list[dict] = []
    for p in parts:
        if merged and (len(p["steps"]) == 1 or p["title"] == merged[-1]["title"]) and not p["title"].startswith("Часть 0"):
            merged[-1]["steps"] += p["steps"]
        else:
            merged.append(p)
    return merged


# --- шаг курса ----------------------------------------------------------------------------
def block(key: str, title: str, body: str, note: str = "") -> dict | None:
    if not body or not body.strip():
        return None
    return {"key": key, "title": title, "html": body, "note": note}


def model_note(field: dict | None, pages: list[int]) -> str:
    if not field:
        return ""
    if field.get("by_model"):
        return f"пересказ модели по {pages_label(pages)}" if pages else "написано моделью"
    return f"текст книги, {pages_label(pages)}"


def step_from_node(n: dict, units: dict[str, dict], glossary: dict[str, dict], tasks: list[dict],
                   illustrations: list[dict], titles: dict[str, str]) -> dict:
    L = n.get("layers", {})
    pages = n.get("pages", [])
    claims = n.get("claims", [])
    blocks: list[dict | None] = []
    blocks.append(block("main", "Суть", f"<p class='lead'>{rich(L.get('main', {}).get('text'))}</p>",
                        model_note(L.get("main"), pages)))
    blocks.append(block("mechanism", "Как это работает", f"<p>{rich(L.get('mechanism', {}).get('text'))}</p>",
                        model_note(L.get("mechanism"), pages)))
    # все мысли автора — у каждой страница и цитата
    kind_ru = {"observation": "наблюдение", "inference": "вывод", "assumption": "допущение"}
    items = []
    for c in claims:
        src = "описание рисунка моделью" if c.get("source_kind") == "figure_description_model" else kind_ru.get(c["kind"], c["kind"])
        page = "карточка-предпосылка" if c["page_pdf"] == 0 else f"стр. {c['page_pdf']}"
        items.append(f"<li id='{html.escape(c['id'])}'><span class='kind k-{c['kind']}'>{src}</span> {rich(c['text'])} "
                     f"<span class='page'>{page}</span>"
                     f"<details><summary>слова автора</summary><blockquote>«{rich(c['quote'])}»</blockquote></details></li>")
    blocks.append(block("claims", "Все мысли автора", f"<ol class='claims'>{''.join(items)}</ol>",
                        "каждая мысль — с номером страницы и цитатой автора"))
    blocks.append(block("practice", "По шагам: как делает специалист", f"<p>{rich(L.get('practice', {}).get('text'))}</p>",
                        model_note(L.get("practice"), pages)))
    ex = []
    for e in n.get("worked_examples", []):
        steps = "".join(f"<li>{rich(s)}</li>" for s in e.get("steps", []))
        fig = f"<p class='muted'>Рисунок: {html.escape(e['figure'])}</p>" if e.get("figure") else ""
        ex.append(f"<div class='example'><p><b>Условие.</b> {rich(e['condition'])}</p><ol>{steps}</ol>"
                  f"<p><b>Итог.</b> {rich(e['result'])}</p>{fig}<p class='page'>стр. {e['page_pdf']}</p></div>")
    blocks.append(block("examples", "Разбор примеров из главы", "".join(ex), "числа — как у автора"))
    blocks.append(block("conditions", "Цифры и условия", f"<p>{rich(L.get('nuances', {}).get('text'))}</p>",
                        model_note(L.get("nuances"), pages)))
    uobjs = [units[u] for u in n.get("unit_ids", []) if u in units]
    forms = [u for u in uobjs if u["kind"] == "formula"]
    fhtml = "".join(f"<div class='formula'>{rich(u['text'])}<span class='page'>стр. {u['page_pdf']}</span></div>" for u in forms)
    fclaims = [c for c in claims if c.get("source_kind") == "formula"]
    if fclaims:
        fhtml += "<ul>" + "".join(f"<li>{rich(c['text'])} <span class='page'>стр. {c['page_pdf']}</span></li>" for c in fclaims) + "</ul>"
    blocks.append(block("formulas", "Формулы", fhtml, "только из главы"))
    tabs = [u for u in uobjs if u["kind"] == "table"]
    thtml = ""
    for u in tabs:
        fixed = u.get("typo_fixes") or []
        fixnote = ("<p class='muted'>Исправлено по листу опечаток: " + "; ".join(f"{html.escape(f['wrong'])} → {html.escape(f['right'])}" for f in fixed) + "</p>") if fixed else ""
        thtml += f"<div class='table-wrap'>{table_html(u.get('text_fixed') or u['text'])}</div><p class='page'>стр. {u['page_pdf']}</p>{fixnote}"
    blocks.append(block("tables", "Таблицы книги", thtml, "целиком, как в книге"))
    terms = []
    for t in n.get("terms_introduced", []):
        g = glossary.get(t.lower()) or glossary.get(re.sub(r"\s*\([^)]*\)\s*$", "", t.lower()))
        d = f" — {rich(g['definition'])}" if g and g.get("definition") else ""
        en = f" <span class='en'>({html.escape(g['en'])})</span>" if g and g.get("en") else ""
        name = html.escape(t)
        link = f"<a href='#/glossary/{term_slug(g['term'])}'>{name}</a>" if g else name
        terms.append(f"<li><b>{link}</b>{en}{d}</li>")
    blocks.append(block("terms", "Новые понятия", f"<ul>{''.join(terms)}</ul>" if terms else ""))
    m = n.get("misconception")
    if m and m.get("text"):
        blocks.append(block("misconception", "Частая ошибка", f"<p class='wrong'>✗ {rich(m['text'])}</p>", "написано моделью"))
    dm = n.get("data_meaning") or {}
    dhtml = ""
    if (dm.get("from_chapter") or {}).get("text"):
        dhtml += f"<p><b>По главе:</b> {rich(dm['from_chapter']['text'])}</p>"
    if (dm.get("breaks") or {}).get("text"):
        dhtml += f"<p><b>Где ломается:</b> {rich(dm['breaks']['text'])}</p>"
    for h in dm.get("hypotheses", []):
        dhtml += f"<p class='hypo'><span class='badge'>гипотеза</span> {rich(h['text'])}</p>"
    blocks.append(block("data", "Что это значит для данных", dhtml, "выводы из главы отделены от гипотез"))
    dod = n.get("decision_on_data")
    if dod:
        feats = "".join(f"<li>{rich(f)}</li>" for f in dod.get("features", []))
        cx = f"<p><b>Контрпример:</b> {rich(dod['counterexample'])}</p>" if dod.get("counterexample") else ""
        blocks.append(block("decision", "Решение на данных",
                            f"<div class='decision'><div><b>Признаки</b><ul>{feats}</ul></div><div><b>Правило</b><p>{rich(dod.get('rule'))}</p></div>"
                            f"<div><b>Вывод</b><p>{rich(dod.get('conclusion'))}</p></div></div>{cx}", "признаки → правило → вывод"))
    a = n.get("it_analogy") or {}
    if a.get("text"):
        blocks.append(block("analogy", "Аналогия из мира данных",
                            f"<p>{rich(a['text'])}</p><p class='limit'><b>Где перестаёт работать:</b> {rich(a.get('breaks_where'))}</p>",
                            "написано моделью"))
    if n.get("now_practice") and n["now_practice"].get("text"):
        blocks.append(block("now", "Как это делается сейчас", f"<p>{rich(n['now_practice']['text'])}</p>",
                            "книга описывает технику своего времени; нюанс написан моделью"))
    lens = n.get("inefficiency_lens")
    if lens:
        rows = [("Входные данные", lens.get("inputs")), ("Ручные шаги", lens.get("manual_steps")),
                ("Где нужно суждение специалиста", lens.get("judgment_points")), ("Время", lens.get("time")),
                ("Типичные ошибки", lens.get("typical_errors"))]
        blocks.append(block("lens", "Где здесь ручной труд",
                            "<dl class='lens'>" + "".join(f"<dt>{k}</dt><dd>{rich(v)}</dd>" for k, v in rows if v) + "</dl>",
                            "линза неэффективности; написано моделью"))
    bt = []
    for t in n.get("book_tasks", []):
        fig = f" <span class='muted'>Нужен {html.escape(t['figure'])}.</span>" if t.get("figure") else ""
        bt.append(f"<li><b>Задача {html.escape(str(t['number']))}.</b> {rich(t['text'])}{fig} <span class='page'>стр. {t['page_pdf']}</span></li>")
    blocks.append(block("booktasks", "Задачи из книги", f"<ul>{''.join(bt)}</ul>" if bt else ""))
    ill = [i for i in illustrations if i.get("step_id") == n["id"]]
    ihtml = ""
    for i in ill:
        label = f"<span class='label'>{html.escape(i.get('label') or '')}</span>" if i.get("label") else ""
        ihtml += (f"<figure><div class='img'><img src='{html.escape(i['file'])}' alt='{html.escape(i['alt'])}' loading='lazy'>{label}</div>"
                  f"<figcaption>{rich(i['caption'])}{' · ' + html.escape(i['source']) if i.get('source') else ''}</figcaption></figure>")
    if not ill:
        figs = [u for u in uobjs if u["kind"] == "figure" and u.get("text")]
        if figs:
            ihtml = "".join(f"<div class='fig-todo'>🖼 {rich(u['text'][:220])}<br><span class='muted'>Рисунок книги будет перерисован кодом "
                            f"(подпись «перерисовано по рис., стр. {u['page_pdf']}»). Вставлять рисунки книги в курс нельзя.</span></div>"
                            for u in figs)
    blocks.append(block("illustrations", "Иллюстрации", ihtml))
    deeper = "".join(f"<details><summary>Подробнее</summary><p>{rich(d['text'])}</p></details>"
                     for d in L.get("deeper", []) if d.get("text"))
    blocks.append(block("deeper", "Глубже", deeper, "для любопытных"))

    words = sum(len(re.sub(r"<[^>]+>", " ", b["html"]).split()) for b in blocks if b)
    my_tasks = [t["id"] for t in tasks if t.get("step_id") == n["id"]]
    minutes = max(3, round(words / 170 + len(my_tasks) * 0.7))
    prereqs = [l["target"] for l in n.get("links", []) if l["type"] == "prerequisite" and l["target"] in titles]
    return {"id": n["id"], "order": n["order"], "title": n["title"], "book_title": n.get("book_title", ""),
            "pages": pages_label(pages), "minutes": minutes, "threshold": n.get("threshold", False),
            "difficulty": n.get("difficulty", {}), "prereqs": prereqs, "is_prereq": ".pre_" in n["id"],
            "blocks": [b for b in blocks if b], "tasks": my_tasks,
            "summary": rich(L.get("main", {}).get("text")),
            "data_summary": rich((dm.get("from_chapter") or {}).get("text")),
            "card_back": rich(L.get("main", {}).get("text"))}


# --- карта связей ------------------------------------------------------------------------
def map_svg(steps: list[dict]) -> str:
    """Вертикальная карта: шаги сверху вниз по порядку, сдвиг вправо — глубина предпосылок, дуги — «опирается на»."""
    idx = {s["id"]: i for i, s in enumerate(steps)}
    depth: dict[str, int] = {}
    for s in steps:
        depth[s["id"]] = 1 + max((depth.get(p, 0) for p in s["prereqs"] if p in idx and idx[p] < idx[s["id"]]), default=-1)
    row_h, x0, dx, w = 54, 40, 22, 300
    maxd = max(depth.values(), default=0)
    width = x0 + maxd * dx + w + 20
    height = len(steps) * row_h + 20
    edges, boxes = [], []
    for s in steps:
        i = idx[s["id"]]
        y = 10 + i * row_h
        x = x0 + depth[s["id"]] * dx
        for p in s["prereqs"]:
            if p in idx and idx[p] < i:
                py = 10 + idx[p] * row_h + 20
                px = x0 + depth[p] * dx
                edges.append(f"<path d='M{px},{py} C{min(px, x) - 30},{py} {min(px, x) - 30},{y + 20} {x},{y + 20}' class='edge'/>")
        title = html.escape(s["title"][:42] + ("…" if len(s["title"]) > 42 else ""))
        cls = "box pre" if s["is_prereq"] else ("box thr" if s["threshold"] else "box")
        boxes.append(f"<a href='#/step/{html.escape(s['id'])}' class='maplink'><g data-step='{html.escape(s['id'])}'>"
                     f"<rect x='{x}' y='{y}' width='{w}' height='40' rx='8' class='{cls}'/>"
                     f"<text x='{x + 12}' y='{y + 25}'>{s['order']}. {title}</text></g></a>")
    return (f"<svg viewBox='0 0 {width} {height}' width='{width}' role='img' aria-label='Карта связей шагов'>"
            f"<defs><marker id='ar' viewBox='0 0 10 10' refX='9' refY='5' markerWidth='6' markerHeight='6' orient='auto'>"
            f"<path d='M0,0 L10,5 L0,10 z' class='arrow'/></marker></defs>{''.join(edges)}{''.join(boxes)}</svg>")


# --- сборка ------------------------------------------------------------------------------
def _load(p: Path, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def build_course(ch: Chapter, layer_path: Path | None = None, tasks_path: Path | None = None,
                 out_dir: Path | None = None) -> Path:
    wd = ch.work_dir
    layer = _load(layer_path or wd / "layer.json", None)
    if not layer:
        raise FileNotFoundError(f"Нет учебного слоя для {ch.id}: сначала g2c layer {ch.id}")
    tasks = _load(tasks_path or wd / "tasks.json", {"tasks": []}).get("tasks", [])
    ills = _load(wd / "illustrations.json", {"illustrations": []}).get("illustrations", [])
    units = {u["id"]: u for u in _load(wd / "units.json", [])}
    if layer_path is None:  # глоссарий — часть курса: если от модели его ещё нет, собираем из слоя без модели
        from .glossary import build_glossary_offline

        build_glossary_offline([ch])
    gl_entries = _load(DOMAIN / ch.domain / "glossary.json", [])
    gl = {e["term"].lower(): e for e in gl_entries}
    for e in list(gl.values()):
        for a in e.get("aliases", []) + ([e["abbr"]] if e.get("abbr") else []):
            gl.setdefault(a.lower(), e)
    nodes = sorted(layer["nodes"], key=lambda n: n["order"])
    titles = {n["id"]: n["title"] for n in nodes}
    steps = [step_from_node(n, units, gl, tasks, ills, titles) for n in nodes]
    videos = _collect_videos(ch, {s["id"] for s in steps})
    for s in steps:  # ролики — на своих шагах, сразу после сути
        for v in videos.get(s["id"], []):
            s["blocks"].insert(1, {"key": "video", "title": "Ролик", "note": v["note"], "html": v["html"]})
    claim_text = {c["id"]: c for n in nodes for c in n["claims"]}
    task_out = []
    for t in tasks:
        tt = dict(t)
        tt["prompt_html"] = rich(t["prompt"])
        tt["explanation_html"] = rich(t.get("explanation", ""))
        tt["options"] = [{**o, "html": rich(o["text"])} for o in t.get("options", [])]
        tt["claims"] = [{"id": c, "page": claim_text[c]["page_pdf"]} for c in t.get("claim_ids", []) if c in claim_text]
        task_out.append(tt)
    data = {
        "chapter": {"id": ch.id, "title": ch.title, "pages": ch.pages, "book": ch.book_md, "difficulty": ch.difficulty},
        "parts": build_parts(nodes, units),
        "steps": steps,
        "tasks": task_out,
        "glossary": glossary_for_course(nodes, gl_entries),
        "notation": notation_for_course(gl_entries, " ".join(u.get("text", "") for u in units.values())
                                        + " " + " ".join(c["text"] for n in nodes for c in n["claims"])),
        "map_svg": map_svg(steps),
        "generated": time.strftime("%Y-%m-%d %H:%M"),
        "generated_by": layer.get("generated_by", ""),
    }
    out = out_dir or OUT / ch.id
    if out.exists():
        shutil.rmtree(out)
    shutil.copytree(TEMPLATE, out)
    for v in [v for vs in videos.values() for v in vs]:  # ролики и субтитры — в media/
        (out / "media").mkdir(exist_ok=True)
        shutil.copyfile(v["mp4"], out / "media" / v["mp4"].name)
        (out / "media" / (v["mp4"].stem + ".vtt")).write_text(v["vtt"], encoding="utf-8")
    for i in ills:  # файлы иллюстраций — рядом с курсом
        src = wd / i["file"]
        if src.exists():
            (out / i["file"]).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, out / i["file"])
    (out / "data.js").write_text("window.COURSE = " + json.dumps(data, ensure_ascii=False) + ";\n", encoding="utf-8")
    idx = (out / "index.html").read_text(encoding="utf-8").replace("{{TITLE}}", html.escape(ch.title))
    (out / "index.html").write_text(idx, encoding="utf-8")
    return out / "index.html"


def _collect_videos(ch: Chapter, step_ids: set[str]) -> dict[str, list[dict]]:
    """Ролики главы из out/video/: паспорт → шаг (node_id), mp4, субтитры WebVTT."""
    res: dict[str, list[dict]] = {}
    vdir = OUT / "video"
    for pp in sorted(vdir.glob(f"{ch.id}.*.passport.json")) if vdir.exists() else []:
        p = json.loads(pp.read_text(encoding="utf-8"))
        mp4 = vdir / (pp.name.replace(".passport.json", ".mp4"))
        if p.get("node_id") not in step_ids or not mp4.exists():
            continue
        srt = mp4.with_suffix(".srt")
        vtt = "WEBVTT\n\n" + (srt.read_text(encoding="utf-8").replace(",", ".") if srt.exists() else "")
        short = p.get("format", "").startswith("шортс")
        name = html.escape(mp4.name)
        body = (f"<video controls preload='metadata' playsinline style='max-width:{'360px' if short else '100%'};width:100%;border-radius:10px'>"
                f"<source src='media/{name}' type='video/mp4'>"
                f"<track kind='subtitles' srclang='ru' label='Русские субтитры' src='media/{html.escape(mp4.stem)}.vtt' default></video>"
                f"<p class='muted'>Вопрос на перенос: {html.escape(p.get('transfer_question', ''))}</p>"
                f"<details><summary>Паспорт ролика</summary><p>Шаблон: {html.escape(p.get('template', ''))}. "
                f"Преувеличения: {html.escape('; '.join(p.get('exaggerations', [])))}. "
                f"Мост к данным: {html.escape(p.get('data_it_bridge', ''))}</p></details>")
        res.setdefault(p["node_id"], []).append({"mp4": mp4, "vtt": vtt, "html": body,
                                                 "note": f"{p.get('format', '')}; кадры рисует код из данных и модели сцены"})
    return res


def term_slug(t: str) -> str:
    from .books import slugify

    return slugify(t, 60)


def glossary_for_course(nodes: list[dict], entries: list[dict]) -> list[dict]:
    """Глоссарий главы: термины, которые узлы вводят или используют, с определением, EN, обозначением,
    страницей и шагом, где термин введён. Определение по книге — со страницей; помечено, что написала модель."""
    by_norm = {}
    for e in entries:
        for k in [e["term"], *e.get("aliases", []), *([e["abbr"]] if e.get("abbr") else [])]:
            by_norm.setdefault(re.sub(r"\s+", " ", k.lower()).strip(), e)
    intro_step = {}
    for n in nodes:
        for t in n.get("terms_introduced", []):
            intro_step.setdefault(re.sub(r"\s+", " ", t.lower()).strip(), n["id"])
    out, seen = [], set()
    for n in nodes:
        for t in n.get("terms_introduced", []) + n.get("terms_used", []):
            k = re.sub(r"\s+", " ", t.lower()).strip()
            e = by_norm.get(k) or by_norm.get(re.sub(r"\s*\([^)]*\)\s*$", "", k))
            if not e or e["term"] in seen:
                continue
            seen.add(e["term"])
            src = e.get("source") or {}
            is_symbol = (bool(re.search(r"[_^*\\₀-ₜᵢ-ᵪ⁰-ⁿ]", e["term"]))
                         or bool(re.fullmatch(r"[A-Za-zα-ωΑ-Ωϰ][A-Za-zА-Яа-яα-ω\d,.*′]{0,9}", e["term"])))  # kn,ca · ρп · Kпр
            kind = e.get("definition_kind") or ("модель" if e.get("by_model") else "книга")
            if e.get("definition"):
                label = {"книга": "определение из книги, дословно", "модель": "определение написано моделью по утверждениям книги"}.get(kind, "")
                d_html = rich(e["definition"])
            else:
                label = "прямого определения в книге нет"
                d_html = ""
            related = (f"<p class='related'><span class='badge'>связано в книге</span> {rich(e['related'])}</p>"
                       if e.get("related") and not e.get("definition") else "")
            out.append({"slug": term_slug(e["term"]), "term": e["term"], "term_html": rich(e["term"]), "is_symbol": is_symbol,
                        "abbr": e.get("abbr", ""), "has_definition": bool(e.get("definition")),
                        "en": e.get("en", ""), "symbol": " / ".join(x for x in (e.get("symbol"), e.get("symbol_en")) if x),
                        "definition": d_html + related, "pages": pages_label(src.get("pages_pdf", [])),
                        "step_id": intro_step.get(k) or src.get("node_id"),
                        "note": "проверено человеком" if e.get("reviewed") else label})
    return sorted(out, key=lambda x: x["term"].lower())


def notation_for_course(entries: list[dict], chapter_text: str) -> list[dict]:
    """Таблица «русская школа ↔ западная ↔ кривая в данных» для обозначений, которые встречаются в главе."""
    low = chapter_text.lower()
    rows = []
    for e in entries:
        if not e.get("notation_group"):
            continue
        probes = [x for x in (e.get("abbr"), e.get("symbol"), e["term"], *e.get("aliases", [])) if x and len(x) >= 2]
        found = False
        for p in probes:
            pl = p.lower()
            if (re.fullmatch(r"[а-яё\- ]{6,}", pl) and pl in low) or re.search(rf"(?<![\wа-яё]){re.escape(pl)}(?![\wа-яё])", low):
                found = True
                break
        if not found:
            continue
        rows.append({"group": e["notation_group"], "ru": e.get("abbr") or e.get("symbol") or "", "ru_html": rich(e.get("abbr") or e.get("symbol") or ""),
                     "name": e["term"], "west": e.get("symbol_en", ""), "west_name": e.get("en", ""), "las": e.get("las", ""),
                     "unit": e.get("unit", ""), "note": e.get("notation_note", ""), "slug": term_slug(e["term"]),
                     "source": e.get("en_source", "")})
    order = ["Методы ГИС (кривые)", "Параметры пласта", "Сопротивления", "Контакты и зоны", "Подземная гидромеханика"]
    uniq = {}
    for r in rows:  # одно обозначение — одна строка
        uniq.setdefault((r["ru"], r["west"]), r)
    return sorted(uniq.values(), key=lambda r: (order.index(r["group"]) if r["group"] in order else 99, r["ru"]))


def formula_report(ch: Chapter) -> dict:
    """Проверка формул главы для новой книги: какие не перевелись в MathML (показаны исходником)
    и где внутри MathML остались непереведённые команды. Починка — строка в TEX_NORMALIZE."""
    units = _load(ch.work_dir / "units.json", [])
    total, failed, leftovers = 0, [], []
    for u in units:
        for m in MATH_RE.finditer(u["text"]):
            s = m.group(0)
            tex = s[2:-2] if s.startswith(("\\[", "\\(", "$$")) else s[1:-1]
            total += 1
            out = mathml(tex, False)
            if out.startswith("<code"):
                failed.append({"unit": u["id"], "page": u["page_pdf"], "tex": tex[:160]})
                continue
            cmds = sorted(set(re.findall(r"\\[a-zA-Z]+", re.sub(r"<[^>]+>", " ", out))))
            if cmds:
                leftovers.append({"unit": u["id"], "page": u["page_pdf"], "commands": cmds, "tex": tex[:160]})
    rep = {"formulas": total, "not_converted": len(failed), "with_leftover_commands": len(leftovers),
           "failed": failed[:50], "leftovers": leftovers[:50]}
    (ch.work_dir / "formula_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1), encoding="utf-8")
    return rep


def course_size_mb(path: Path) -> float:
    return sum(f.stat().st_size for f in path.parent.rglob("*") if f.is_file()) / 1e6
