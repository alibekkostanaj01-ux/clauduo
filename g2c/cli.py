"""Команда g2c — единственная точка запуска конвейера.

    uv run g2c chapters                 список глав
    uv run g2c toc train_latyshova.md   оглавление книги со страницами
    uv run g2c ingest <глава>           глава → единицы (units.json) + черновик покрытия
    uv run g2c scans <глава>            сканы страниц главы из PDF
    uv run g2c check-schema <файл> <схема>
    uv run g2c check-layer <глава> [--layer путь]
    uv run g2c llm-ping                 проверить ключ и модель
    uv run g2c journal ...              записать ручную правку
    uv run g2c metrics                  собрать metrics.csv
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import ingest as ingest_mod
from .mdbook import load_book, toc as toc_fn
from .paths import BOOKS_MD, DOMAIN, get_chapter, load_chapters, parse_pages

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

app = typer.Typer(add_completion=False, help="Конвейер «Глава → курс»", no_args_is_help=True)
con = Console()


@app.command()
def chapters():
    """Список глав из chapters.json."""
    t = Table("id", "выборка", "страницы", "типы", "название")
    for c in load_chapters().values():
        t.add_row(c.id, c.split, c.pages, " ".join(c.difficulty), c.title[:70])
    con.print(t)


@app.command()
def toc(book: str, page_from: int = 0, page_to: int = 100000):
    """Оглавление книги (заголовки md) с номерами страниц PDF."""
    b = load_book(BOOKS_MD / book)
    for p, lvl, title in toc_fn(b):
        if page_from <= p <= page_to:
            con.print(f"{p:>4}  {'  ' * (lvl - 1)}{title}", markup=False)


@app.command()
def ingest(chapter: str, with_scans: bool = typer.Option(False, "--scans", help="заодно отрендерить сканы страниц")):
    """Разобрать главу на единицы: work/<глава>/units.json, prereq_units.json, coverage.draft.json."""
    ch = get_chapter(chapter)
    stats = ingest_mod.ingest(ch)
    con.print(stats)
    if with_scans:
        scans(chapter)


@app.command()
def scans(chapter: str, dpi: int = 150):
    """Сканы страниц главы из PDF в work/<глава>/scans/."""
    from .scans import pdf_page_count, render_pages

    ch = get_chapter(chapter)
    n = pdf_page_count(ch.pdf_path)
    md_max = load_book(ch.md_path).max_page
    if n != md_max:
        con.print(f"[yellow]Внимание: в PDF {n} стр., в md последняя метка — {md_max}. Сверьте нумерацию.[/]")
    files = render_pages(ch, dpi=dpi)
    con.print(f"Готово: {len(files)} сканов в {files[0].parent if files else '—'}")



def _workers(n: int) -> int:
    """0 — значение по умолчанию из config.yaml → llm.workers."""
    from .settings import config

    return n if n > 0 else int(config()["llm"].get("workers", 6))


@app.command()
def layer(chapter: str, workers: int = typer.Option(0, help="параллельных запросов к модели (0 — из config.yaml → llm.workers)")):
    """Построить учебный слой главы: work/<глава>/layer.json, coverage.json, issues.json."""
    from .layer import build_layer

    ch = get_chapter(chapter)
    if not (ch.work_dir / "units.json").exists():
        ingest_mod.ingest(ch)
    build_layer(ch, workers=_workers(workers))


@app.command()
def glossary(chapters: list[str] = typer.Argument(None, help="главы со слоем; по умолчанию — все, у которых есть layer.json"),
             workers: int = 0):
    """Глоссарий RU/EN и ИТ-мосты доменного пакета из учебного слоя (domain/<домен>/glossary.*, it_bridges.md)."""
    from .glossary import build_glossary, build_it_bridges

    chs = [get_chapter(c) for c in chapters] if chapters else \
        [c for c in load_chapters().values() if (c.work_dir / "layer.json").exists()]
    if not chs:
        con.print("[red]Нет глав с готовым слоем: сначала g2c layer <глава>[/]")
        raise typer.Exit(1)
    con.print(build_glossary(chs, workers=_workers(workers)))
    con.print(f"ИТ-мостов: {build_it_bridges(chs)}")


@app.command()
def preview(chapter: str):
    """Страница просмотра главы для команды: сканы, единицы, утверждения и проверка цитат (без запросов к модели)."""
    from .preview import build_preview

    ch = get_chapter(chapter)
    if not (ch.work_dir / "units.json").exists():
        ingest_mod.ingest(ch)
    con.print(f"Готово: {build_preview(ch)}")


@app.command()
def route(chapter: str):
    """Маршрутизация форматов: work/<глава>/routing.json (правилами, без модели)."""
    from .routing import build_routing

    con.print(build_routing(get_chapter(chapter)))


@app.command()
def tasks(chapter: str, workers: int = 0):
    """Задания курса из учебного слоя: work/<глава>/tasks.json (≥15, ≥4 типов, ключи проверены кодом)."""
    from .tasks import build_tasks

    build_tasks(get_chapter(chapter), workers=_workers(workers))


@app.command()
def course(chapter: str, layer_file: Path = typer.Option(None, "--layer", help="другой слой (по умолчанию work/<глава>/layer.json)"),
           tasks_file: Path = typer.Option(None, "--tasks", help="другие задания"),
           out: Path = typer.Option(None, help="папка курса (по умолчанию out/<глава>)")):
    """Собрать демо-курс главы из учебного слоя: out/<глава>/index.html (офлайн, двойной клик)."""
    from .course import build_course, course_size_mb
    from .validate import check_schema

    ch = get_chapter(chapter)
    if tasks_file:
        iss = check_schema(json.loads(tasks_file.read_text(encoding="utf-8")), "task")
        if iss:
            _report(iss)
    idx = build_course(ch, layer_file, tasks_file, out)
    size = course_size_mb(idx)
    con.print(f"Курс: {idx}  ({size:.1f} МБ{' — больше лимита 30 МБ!' if size > 30 else ''})")
    from .course import formula_report

    fr = formula_report(ch)
    if fr["not_converted"] or fr["with_leftover_commands"]:
        con.print(f"[yellow]Формулы: {fr['formulas']}, не перевелись {fr['not_converted']}, с непереведёнными командами "
                  f"{fr['with_leftover_commands']} — см. work/{ch.id}/formula_report.json[/]")
    else:
        con.print(f"Формулы: все {fr['formulas']} показаны в математическом виде")


@app.command()
def build(chapter: str,
          video: bool = typer.Option(True, help="делать ролик-шортс по лучшему видео-узлу"),
          llm_glossary: bool = typer.Option(True, help="дополнить глоссарий моделью (EN, определения)"),
          workers: int = typer.Option(0, help="параллельных запросов к модели (0 — из config.yaml → llm.workers)")):
    """Собрать курс главы целиком: разбор → слой → маршрутизация → глоссарий → задания → ролик → сайт.
    Если модель недоступна (квота), шаги с моделью пропускаются с пометкой, а курс собирается из того, что есть."""
    import traceback

    from .llm import LLMError

    ch = get_chapter(chapter)
    workers = _workers(workers)
    steps = ["Разбор главы и сканы", "Учебный слой", "Маршрутизация форматов", "Глоссарий", "Задания", "Ролик", "Сайт курса"]
    done, skipped = [], []

    def stage(i: int, name: str):
        con.print(f"### ЭТАП {i}/{len(steps)}: {name}", markup=False)

    def run(i, name, fn, needs_model=False):
        stage(i, name)
        try:
            fn()
            done.append(name)
            return True
        except LLMError as e:
            skipped.append(f"{name}: модель недоступна — {str(e)[:160]}")
        except Exception as e:  # шаг упал — курс всё равно собираем
            skipped.append(f"{name}: {type(e).__name__}: {str(e)[:160]}")
            con.print(traceback.format_exc()[-1500:], markup=False)
        con.print(f"!!! пропущено: {skipped[-1]}", markup=False)
        return False

    def s1():
        con.print(ingest_mod.ingest(ch))
        scans(chapter)

    def s2():
        from .layer import build_layer, build_partial_layer

        try:
            build_layer(ch, workers=workers)
        except Exception:
            if (ch.work_dir / "pass2_plan.json").exists():  # прерванный прогон: собрать слой из готового
                con.print("Слой собран частично из уже посчитанного", markup=False)
                con.print(build_partial_layer(ch))
            raise

    def s3():
        from .routing import build_routing

        con.print(build_routing(ch))

    def s4():
        from .glossary import build_glossary, build_glossary_offline, build_it_bridges

        con.print(build_glossary_offline([ch]))
        build_it_bridges([ch])
        if llm_glossary:
            con.print(build_glossary([ch], workers=workers))

    def s5():
        from .tasks import build_tasks

        build_tasks(ch, workers=workers)

    def s6():
        from .video.assemble import render_video
        from .video.planner import best_video_node, plan_video

        node = best_video_node(chapter, "short")
        if not node:
            raise RuntimeError("маршрутизация не дала узла для ролика")
        spec, errs = plan_video(chapter, node, "short")
        con.print(f"Сценарий: {spec.planned_by}; замечаний до рендера: {len(errs)}", markup=False)
        con.print(render_video(ROOT / "work" / chapter / "video" / spec.video_id / "spec.json", force=bool(errs)))

    def s7():
        from .course import build_course, course_size_mb, formula_report

        idx = build_course(ch)
        con.print(f"Курс: {idx} ({course_size_mb(idx):.1f} МБ)", markup=False)
        fr = formula_report(ch)
        con.print(f"Формулы: {fr['formulas']}, не перевелись {fr['not_converted']}", markup=False)

    from .paths import ROOT

    run(1, steps[0], s1)
    layer_ok = run(2, steps[1], s2, True) or (ch.work_dir / "layer.json").exists()
    if layer_ok:
        run(3, steps[2], s3)
        run(4, steps[3], s4, True)
        run(5, steps[4], s5, True)
        if video:
            run(6, steps[5], s6, True)
        else:
            stage(6, steps[5]); skipped.append("Ролик: выключен")
        run(7, steps[6], s7)
    con.print(f"### ИТОГ: выполнено {len(done)} из {len(steps)}", markup=False)
    for s in skipped:
        con.print(f"### ПРОПУЩЕНО: {s}", markup=False)
    if not (ch.work_dir / "layer.json").exists():
        raise typer.Exit(1)


@app.command("video-plan")
def video_plan(chapter: str, node: str = typer.Option(None, help="id узла; по умолчанию — лучший видео-узел из маршрутизации"),
               fmt: str = typer.Option("short", help="short (9:16) | long (16:9)"),
               no_llm: bool = typer.Option(False, "--no-llm", help="запасной сценарий без модели"),
               template: str = typer.Option(None, help="шаблон сцены, напр. T1_well_log"),
               layer_file: str = typer.Option("", "--layer", help="другой слой")):
    """Спецификация сцены + сценарий ролика (ИИ по API) + лист для ментора: work/<глава>/video/<id>/."""
    from .video.planner import best_video_node, plan_video

    node = node or best_video_node(chapter, fmt)
    if not node:
        con.print("[red]Нет узла для ролика: сначала g2c route <глава> или укажите --node[/]")
        raise typer.Exit(1)
    spec, errs = plan_video(chapter, node, fmt, use_llm=not no_llm, template_id=template, layer_file=layer_file)
    con.print(f"Спецификация: work/{chapter}/video/{spec.video_id}/spec.json · шаблон {spec.template} · {spec.planned_by}")
    con.print("[green]Проверка до рендера пройдена[/]" if not errs else f"[yellow]Замечания ({len(errs)}):[/] " + "; ".join(errs))


@app.command("video-render")
def video_render(spec: Path, force: bool = typer.Option(False, help="рендерить, даже если проверка не пройдена"),
                 require_mentor: bool = typer.Option(False, help="только после подписи ментора")):
    """Собрать ролик из спецификации одной командой: out/video/<id>.mp4 + .srt + паспорт."""
    from .video.assemble import render_video

    res = render_video(spec, force=force, require_mentor=require_mentor)
    con.print(res)
    if not res["ok"]:
        raise typer.Exit(1)


@app.command()
def video(chapter: str, fmt: str = typer.Option("short"), node: str = typer.Option(None), no_llm: bool = typer.Option(False, "--no-llm")):
    """Ролик целиком: выбор узла → спецификация и сценарий → проверка → рендер → паспорт."""
    video_plan(chapter, node, fmt, no_llm, None, "")
    from .video.planner import best_video_node

    n = node or best_video_node(chapter, fmt)
    vid = f"{chapter}.{n.split('.')[-1]}.{fmt}"[:90]
    video_render(Path("work") / chapter / "video" / vid / "spec.json", False, False)


@app.command("templates-coverage")
def templates_coverage(chapter: str):
    """Отчёт о покрытии шаблонами: какой шаблон закрывает каждый видеоёмкий узел или «нужен новый»."""
    from .video.templates import pick_template

    layer = json.loads((get_chapter(chapter).work_dir / "layer.json").read_text(encoding="utf-8"))
    routes = {r["node_id"]: r for r in json.loads((get_chapter(chapter).work_dir / "routing.json").read_text(encoding="utf-8"))["routes"]}
    rows = []
    for n in layer["nodes"]:
        r = routes.get(n["id"], {})
        if r.get("format") not in ("шортс", "длинное видео", "интерактив", "практикум на данных"):
            continue
        tpl, why = pick_template(n, "short")
        rows.append({"node_id": n["id"], "title": n["title"], "format": r.get("format"), "template": tpl.id,
                     "state_model": tpl.name, "status": "нужен новый" if tpl.id == "T0_schema_cards" else "закрыт", "why": why})
    out = get_chapter(chapter).work_dir / "templates_coverage.json"
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    t = Table("узел", "формат", "шаблон", "статус")
    for r in rows:
        t.add_row(r["title"][:50], r["format"], r["template"], r["status"])
    con.print(t)
    con.print(f"Записано: {out}")


@app.command("import-book")
def import_book(pdf: Path, title: str, md: Path = typer.Option(None, help="md с метками страниц, если есть")):
    """Загрузить книгу: PDF (+ md организаторов). Без md текст берётся из текстового слоя PDF."""
    from .books import import_pdf

    con.print(import_pdf(pdf, title, md))


@app.command()
def ocr(book_md: str, pages: str):
    """Распознать страницы книги через Gemini (формулы → LaTeX): g2c ocr kniga.md 12-15,20"""
    from .ocr import ocr_pages

    ocr_pages(book_md, parse_pages(pages), progress=lambda m: con.print(m))


@app.command()
def panel(port: int = 8501):
    """Открыть панель команды в браузере (только на этом компьютере)."""
    import subprocess

    from .paths import ROOT

    subprocess.run([sys.executable, "-m", "streamlit", "run", str(ROOT / "app" / "panel.py"),
                    "--server.address", "127.0.0.1", "--server.port", str(port), "--server.headless", "true",
                    "--browser.gatherUsageStats", "false"], cwd=ROOT)


@app.command("check-schema")
def check_schema_cmd(file: Path, schema: str = typer.Argument(..., help="node | coverage | routing | task | illustration | passport | review_package | review_output")):
    """Проверить JSON-файл по схеме организаторов."""
    from .validate import check_schema

    issues = check_schema(json.loads(file.read_text(encoding="utf-8")), schema)
    _report(issues)


@app.command("check-layer")
def check_layer(chapter: str, layer: Path = typer.Option(None, help="по умолчанию work/<глава>/layer.json")):
    """Проверить учебный слой: схема, цитаты на страницах, числа, порядок терминов, связи."""
    from .validate import validate_layer

    ch = get_chapter(chapter)
    layer = layer or ch.work_dir / "layer.json"
    pages = parse_pages(ch.pages) + parse_pages(ch.prereq_pages)
    issues = validate_layer(layer, ch.md_path, DOMAIN / ch.domain / "misconceptions.json", pages)
    _report(issues)


@app.command("llm-ping")
def llm_ping(role: str = "fast"):
    """Проверить, что ключ и модель работают (короткий запрос, пишет строку в usage.jsonl)."""
    from .llm import generate

    text, u = generate("Ответь одним словом: работает?", role=role, stage="ping", use_cache=False)
    con.print(f"Модель {u.model}: «{text.strip()[:60]}», токены {u.tokens_in}/{u.tokens_out}, {u.seconds} с")


@app.command()
def journal(chapter: str, file: str, what: str, minutes: float,
            people: int = 1, run_no: int = 1, reason: str = ""):
    """Записать ручную правку в manual_edits.csv (обязательно на тесте)."""
    from .metrics import add_edit

    con.print(add_edit(chapter, file, what, minutes, people, run_no, reason))


@app.command()
def metrics():
    """Собрать metrics.csv из work/usage.jsonl и manual_edits.csv."""
    from .metrics import build_metrics

    rows = build_metrics()
    con.print(f"metrics.csv: {len(rows)} строк")


def _report(issues) -> None:
    if not issues:
        con.print("[green]Ошибок нет[/]")
        return
    t = Table("где", "проверка", "что", "подсказка")
    for i in issues[:200]:
        t.add_row(i.where, i.check, i.message, i.hint)
    con.print(t)
    con.print(f"[red]Ошибок: {len(issues)}[/]")
    raise typer.Exit(1)


if __name__ == "__main__":
    app()
