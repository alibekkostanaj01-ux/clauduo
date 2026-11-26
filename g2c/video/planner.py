"""Режиссёр: узел слоя → спецификация сцены + сценарий.

Шаблон выбирается кодом (тип трудности + слова узла), параметры сцены — из данных и утверждений,
сценарий пишет ИИ по API (роль writer) строго по утверждениям узла; спецификация проверяется (check_spec),
при ошибках — одна попытка исправления. Без доступа к модели сценарий собирается из полей узла (запасной путь).
Рядом кладётся лист для ментора: спецификация + таблица источников — проверка до рендера.
"""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, Field

from ..layer import log, make_prompt
from ..llm import LLMError, generate
from ..paths import DOMAIN, ROOT, get_chapter
from ..settings import model_for
from .spec import Line, SceneSpec, Source, check_spec
from .templates import REGISTRY, pick_template

Role = Literal["hook", "problem", "solution", "term", "data", "predict", "answer", "honesty", "action", "transfer"]


class LineDraft(BaseModel):
    role: Role
    text: str
    on_screen: str
    claim_id: str = ""
    is_analogy: bool = False


class ScriptDraft(BaseModel):
    lines: list[LineDraft]
    prediction_question: str
    transfer_question: str
    data_it_bridge: str = Field(description="где здесь данные и ИТ: 1–2 фразы")
    counterexample: str = ""


def load_node(chapter: str, node_id: str, layer_file: str = "") -> dict:
    lp = ROOT / layer_file if layer_file else ROOT / "work" / chapter / "layer.json"
    layer = json.loads(lp.read_text(encoding="utf-8"))
    return next(n for n in layer["nodes"] if n["id"] == node_id)


def _fallback_script(node: dict, ex_text: str) -> ScriptDraft:
    """Запасной сценарий без модели: из полей узла и первых утверждений."""
    cl = node["claims"]
    short = lambda s, n=16: " ".join(s.split()[:n]).rstrip(",;:") + ("." if not s.rstrip().endswith((".", "?", "!")) else "")
    keyw = lambda s: " ".join(s.split()[:5])
    m = (node.get("misconception") or {}).get("text") or ""
    hook = f"Правда ли, что {m[0].lower() + m[1:].rstrip('.')}?" if m else f"Почему {node['title'][0].lower() + node['title'][1:]}?"
    hook = " ".join(hook.split()[:9]).rstrip("?") + "?"
    pick = lambda i: cl[min(i, len(cl) - 1)]
    lines = [LineDraft(role="hook", text=hook, on_screen=keyw(hook))]
    for role, i in (("problem", 0), ("solution", 1), ("term", 2), ("data", 3)):
        c = pick(i)
        lines.append(LineDraft(role=role, text=short(c["text"]), on_screen=keyw(c["text"]), claim_id=c["id"]))
    lines.append(LineDraft(role="predict", text="Как думаете, что покажет следующий участок?", on_screen="Ваш прогноз?"))
    c = pick(4)
    lines.append(LineDraft(role="answer", text=short(c["text"]), on_screen=keyw(c["text"]), claim_id=c["id"]))
    c = pick(5)
    lines.append(LineDraft(role="honesty", text=short(f"Честно о картинке: {ex_text}.", 18), on_screen="Не в масштабе", claim_id=c["id"]))
    lines.append(LineDraft(role="transfer", text="Где в ваших данных встретится та же ловушка?", on_screen="Где это в ваших данных?"))
    dm = ((node.get("data_meaning") or {}).get("from_chapter") or {}).get("text", "")
    return ScriptDraft(lines=lines, prediction_question=lines[5].text, transfer_question=lines[-1].text,
                       data_it_bridge=dm or "Кривая — признак для модели; вывод о породе — интерпретация с допущениями.")


def plan_video(chapter: str, node_id: str, fmt: str = "short", use_llm: bool = True, template_id: str | None = None,
               layer_file: str = "") -> tuple[SceneSpec, list[str]]:
    ch = get_chapter(chapter)
    node = load_node(chapter, node_id, layer_file)
    tpl, why = (REGISTRY[template_id], "задан вручную") if template_id else pick_template(node, fmt)
    params = tpl.params(node, fmt)
    if tpl.id == "T0_schema_cards":
        params["cards"] = [{"text": " ".join(c["text"].split()[:22]), "page": c["page_pdf"]} for c in node["claims"][:4]]
    shots, ex = tpl.shots(params, fmt)
    ex_text = "; ".join(e.text for e in ex)
    misc = json.loads((DOMAIN / ch.domain / "misconceptions.json").read_text(encoding="utf-8"))
    video_id = f"{chapter}.{node_id.split('.')[-1]}.{'short' if fmt == 'short' else 'long'}"[:90]
    planned_by = "запасной сценарий из полей узла (без модели)"
    draft = None
    errors: list[str] = []
    if use_llm:
        prompt = make_prompt(
            "video_script", ch, node_title=node["title"], template_name=tpl.name,
            format_ru="шортс 9:16, один механизм" if fmt == "short" else "разбор 16:9, решение на данных",
            length="40–75 секунд" if fmt == "short" else "1,5–3 минуты",
            roles_hint="\n".join(f"  {r}: {h}" for r, h in tpl.roles_hint.items()), exaggeration=ex_text,
            misconceptions="; ".join(m["text"] for m in misc),
            claims="\n".join(f"{c['id']} [стр. {c['page_pdf']}] {c['text']}" for c in node["claims"]),
            main=(node["layers"].get("main") or {}).get("text", ""),
            misconception=(node.get("misconception") or {}).get("text", "—"),
            data_meaning=((node.get("data_meaning") or {}).get("from_chapter") or {}).get("text", ""))
        for attempt in range(2):
            try:
                draft, u = generate(prompt, role="writer", schema=ScriptDraft, stage="video", chapter=chapter, node=node_id,
                                    use_cache=attempt == 0)
                planned_by = f"сценарий: {u.model}; шаблон: {why}"
            except LLMError as e:
                log(f"  модель недоступна ({str(e)[:120]}) — запасной сценарий")
                break
            spec = _assemble(draft, node, tpl, params, shots, ex, video_id, chapter, fmt, planned_by)
            errors = check_spec(spec, node, ch.domain)
            if not errors:
                break
            log(f"  сценарий не прошёл проверку ({len(errors)}), попытка исправления")
            prompt += "\n\nПРЕДЫДУЩИЙ ВАРИАНТ НЕ ПРОШЁЛ ПРОВЕРКУ, исправь:\n" + "\n".join(f"- {e}" for e in errors)
    if draft is None or errors:
        if draft is None:
            draft = _fallback_script(node, ex_text)
            planned_by = f"запасной сценарий из полей узла (без модели); шаблон: {why}"
    spec = _assemble(draft, node, tpl, params, shots, ex, video_id, chapter, fmt, planned_by)
    spec.layer_file = layer_file
    errors = check_spec(spec, node, ch.domain)
    out = ROOT / "work" / chapter / "video" / video_id / "spec.json"
    spec.save(out)
    _mentor_sheet(spec, node, errors, out.parent / "mentor_sheet.md")
    return spec, errors


def _assemble(draft: ScriptDraft, node, tpl, params, shots, ex, video_id, chapter, fmt, planned_by) -> SceneSpec:
    claims = {c["id"]: c for c in node["claims"]}
    order = {r: i for i, r in enumerate(["hook", "problem", "solution", "term", "data", "predict", "answer", "honesty", "action", "transfer"])}
    dl = sorted(draft.lines, key=lambda l: order[l.role])  # порядок частей — по структуре ТЗ
    lines = [Line(id=f"L{i:02d}", role=l.role, text=l.text.strip(), on_screen=l.on_screen.strip(),
                  claim_id=l.claim_id or None, is_analogy=l.is_analogy, shot=tpl.role_shot[l.role]) for i, l in enumerate(dl, 1)]
    sources = []
    for l in lines:
        if l.claim_id in claims:
            c = claims[l.claim_id]
            sources.append(Source(claim_id=l.claim_id, page_or_dataset=f"стр. {c['page_pdf']}", quote=c["quote"]))
    if "dataset" in params:
        sources.append(Source(claim_id="data", page_or_dataset=params["dataset"], quote=""))
    used = {l.shot for l in lines}
    return SceneSpec(
        video_id=video_id, chapter=chapter, node_id=node["id"], node_title=node["title"], format=fmt, template=tpl.id,
        objects=tpl.objects, params=params, invariants=tpl.invariants,
        assumptions=tpl.assumptions + ([draft.counterexample] if draft.counterexample else []),
        exaggerations=ex, shots=[s for s in shots if s.id in used], lines=lines,
        prediction_question=draft.prediction_question, transfer_question=draft.transfer_question,
        data_it_bridge=draft.data_it_bridge, sources=sources,
        references=["фото-референсы организаторов: каротажный подъёмник, скважинные приборы"],
        planned_by=planned_by)


def _mentor_sheet(spec: SceneSpec, node: dict, errors: list[str], path) -> None:
    rows = "\n".join(f"| {l.t_start or ''} | {l.role} | {l.text} | {l.on_screen} | {l.claim_id or '—'} |" for l in spec.lines)
    src = "\n".join(f"| {s.claim_id} | {s.page_or_dataset} | {s.quote[:120]} |" for s in spec.sources)
    path.write_text(f"""# Лист для ментора: {spec.video_id}

Узел: **{node['title']}** (`{spec.node_id}`), шаблон `{spec.template}`, формат {spec.format}.
Проверка до рендера по чек-листу физики. После проверки — `mentor_approved: true` в spec.json.

## Сценарий
| t | часть | закадр | на экране | утверждение |
|---|---|---|---|---|
{rows}

## Источники
| утверждение | страница / датасет | цитата |
|---|---|---|
{src}

## Сцена
- Объекты: {', '.join(spec.objects)}
- Инварианты: {', '.join(spec.invariants)}
- Допущения: {'; '.join(spec.assumptions)}
- Преувеличения: {'; '.join(e.text for e in spec.exaggerations)}
- Вопрос-предсказание: {spec.prediction_question}
- Вопрос на перенос: {spec.transfer_question}
- Мост к данным и ИТ: {spec.data_it_bridge}

## Автоматическая проверка
{chr(10).join('- ' + e for e in errors) if errors else 'Ошибок нет.'}
""", encoding="utf-8")


def best_video_node(chapter: str, fmt: str = "short") -> str | None:
    """Узел для ролика: из маршрутизации (шортс/длинное видео), с наибольшей видеоёмкостью и подходящим шаблоном."""
    rpath = ROOT / "work" / chapter / "routing.json"
    if not rpath.exists():
        return None
    routes = json.loads(rpath.read_text(encoding="utf-8"))["routes"]
    want = {"short": ("шортс",), "long": ("длинное видео", "практикум на данных")}[fmt]
    cand = [r for r in routes if r["format"] in want or r.get("secondary_format") in want]
    layer = {n["id"]: n for n in json.loads((ROOT / "work" / chapter / "layer.json").read_text(encoding="utf-8"))["nodes"]}

    def score(r):
        tpl, _ = pick_template(layer[r["node_id"]], fmt)
        return (tpl.id != "T0_schema_cards", sum(r["video_scores"].values()))
    cand.sort(key=score, reverse=True)
    return cand[0]["node_id"] if cand else None
