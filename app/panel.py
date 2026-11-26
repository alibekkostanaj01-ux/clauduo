"""Панель команды «Глава → курс» — локальная оболочка над конвейером g2c.

Запуск:  uv run g2c panel   (открывается на http://127.0.0.1:8501, только на этом компьютере)
Панель ничего не считает сама: все этапы — те же команды g2c, что видит жюри. Данные — в файлах проекта.
"""
from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from g2c import jobs  # noqa: E402
from g2c.books import library, page_text_head, save_chapter, slugify, store_upload, toc_entries  # noqa: E402
from g2c.ingest import Unit  # noqa: E402
from g2c.mdbook import load_book  # noqa: E402
from g2c.paths import BOOKS_MD, DOMAIN, Chapter, load_chapters, parse_pages  # noqa: E402
from g2c.settings import config  # noqa: E402

st.set_page_config(page_title="Глава → курс", page_icon="📘", layout="wide")

DIFF = ["М", "В", "И", "Ф", "П", "Пр", "Н", "Р"]
DIFF_RU = {"М": "механизм", "В": "время", "И": "косвенное измерение", "Ф": "формализм", "П": "пространство",
           "Пр": "процедура", "Н": "неопределённость", "Р": "распознавание"}
KIND_RU = {"paragraph": "абзац", "figure": "рисунок", "formula": "формула", "table": "таблица",
           "worked_example": "пример", "task": "задача", "heading": "заголовок"}


# --- общие помощники -------------------------------------------------------------
def chapters() -> dict[str, Chapter]:
    return load_chapters()


def chapter_picker(key: str) -> Chapter | None:
    chs = chapters()
    if not chs:
        st.info("Глав пока нет — создайте на странице «Главы».")
        return None
    labels = {cid: f"{c.title[:80]}  [{c.split}, стр. {c.pages}]" for cid, c in chs.items()}
    default = st.session_state.get("chapter")
    ids = list(chs)
    cid = st.selectbox("Глава", ids, index=ids.index(default) if default in ids else 0,
                       format_func=lambda x: labels[x], key=key)
    st.session_state["chapter"] = cid
    return chs[cid]


def read_json(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def run_job(args: list[str], title: str) -> None:
    j = jobs.start(args, title)
    st.toast(f"Запущено: {title}")
    st.session_state["last_job"] = j["id"]


def quota_hint() -> None:
    st.caption("Этапы с пометкой 🤖 обращаются к Gemini. На бесплатном тарифе — 20 запросов в день на модель; "
               "готовые ответы кэшируются, и повторный запуск продолжает с места остановки.")


@st.fragment(run_every=3)
def jobs_panel(filter_text: str | None = None) -> None:
    js = [j for j in jobs.list_jobs() if not filter_text or filter_text in " ".join(j["args"])][:8]
    if not js:
        st.caption("Задач ещё не было.")
        return
    for j in js:
        icon = {"готово": "✅", "ошибка": "❌", "идёт": "⏳"}.get(j["status"], "•")
        with st.expander(f"{icon} {j['title']} — {j['status']} (старт {j['started']})",
                         expanded=j["status"] == "идёт" or j["id"] == st.session_state.get("last_job")):
            st.code(j["tail"] or "…", language=None)


# --- страницы -----------------------------------------------------------------------
def page_books() -> None:
    st.header("📚 Книги")
    st.write("Загрузите книгу: **PDF** (для сканов страниц) и, если есть, **md организаторов** с метками `<!-- page N -->`. "
             "Без md текст берётся из текстового слоя PDF; страницы-сканы и страницы с формулами распознаются через Gemini.")
    lib = library()
    rows = []
    for name, m in lib.items():
        n_pages = load_book(BOOKS_MD / name).max_page if (BOOKS_MD / name).exists() else 0
        rows.append({"книга": m.get("title", name), "md": name, "PDF": m.get("pdf") or "—", "страниц": n_pages,
                     "откуда": m.get("origin", ""), "нужно распознать": len(m.get("need_ocr", [])),
                     "с формулами": len(m.get("with_math", [])), "распознано": len(m.get("ocr_done", []))})
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    upload_book_ui("books")
    st.subheader("Распознать страницы через ИИ 🤖")
    names = [n for n, m in lib.items() if m.get("pdf")]
    if names:
        name = st.selectbox("Книга", names, format_func=lambda n: lib[n].get("title", n), key="ocr_book")
        ocr_ui(name, "books")
    jobs_panel("ocr")


def upload_book_ui(key: str) -> str | None:
    """Загрузка книги: PDF и/или md организаторов. Возвращает имя md загруженной книги."""
    with st.form(f"upload_{key}"):
        st.subheader("Загрузить книгу")
        title = st.text_input("Название (автор — книга — год)", placeholder="Иванов — Интерпретация ГИС — 2010")
        pdf = st.file_uploader("PDF книги", type=["pdf"], help="нужен для сканов страниц и распознавания формул")
        md = st.file_uploader("md с метками страниц <!-- page N --> (необязательно, как у организаторов)", type=["md"])
        ok = st.form_submit_button("Загрузить", type="primary")
    if not ok:
        return None
    if not title or (not pdf and not md):
        st.error("Нужны название и PDF (или md).")
        return None
    from g2c.books import import_md_only, import_pdf

    with tempfile.TemporaryDirectory() as td:
        md_path = None
        if md:
            md_path = Path(td) / "book.md"
            md_path.write_bytes(md.getvalue())
        try:
            if pdf:
                pdf_path = Path(td) / "book.pdf"
                pdf_path.write_bytes(pdf.getvalue())
                rep = import_pdf(pdf_path, title, md_path)
                st.success(f"Загружено: {rep.md}, {rep.pages} стр., оглавление PDF: {rep.toc_entries} пунктов.")
                if rep.pages_need_ocr:
                    st.warning(f"Сканы без текста: {len(rep.pages_need_ocr)} стр. ({_compact(rep.pages_need_ocr)}) — распознайте их ИИ ниже.")
                if rep.pages_with_math:
                    st.warning(f"Страницы с формулами: {len(rep.pages_with_math)} ({_compact(rep.pages_with_math)}) — "
                               "текстовый слой PDF портит формулы, лучше распознать их ИИ (формулы станут LaTeX).")
                return rep.md
            name = import_md_only(md_path, title)
            st.success(f"Загружено: {name} (без PDF сканов не будет).")
            return name
        except Exception as e:
            st.error(str(e))
            return None


def ocr_ui(name: str, key: str) -> None:
    """Распознавание страниц книги ИИ: скан → markdown, формулы в LaTeX, таблицы, описания рисунков."""
    m = library().get(name, {})
    if not m.get("pdf"):
        return
    st.caption("Скан страницы → текст: формулы в LaTeX, таблицы, подписи и описания рисунков. Один запрос к модели на страницу.")
    c2, c3 = st.columns([3, 1])
    suggest = _compact(sorted(set(m.get("need_ocr", [])) | set(m.get("with_math", []))))
    pages = c2.text_input("Страницы для распознавания", value=suggest[:60], help="например 12-15,20", key=f"ocr_pages_{key}")
    if m.get("ocr_done"):
        st.caption(f"Уже распознано: {_compact(m['ocr_done'])}")
    if c3.button("Распознать 🤖", use_container_width=True, disabled=not pages, key=f"ocr_btn_{key}"):
        run_job(["ocr", name, pages], f"Распознавание {name}: стр. {pages}")


def _compact(pages: list[int]) -> str:
    out, i = [], 0
    pages = sorted(pages)
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        out.append(str(pages[i]) if i == j else f"{pages[i]}-{pages[j]}")
        i = j + 1
    return ",".join(out)


def page_chapters() -> None:
    st.header("📑 Главы")
    chs = chapters()
    st.dataframe(pd.DataFrame([{"id": c.id, "выборка": c.split, "страницы": c.pages, "типы": " ".join(c.difficulty),
                                "предпосылки": c.prereq_pages or (c.prereq_file or "—"), "опечатки": c.errata_file or "—",
                                "название": c.title} for c in chs.values()]), hide_index=True, use_container_width=True)

    st.subheader("Новая глава из оглавления книги")
    lib = library()
    name = st.selectbox("Книга", list(lib), format_func=lambda n: lib[n].get("title", n), key="ch_book")
    chapter_form_ui(name, "chapters")


def chapter_form_ui(name: str, key: str) -> str | None:
    """Выбор главы из оглавления книги (страницы, границы, разрывы), предпосылки и лист опечаток. Возвращает id главы."""
    lib = library()
    entries = toc_entries(name)
    if not entries:
        st.warning("В md книги нет заголовков — задайте страницы вручную.")
    idx = st.selectbox("Раздел (начало главы)", range(len(entries)),
                       format_func=lambda i: f"стр. {entries[i].page}  {'  ' * (entries[i].level - 1)}{entries[i].title[:90]}",
                       key=f"toc_pick_{key}") if entries else None
    e = entries[idx] if idx is not None else None
    book_slug = slugify(lib[name].get("title", name).split("—")[0].split("-")[0])[:20]

    with st.form(f"chapter_{key}"):
        c1, c2 = st.columns(2)
        title = c1.text_input("Название главы для курса", value=(e.title if e else ""))
        split = c2.selectbox("Выборка", ["train", "ctrl", "test"], help="обучающая / контрольная / тестовая")
        c3, c4, c5 = st.columns(3)
        pages = c3.text_input("Страницы PDF", value=(f"{e.page}-{e.end_page}" if e else ""),
                              help="диапазоны через запятую, разрывы допустимы: 9-14,20-30")
        start_at = c4.text_input("Начинается с текста", value=(e.title if e else ""),
                                 help="если страница начала общая с предыдущим разделом")
        end_before = c5.text_input("Заканчивается перед текстом",
                                   value=(e.next_title if e and e.next_title and e.end_page == _page_of(entries, e.next_title) else ""),
                                   help="если последняя страница общая со следующим разделом")
        c6, c7 = st.columns(2)
        prefix = c6.text_input("Префикс id узлов", value=book_slug)
        cid = c7.text_input("id главы", value=f"{book_slug}_{slugify(e.title, 20) if e else 'gl'}")
        diff = st.multiselect("Типы трудности", DIFF, format_func=lambda d: f"{d} — {DIFF_RU[d]}")
        prereq_pages = st.text_input("Короткое чтение — страницы той же книги (предпосылки)", help="например 4-17,23-28")
        prereq_card = st.file_uploader("…или карточка-предпосылка (md/txt)", type=["md", "txt"])
        errata = st.file_uploader("Лист опечаток (txt/csv/md)", type=["txt", "csv", "md"],
                                  help="строки вида «стр. 95: неверно → верно» или «95;неверно;верно»")
        saved = st.form_submit_button("Сохранить главу", type="primary")
    result = None
    if saved:
        ch = {"id": cid.strip(), "prefix": prefix.strip(), "split": split, "title": title.strip(), "book_md": name,
              "book_pdf": lib[name].get("pdf"), "pages": pages.replace(" ", ""), "difficulty": diff,
              "start_at": start_at.strip() or None, "end_before": end_before.strip() or None,
              "prereq_pages": prereq_pages.replace(" ", "")}
        try:
            if prereq_card:
                ch["prereq_file"] = store_upload("prereq", ch["id"], prereq_card.name, prereq_card.getvalue())
            if errata:
                ch["errata_file"] = store_upload("errata", ch["id"], errata.name, errata.getvalue())
            save_chapter(ch)
            st.session_state["chapter"] = ch["id"]
            st.success(f"Глава {ch['id']} сохранена.")
            result = ch["id"]
        except Exception as ex:
            st.error(f"Не сохранено: {ex}")
    if e:
        with st.expander("Как выглядят первая и последняя страницы"):
            try:
                ps = parse_pages(pages) if pages else [e.page]
                st.text(f"стр. {ps[0]}:\n{page_text_head(name, ps[0], 600)}")
                st.text(f"стр. {ps[-1]} (конец):\n{load_book(BOOKS_MD / name).pages.get(ps[-1], '')[-600:]}")
            except ValueError:
                st.caption("Страницы заданы с ошибкой: пример 9-14,20-30")
    return result


def _page_of(entries, title):
    return next((x.page for x in entries if x.title == title), None)


def page_pipeline() -> None:
    st.header("▶️ Конвейер")
    ch = chapter_picker("pipe_ch")
    if not ch:
        return
    wd = ch.work_dir
    units = read_json(wd / "units.json", [])
    summary = read_json(wd / "layer_summary.json")
    c = st.columns(4)
    c[0].metric("Страниц", len(parse_pages(ch.pages)))
    c[1].metric("Единиц главы", len(units))
    c[2].metric("Узлов слоя", summary["nodes"] if summary else "—")
    c[3].metric("Утверждений", summary["claims"] if summary else "—")

    st.subheader("Этапы")
    steps = [
        ("1. Разобрать главу и сканы", ["ingest", ch.id, "--scans"], "единицы, задачи, описания рисунков, опечатки; без модели"),
        ("2. Учебный слой 🤖", ["layer", ch.id], "5 проходов: утверждения → план → предпосылки → поля → покрытие"),
        ("3. Глоссарий и ИТ-мосты 🤖", ["glossary", ch.id], "доменный пакет из слоя"),
        ("4. Задания 🤖", ["tasks", ch.id], "≥15 заданий 5 типов, ключи проверяет код"),
        ("Маршрутизация форматов", ["route", ch.id], "формат каждого узла по типу трудности и видеоёмкости; без модели"),
        ("Ролик-шортс 9:16 🤖", ["video", ch.id, "--fmt", "short"], "узел → спецификация и сценарий (ИИ) → проверка → рендер кодом → озвучка → паспорт"),
        ("Ролик-разбор 16:9 🤖", ["video", ch.id, "--fmt", "long"], "то же для разбора: решение на данных, синхронные виды"),
        ("Покрытие шаблонами", ["templates-coverage", ch.id], "какой шаблон закрывает каждый видеоёмкий узел или «нужен новый»"),
        ("5. Собрать курс", ["course", ch.id], "офлайн-сайт out/<глава>/index.html из слоя (с роликами); без модели"),
        ("Страница просмотра", ["preview", ch.id], "сканы + единицы + утверждения; без модели"),
        ("Проверить слой", ["check-layer", ch.id], "схема, цитаты, числа, термины, связи; без модели"),
    ]
    for label, args, hint in steps:
        a, b = st.columns([1, 3])
        if a.button(label, key="b" + "_".join(args), use_container_width=True):
            run_job(args, f"{label} — {ch.id}")
        b.caption(hint)
    course_idx = ROOT / "out" / ch.id / "index.html"
    if course_idx.exists():
        st.success(f"Курс собран: `{course_idx}` — откройте файл двойным клике в проводнике (работает без интернета).")
    st.caption("Дальше по плану: маршрутизация форматов и ролики — появятся здесь же.")
    quota_hint()
    st.subheader("Задачи")
    jobs_panel(ch.id)
    if summary:
        st.subheader("Итог слоя")
        st.json(summary)


def page_review() -> None:
    st.header("🔎 Проверка слоя")
    ch = chapter_picker("rev_ch")
    if not ch:
        return
    wd = ch.work_dir
    if not (wd / "units.json").exists():
        st.info("Сначала «Конвейер → 1. Разобрать главу».")
        return
    from g2c.layer import ClaimStore
    from g2c.preview import _cached_claims

    units = [Unit(**d) for d in read_json(wd / "units.json")]
    store = ClaimStore(ch, units, parse_pages(ch.pages) + parse_pages(ch.prereq_pages))
    layer = read_json(wd / "layer.json")
    node_title = {}
    if layer:
        src = "учебный слой"
        for n in layer["nodes"]:
            for cl in n["claims"]:
                r = store.add({k: cl[k] for k in ("unit_id", "page_pdf", "text", "kind", "quote", "source_kind")})
                node_title[r] = n["title"]
    else:
        src = "черновик прохода 1 (слой не собран)"
        for cl in _cached_claims(ch, units):
            store.add(cl)
    errs = {r: store.check(r) for r in list(store.claims)}
    issues = read_json(wd / "issues.json", [])
    cov = read_json(wd / "coverage.json")
    c = st.columns(4)
    c[0].metric("Утверждений", len(store.claims))
    c[1].metric("Цитата не на месте", sum(1 for v in errs.values() if v))
    c[2].metric("Ошибок проверки", len(issues) if layer else "—")
    if cov:
        done = sum(1 for r in cov["rows"] if r["node_ids"] or r["reason"])
        c[3].metric("Покрытие единиц", f"{done}/{len(cov['rows'])}")
    st.caption(f"Источник утверждений: {src}.")
    if issues:
        with st.expander(f"Ошибки проверки ({len(issues)})"):
            st.dataframe(pd.DataFrame(issues), hide_index=True, use_container_width=True)

    pages = sorted({u.page_pdf for u in units})
    p = st.select_slider("Страница PDF", options=pages)
    left, right = st.columns([2, 3])
    scan = wd / "scans" / f"p{p:03d}.png"
    if scan.exists():
        left.image(str(scan), use_container_width=True)
    else:
        left.info("Скана нет: «Конвейер → 1. Разобрать главу и сканы».")
    by_unit = {}
    for r, cl in store.claims.items():
        by_unit.setdefault(cl["unit_id"], []).append((r, cl))
    for u in [x for x in units if x.page_pdf == p]:
        flag = " ⚠ брак распознавания" if u.ocr_suspect else ""
        with right.container(border=True):
            st.caption(f"**{u.id}** · {KIND_RU.get(u.kind, u.kind)}{' · №' + u.task_number if u.task_number else ''}{flag}")
            st.text(u.text_fixed or u.text or "(без подписи автора)")
            if u.typo_fixes:
                st.caption("Исправлено по листу опечаток: " + "; ".join(f"{f['wrong']} → {f['right']}" for f in u.typo_fixes))
            if u.model_description:
                with st.expander("описание рисунка моделью"):
                    st.text(u.model_description)
            for r, cl in by_unit.get(u.id, []):
                ok = not errs.get(r)
                st.markdown(f"{'✅' if ok else '❌'} *{cl['kind']}* {('→ **' + node_title[r] + '**') if r in node_title else ''}  \n"
                            f"{cl['text']}  \n> «{cl['quote']}»" + ("" if ok else f"  \n:red[{'; '.join(errs[r])}]"))


def page_edit() -> None:
    st.header("✏️ Правка факта")
    st.write("Правится **учебный слой** — единственный источник правды; курс и ролики пересобираются из него. "
             "Каждая правка записывается в журнал с человеко-минутами и коммитится — так требует ТЗ.")
    ch = chapter_picker("edit_ch")
    if not ch:
        return
    from g2c.editing import find_node, load_layer, save_edit, validate_node

    if not (ch.work_dir / "layer.json").exists():
        st.info("Слоя ещё нет: «Конвейер → 2. Учебный слой».")
        return
    if "edit_t0" not in st.session_state:
        st.session_state["edit_t0"] = time.time()
    layer = load_layer(ch)
    nid = st.selectbox("Узел", [n["id"] for n in layer["nodes"]],
                       format_func=lambda i: f"{find_node(layer, i)['order']}. {find_node(layer, i)['title']}")
    node = find_node(layer, nid)
    target = st.radio("Что правим", ["утверждение", "поле узла"], horizontal=True)
    what = ""
    if target == "утверждение":
        ci = st.selectbox("Утверждение", range(len(node["claims"])),
                          format_func=lambda i: f"{node['claims'][i]['id'].split('#')[1]} · стр. {node['claims'][i]['page_pdf']} · {node['claims'][i]['text'][:90]}")
        cl = node["claims"][ci]
        cl["text"] = st.text_area("Текст утверждения", cl["text"])
        cl["quote"] = st.text_area("Цитата (дословно из книги)", cl["quote"])
        c1, c2 = st.columns(2)
        cl["page_pdf"] = int(c1.number_input("Страница PDF", value=int(cl["page_pdf"]), step=1))
        kinds = ["observation", "inference", "assumption"]
        cl["kind"] = c2.selectbox("Тип", kinds, index=kinds.index(cl["kind"]),
                                  format_func=lambda k: {"observation": "наблюдение", "inference": "вывод", "assumption": "допущение"}[k])
        what = f"утверждение {cl['id'].split('#')[1]}"
    else:
        fields = ["main", "mechanism", "practice", "nuances", "topic"]
        f = st.selectbox("Поле", fields, format_func=lambda k: {"main": "суть", "mechanism": "механизм", "practice": "практика",
                                                                "nuances": "нюансы", "topic": "тема"}[k])
        node["layers"][f]["text"] = st.text_area("Текст", node["layers"][f]["text"], height=200)
        node["layers"][f]["by_model"] = st.checkbox("написано моделью", value=node["layers"][f]["by_model"])
        what = f"поле {f}"
    issues = validate_node(ch, node)
    if issues:
        st.error("Проверка узла: " + "; ".join(f"{i.where}: {i.message}" for i in issues[:6]))
    else:
        st.success("Проверка узла пройдена: цитаты на месте, числа из книги.")
    elapsed = round((time.time() - st.session_state["edit_t0"]) / 60, 1)
    c1, c2 = st.columns(2)
    minutes = c1.number_input("Человеко-минуты", value=max(1.0, elapsed), step=1.0,
                              help="таймер идёт с открытия страницы; два человека по 10 мин — 20")
    reason = c2.text_input("Почему понадобилась правка", placeholder="конвейер взял число с соседней страницы")
    commit = st.checkbox("Закоммитить слой и журнал", value=True)
    if st.button("Сохранить правку", type="primary", disabled=not reason):
        res = save_edit(ch, layer, nid, what, minutes, reason, commit=commit)
        if res["ok"]:
            st.success(f"Сохранено. Журнал: {res['journal']['time']}, {minutes} чел.-мин. Коммит: {res['commit'] or 'не делали'}")
            st.session_state["edit_t0"] = time.time()
        else:
            st.error(f"Не сохранено — слой не проходит схему: {res['issues'][:3]}")
    st.caption("Пересборка страницы шага и ролика после правки появится вместе со сборщиком курса.")


def page_review_module() -> None:
    st.header("🧪 Модуль проверки")
    st.write("Принимает внешний пакет по главе (`review_package.schema.json`) и выдаёт замечания с исправлением и страницей. "
             "Сам модуль — следующий этап разработки; сейчас пакет можно проверить на соответствие схеме.")
    up = st.file_uploader("package.json", type=["json"])
    if up:
        from g2c.validate import check_schema

        pkg = json.loads(up.getvalue().decode("utf-8"))
        iss = check_schema(pkg, "review_package")
        if iss:
            st.error(f"Пакет не по схеме: {len(iss)} ошибок")
            st.dataframe(pd.DataFrame([i.as_dict() for i in iss]), hide_index=True)
        else:
            kinds = pd.Series([i["type"] for i in pkg["items"]]).value_counts().to_dict()
            st.success(f"Пакет {pkg['package_id']} по схеме: {len(pkg['items'])} элементов {kinds}, кадров {len(pkg['frames'])}.")


def page_metrics() -> None:
    st.header("📊 Метрики")
    from g2c.llm import USAGE_FILE
    from g2c.metrics import EDITS_FILE, build_metrics

    if USAGE_FILE.exists():
        u = pd.DataFrame([json.loads(x) for x in USAGE_FILE.read_text(encoding="utf-8").splitlines()])
        real = u[~u["cached"]]
        c = st.columns(4)
        c[0].metric("Запросов к модели", len(real))
        c[1].metric("Токены вход / выход", f"{int(real['tokens_in'].sum()):,} / {int(real['tokens_out'].sum()):,}".replace(",", " "))
        money = real["cost_usd"].dropna().sum() if real["cost_usd"].notna().any() else None
        c[2].metric("Деньги, $", f"{money:.2f}" if money is not None else "цены не заданы")
        c[3].metric("Ответов из кэша", int(u["cached"].sum()))
        today = real[real["time"].str.startswith(time.strftime("%Y-%m-%d"))]
        st.caption(f"Сегодня реальных запросов: {len(today)} (бесплатный тариф: 20 в день на модель).")
        st.dataframe(real.groupby(["chapter", "stage"])[["tokens_in", "tokens_out", "seconds"]].sum().reset_index(),
                     hide_index=True, use_container_width=True)
    else:
        st.info("Запросов к модели ещё не было.")
    st.subheader("Журнал ручных правок")
    if EDITS_FILE.exists():
        st.dataframe(pd.read_csv(EDITS_FILE), hide_index=True, use_container_width=True)
    if st.button("Собрать metrics.csv"):
        st.success(f"metrics.csv: {len(build_metrics())} строк")


# --- шестерёнка: ключ API и модель ИИ ----------------------------------------------------------------

@st.dialog("⚙️ Настройки ИИ")
def settings_dialog() -> None:
    from g2c import settings as S

    cur_key, cur_model = S.current_ai()
    st.caption("Ключ хранится только на этом компьютере (файл `.env`, в git не попадает).")
    key = st.text_input("Ключ API", type="password", placeholder=f"сейчас: {S.mask(cur_key)}" if cur_key else "вставьте ключ",
                        help="оставьте пустым, чтобы не менять текущий")
    model = st.text_input("Модель ИИ", value=cur_model, help="название модели, например gemini-3.8-flash")
    c1, c2 = st.columns(2)
    if c1.button("🔌 Проверить", use_container_width=True):
        from g2c.llm import check_model

        with st.spinner("Отправляю короткий запрос модели…"):
            r = check_model(key.strip() or cur_key, model.strip())
        if r["ok"]:
            st.success(f"ИИ работает: ответил «{r['answer']}» за {r['seconds']} с")
        else:
            st.error(f"ИИ не ответил{': ' + r['hint'] if r.get('hint') else ''}.")
            st.caption(r.get("error", ""))
    if c2.button("💾 Сохранить", type="primary", use_container_width=True, disabled=not model.strip()):
        S.save_ai(key, model)
        st.success("Сохранено")
        st.rerun()


def top_bar(title: str) -> None:
    """Заголовок раздела + шестерёнка настроек в правом углу."""
    from g2c.settings import config as cfg

    cfg.cache_clear()
    c1, c2 = st.columns([12, 1])
    c1.header(title)
    if c2.button("⚙️", help="Настройки: ключ API и модель ИИ", key="gear"):
        settings_dialog()
    model = cfg()["llm"]["roles"].get("structurer", "—")
    c1.caption(f"Модель ИИ: `{model}` · настройки — шестерёнка справа")


# --- мастер: создать курс ---------------------------------------------------------------------------
def _last_build(ch_id: str) -> dict | None:
    js = [j for j in jobs.list_jobs() if j["args"][:2] == ["build", ch_id]]
    if not js:
        return None
    j = js[0]
    text = Path(j["log"]).read_text(encoding="utf-8", errors="replace") if Path(j["log"]).exists() else ""
    stages = re.findall(r"### ЭТАП (\d+)/(\d+): (.+)", text)
    skipped = re.findall(r"### ПРОПУЩЕНО: (.+)", text)
    return {"job": j, "stage": stages[-1] if stages else None, "skipped": skipped, "text": text}


@st.fragment(run_every=3)
def build_progress(ch_id: str) -> None:
    b = _last_build(ch_id)
    if not b:
        st.caption("Курс ещё не собирали.")
        return
    j = b["job"]
    if b["stage"]:
        i, n, name = int(b["stage"][0]), int(b["stage"][1]), b["stage"][2]
        done = j["status"] in ("готово", "ошибка")
        st.progress(1.0 if done else (i - 1) / n, text=("Готово" if j["status"] == "готово" else
                                                       "Остановлено с ошибкой" if j["status"] == "ошибка" else f"Этап {i} из {n}: {name}"))
    for s in b["skipped"]:
        st.warning(f"Пропущено: {s}")
    with st.expander(f"Журнал сборки (старт {j['started']})", expanded=j["status"] == "идёт"):
        st.code(j["tail"] or "…", language=None)


def page_create() -> None:
    top_bar("🎓 Создать курс")
    st.write("Четыре шага: книга → глава → сборка → результат. Всё работает на этом компьютере; "
             "к ИИ уходят только тексты глав (сценарии роликов — в синтез голоса Microsoft).")
    lib = library()

    # 1. книга
    st.subheader("1. Книга")
    names = list(lib)
    tab_pick, tab_new = st.tabs(["Выбрать загруженную", "Загрузить новую (PDF / md)"])
    with tab_new:
        new = upload_book_ui("wizard")
        if new:
            st.session_state["wiz_book"] = new
            st.rerun()
    with tab_pick:
        if not names:
            st.info("Книг пока нет — загрузите на соседней вкладке.")
            return
        cur = st.session_state.get("wiz_book")
        book = st.selectbox("Книга", names, index=names.index(cur) if cur in names else 0,
                            format_func=lambda n: f"{lib[n].get('title', n)}  ·  {lib[n].get('origin', '')}", key="wiz_book_sel")
        st.session_state["wiz_book"] = book
        m = lib[book]
        if m.get("need_ocr") or m.get("with_math"):
            with st.expander("Распознать сканы и страницы с формулами ИИ (рекомендуется)"):
                ocr_ui(book, "wizard")
    book = st.session_state["wiz_book"]

    # 2. глава
    st.subheader("2. Глава")
    own = {cid: c for cid, c in chapters().items() if c.book_md == book}
    tab_ch, tab_newch = st.tabs([f"Главы этой книги ({len(own)})", "Новая глава из оглавления"])
    with tab_newch:
        new_ch = chapter_form_ui(book, "wizard")
        if new_ch:
            st.session_state["chapter"] = new_ch
            st.rerun()
    with tab_ch:
        if not own:
            st.info("Глав этой книги ещё нет — создайте на соседней вкладке.")
            return
        ids = list(own)
        cur = st.session_state.get("chapter")
        cid = st.selectbox("Глава", ids, index=ids.index(cur) if cur in ids else 0,
                           format_func=lambda x: f"{own[x].title[:90]}  [стр. {own[x].pages}]", key="wiz_ch")
        st.session_state["chapter"] = cid
        ch = own[cid]
        st.caption(f"Страницы {ch.pages}; предпосылки: {ch.prereq_pages or ch.prereq_file or 'нет'}; "
                   f"лист опечаток: {ch.errata_file or 'нет'}; типы трудности: {' '.join(ch.difficulty) or '—'}")
    ch = own[st.session_state["chapter"]] if st.session_state.get("chapter") in own else None
    if ch is None:
        return

    # 3. сборка
    st.subheader("3. Сборка курса")
    c1, c2, c3 = st.columns(3)
    video = c1.checkbox("Ролик-шортс по главе", value=True, help="узел → сценарий ИИ → кадры кодом → голос → паспорт")
    llm_gl = c2.checkbox("Глоссарий: EN и определения от ИИ", value=True)
    workers = c3.number_input("Параллельных запросов к ИИ", 1, 8, 4)
    st.caption("Этапы: разбор главы и сканы → учебный слой (ИИ) → маршрутизация → глоссарий → задания (ИИ) → ролик (ИИ) → сайт. "
               "Если квота ИИ кончится, шаги с ИИ пропустятся с пометкой, а курс соберётся из готового; повторный запуск продолжит с места.")
    b = _last_build(ch.id)
    running = bool(b and b["job"]["status"] == "идёт")
    if st.button("🚀 Собрать курс" if not running else "⏳ Сборка идёт…", type="primary", disabled=running):
        args = ["build", ch.id, "--workers", str(int(workers))] + ([] if video else ["--no-video"]) + ([] if llm_gl else ["--no-llm-glossary"])
        run_job(args, f"Сборка курса — {ch.id}")
        st.rerun()
    build_progress(ch.id)

    # 4. результат
    st.subheader("4. Результат")
    idx = ROOT / "out" / ch.id / "index.html"
    if not idx.exists():
        st.caption("Курс появится здесь после сборки.")
        return
    summ = read_json(ch.work_dir / "layer_summary.json") or {}
    layer = read_json(ch.work_dir / "layer.json") or {"nodes": []}
    tasks = (read_json(ch.work_dir / "tasks.json") or {}).get("tasks", [])
    cov = read_json(ch.work_dir / "coverage.json") or {"rows": []}
    node_ids = {n["id"] for n in layer["nodes"]}
    vids = [p.with_name(p.name.replace(".passport.json", ".mp4")) for p in sorted((ROOT / "out" / "video").glob(f"{ch.id}.*.passport.json"))
            if (read_json(p) or {}).get("node_id") in node_ids]
    m = st.columns(5)
    m[0].metric("Шагов", len(layer["nodes"]))
    m[1].metric("Мыслей автора", sum(len(n["claims"]) for n in layer["nodes"]))
    m[2].metric("Заданий", len(tasks))
    m[3].metric("Роликов", len(vids))
    done = sum(1 for r in cov["rows"] if r["node_ids"] or r["reason"])
    m[4].metric("Покрытие главы", f"{done}/{len(cov['rows'])}" if cov["rows"] else "—")
    if "ЧАСТИЧНЫЙ" in layer.get("generated_by", ""):
        st.warning("Слой собран частично (прервался прогон ИИ) — пересоберите, когда будет квота.")
    c1, c2, c3 = st.columns(3)
    if c1.button("🌐 Открыть курс в браузере", type="primary", use_container_width=True):
        import webbrowser

        webbrowser.open(idx.as_uri())
    if c2.button("📁 Открыть папку курса", use_container_width=True):
        os.startfile(str(idx.parent)) if hasattr(os, "startfile") else None
    if vids and c3.button("🎬 Открыть ролик", use_container_width=True):
        os.startfile(str(vids[0])) if hasattr(os, "startfile") else None
    st.caption(f"Файл курса: `{idx}` — открывается двойным кликом, без интернета. Проверка и правка фактов — разделы слева.")


def page_courses() -> None:
    top_bar("📂 Мои курсы")
    import time as _t

    out = ROOT / "out"
    chs = chapters()
    built = [d for d in sorted(out.iterdir()) if d.is_dir() and not d.name.startswith("_") and (d / "index.html").exists()] if out.exists() else []
    if not built:
        st.info("Собранных курсов пока нет — создайте на вкладке «🎓 Создать курс».")
        return
    st.caption(f"Собрано курсов: {len(built)}. Каждый — папка с index.html: открывается двойным кликом, без интернета; можно копировать на флешку.")
    for d in sorted(built, key=lambda p: (p / "index.html").stat().st_mtime, reverse=True):
        ch = chs.get(d.name)
        wd = ROOT / "work" / d.name
        layer = read_json(wd / "layer.json") or {"nodes": [], "generated_by": ""}
        tasks = (read_json(wd / "tasks.json") or {}).get("tasks", [])
        node_ids = {n["id"] for n in layer["nodes"]}
        vids = [p for p in sorted((out / "video").glob(f"{d.name}.*.passport.json")) if (out / "video").exists()
                and (read_json(p) or {}).get("node_id") in node_ids]  # только ролики, стоящие на шагах этого курса
        size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1e6
        when = _t.strftime("%d.%m.%Y %H:%M", _t.localtime((d / "index.html").stat().st_mtime))
        with st.container(border=True):
            st.markdown(f"#### {ch.title if ch else d.name}")
            st.caption(f"{ch.book_md if ch else ''} · стр. {ch.pages if ch else '—'} · собран {when} · {size:.1f} МБ"
                       + (" · ⚠ слой частичный" if "ЧАСТИЧНЫЙ" in layer.get("generated_by", "") else ""))
            m = st.columns(4)
            m[0].metric("Шагов", len(layer["nodes"]))
            m[1].metric("Мыслей автора", sum(len(n["claims"]) for n in layer["nodes"]))
            m[2].metric("Заданий", len(tasks))
            m[3].metric("Роликов", len(vids))
            b = st.columns(4)
            if b[0].button("🌐 Открыть", key=f"open_{d.name}", type="primary", use_container_width=True):
                import webbrowser

                webbrowser.open((d / "index.html").as_uri())
            if b[1].button("📁 Папка", key=f"dir_{d.name}", use_container_width=True) and hasattr(os, "startfile"):
                os.startfile(str(d))
            if b[2].button("🔄 Пересобрать сайт", key=f"rb_{d.name}", use_container_width=True,
                           help="без ИИ: заново собрать сайт из текущего слоя (после правок фактов)"):
                run_job(["course", d.name], f"Пересборка сайта — {d.name}")
            if ch and b[3].button("🚀 Собрать заново", key=f"full_{d.name}", use_container_width=True,
                                  help="весь конвейер с ИИ (готовое берётся из кэша)"):
                st.session_state["chapter"] = d.name
                run_job(["build", d.name], f"Сборка курса — {d.name}")
    jobs_panel()


PAGES = {"🎓 Создать курс": page_create, "📂 Мои курсы": page_courses, "📚 Книги": page_books, "📑 Главы": page_chapters, "▶️ Конвейер (по шагам)": page_pipeline,
         "🔎 Проверка слоя": page_review, "✏️ Правка факта": page_edit, "🧪 Модуль проверки": page_review_module,
         "📊 Метрики": page_metrics}

with st.sidebar:
    st.title("Глава → курс")
    choice = st.radio("Раздел", list(PAGES), label_visibility="collapsed")
    st.caption("Работает только на этом компьютере. Главы книг не публикуются.")
    if st.button("⚙️ Настройки ИИ", use_container_width=True, key="gear_side"):
        settings_dialog()
if choice in ("🎓 Создать курс", "📂 Мои курсы"):
    PAGES[choice]()
else:
    c1, c2 = st.columns([12, 1])
    if c2.button("⚙️", help="Настройки: ключ API и модель ИИ", key="gear_top"):
        settings_dialog()
    PAGES[choice]()
