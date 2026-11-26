"""Задания курса (WP8): модель предлагает, код проверяет ключи.

ТЗ: не меньше 15 заданий на главу, не меньше 4 типов из 5; ответ каждого есть в тексте шага; у задания —
ссылка на утверждение узла; после проверки виден верный ответ и пояснение в одну фразу.
Код проверяет: ссылки на утверждения, согласованность ответа с вариантами, что ответ опирается на текст шага
(слова пропусков и верные варианты — в утверждениях, число расчёта — в цитатах или выводится из них),
заблуждения — из списка доменного пакета. Неверные задания отбрасываются, недостающие типы добираются.
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Literal

from pydantic import BaseModel, Field
from rapidfuzz import fuzz

from .layer import log, make_prompt
from .llm import generate
from .mdbook import norm, numbers
from .paths import DOMAIN, Chapter
from .validate import Issue, check_schema

TYPES = ["single", "multiple", "order", "gap", "calc"]


class OptionDraft(BaseModel):
    id: str
    text: str
    misconception: str = ""


class TaskDraft(BaseModel):
    step_id: str
    type: Literal["single", "multiple", "order", "gap", "calc"]
    prompt: str
    options: list[OptionDraft] = Field(default_factory=list)
    answer_option: str = Field(default="", description="single: id верного варианта")
    answer_options: list[str] = Field(default_factory=list, description="multiple: id верных вариантов")
    answer_order: list[str] = Field(default_factory=list, description="order: id в верном порядке")
    answer_gaps: list[str] = Field(default_factory=list, description="gap: пропущенные слова по порядку")
    answer_number: float | None = Field(default=None, description="calc: числовой ответ")
    tolerance: float | None = None
    unit: str = ""
    explanation: str
    claim_ids: list[str]
    kind: Literal["recall", "understanding", "transfer", "data"] = "understanding"


class TasksOut(BaseModel):
    tasks: list[TaskDraft]


def _node_text(n: dict) -> str:
    parts = [c["text"] + " " + c["quote"] for c in n["claims"]]
    for k in ("main", "mechanism", "practice", "nuances"):
        parts.append((n.get("layers", {}).get(k) or {}).get("text", ""))
    for e in n.get("worked_examples", []):
        parts += [e.get("condition", ""), " ".join(e.get("steps", [])), e.get("result", "")]
    return "\n".join(parts)


def allowed_types(n: dict) -> list[str]:
    txt = _node_text(n)
    t = ["single", "multiple", "gap"]
    if n.get("worked_examples") or len(re.findall(r"(?i)\b(сначала|затем|после|далее|по\s+шагам|этап)", txt)) >= 2 or \
            (n.get("layers", {}).get("practice") or {}).get("text"):
        t.append("order")
    if n.get("worked_examples") or any(c.get("source_kind") == "formula" for c in n["claims"]) or len(numbers(txt)) >= 3:
        t.append("calc")
    return t


def check_task(t: TaskDraft, node: dict, misc_ids: set[str]) -> list[str]:
    errs = []
    claim_ids = {c["id"] for c in node["claims"]}
    if not t.claim_ids or not set(t.claim_ids) <= claim_ids:
        errs.append("ссылки на утверждения не из этого шага")
    support = " ".join(c["text"] + " " + c["quote"] for c in node["claims"] if c["id"] in t.claim_ids) or _node_text(node)
    opt_ids = [o.id for o in t.options]
    if len(set(opt_ids)) != len(opt_ids):
        errs.append("повторяются id вариантов")
    for o in t.options:
        if o.misconception and o.misconception not in misc_ids:
            o.misconception = ""
    if t.type == "single":
        if len(t.options) < 3 or t.answer_option not in opt_ids:
            errs.append("один ответ: меньше 3 вариантов или ответ не из вариантов")
    elif t.type == "multiple":
        if len(t.options) < 4 or len(t.answer_options) < 2 or not set(t.answer_options) <= set(opt_ids) or len(t.answer_options) == len(opt_ids):
            errs.append("несколько ответов: нужно ≥4 вариантов и 2+ верных, но не все")
    elif t.type == "order":
        if len(t.options) < 3 or sorted(t.answer_order) != sorted(opt_ids):
            errs.append("порядок: нужно ≥3 элементов, ответ — перестановка всех")
    elif t.type == "gap":
        n_gaps = len(re.findall(r"\{\{\d+\}\}", t.prompt))
        if n_gaps == 0 or n_gaps != len(t.answer_gaps):
            errs.append("пропуск: число {{n}} не совпадает с ответами")
        src = norm(_node_text(node))
        for g in t.answer_gaps:  # слово пропуска должно быть в тексте шага (с точностью до окончания)
            stem = norm(g)[: max(4, len(norm(g)) - 2)]
            if stem and stem not in src:
                errs.append(f"пропуск «{g}» не найден в тексте шага")
    elif t.type == "calc":
        if t.answer_number is None:
            errs.append("расчёт без числового ответа")
    if t.type in ("single", "multiple"):  # верный вариант должен опираться на утверждения
        right = [o.text for o in t.options if o.id in ([t.answer_option] if t.type == "single" else t.answer_options)]
        for r in right:
            if fuzz.partial_token_set_ratio(r.lower(), support.lower()) < 60:
                errs.append(f"верный вариант «{r[:60]}» слабо опирается на утверждения")
    return errs


def build_tasks(ch: Chapter, workers: int = 4, per_call: int = 4) -> dict:
    wd = ch.work_dir
    layer = json.loads((wd / "layer.json").read_text(encoding="utf-8"))
    nodes = {n["id"]: n for n in layer["nodes"]}
    misc = json.loads((DOMAIN / ch.domain / "misconceptions.json").read_text(encoding="utf-8"))
    misc_ids = {m["id"] for m in misc}
    misc_txt = "\n".join(f"{m['id']}: {m['text']}" for m in misc)

    def step_block(n: dict) -> str:
        cl = "\n".join(f"  - {c['id']} [стр. {c['page_pdf']}]: {c['text']}" for c in n["claims"])
        ex = "".join(f"\n  пример: {e['condition']} → {e['result']}" for e in n.get("worked_examples", []))
        return f"### {n['id']} — {n['title']}\nallowed: {', '.join(allowed_types(n))}\nсуть: {(n['layers'].get('main') or {}).get('text', '')}\n{cl}{ex}"

    def run(group: list[dict], extra: str = ""):
        prompt = make_prompt("tasks", ch, misconceptions=misc_txt, steps="\n\n".join(step_block(n) for n in group)) + extra
        out, u = generate(prompt, role="fast", schema=TasksOut, stage="course", chapter=ch.id,
                          node="tasks." + ",".join(n["id"].split(".")[-1] for n in group))
        log(f"  {len(out.tasks)} заданий для {len(group)} шагов ({u.seconds} с)")
        return out

    order = sorted(nodes.values(), key=lambda n: n["order"])
    groups = [order[i:i + per_call] for i in range(0, len(order), per_call)]
    log(f"Задания: {len(order)} шагов, {len(groups)} запросов")
    drafts: list[TaskDraft] = []
    with ThreadPoolExecutor(workers) as ex:
        for out in ex.map(run, groups):
            drafts += out.tasks

    kept, issues = [], []

    def accept(ds):
        for t in ds:
            n = nodes.get(t.step_id)
            errs = ["шаг не найден"] if n is None else check_task(t, n, misc_ids)
            if errs:
                issues.append(Issue(t.step_id, "tasks", f"{t.type}: отброшено — {'; '.join(errs)}", t.prompt[:100]))
            else:
                kept.append(t)

    accept(drafts)
    # добрать недостающие типы (ТЗ: ≥4 из 5) и общее число (≥15)
    for typ in TYPES:
        if any(t.type == typ for t in kept):
            continue
        cands = [n for n in order if typ in allowed_types(n)][:3]
        if cands:
            log(f"  добираю тип {typ}")
            accept(run(cands, extra=f"\n\nСоставь для этих шагов задания ТОЛЬКО типа {typ}.").tasks)
    if len(kept) < 15:
        covered = {t.step_id for t in kept}
        rest = [n for n in order if n["id"] not in covered] or order
        accept(run(rest[:6], extra="\n\nСоставь по 2 задания на шаг.").tasks)

    tasks = []
    for i, t in enumerate(sorted(kept, key=lambda t: (nodes[t.step_id]["order"], TYPES.index(t.type))), 1):
        d = {"id": f"T{i:03d}", "type": t.type, "step_id": t.step_id, "claim_ids": t.claim_ids, "prompt": t.prompt,
             "explanation": t.explanation, "kind": t.kind}
        if t.type in ("single", "multiple", "order"):
            d["options"] = [{"id": o.id, "text": o.text, "misconception": o.misconception or None} for o in t.options]
        d["answer"] = {"single": t.answer_option, "multiple": t.answer_options, "order": t.answer_order,
                       "gap": t.answer_gaps, "calc": t.answer_number}[t.type]
        if t.type == "calc":
            d["tolerance"] = t.tolerance if t.tolerance is not None else round(abs(t.answer_number or 0) * 0.02, 6)
            d["unit"] = t.unit or None
            src = numbers(_node_text(nodes[t.step_id]))
            if str(t.answer_number).rstrip("0").rstrip(".") not in src:
                d["review"] = "ответ расчёта не встречается в тексте шага — проверить вручную"
        tasks.append(d)
    out = {"chapter": ch.id, "tasks": tasks}
    issues += check_schema(out, "task")
    (wd / "tasks.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    (wd / "tasks_issues.json").write_text(json.dumps([i.as_dict() for i in issues], ensure_ascii=False, indent=1), encoding="utf-8")
    types = sorted({t["type"] for t in tasks})
    summary = {"tasks": len(tasks), "types": types, "dropped": sum(1 for i in issues if i.check == "tasks"),
               "need_review": sum(1 for t in tasks if t.get("review")),
               "meets_tz": len(tasks) >= 15 and len(types) >= 4}
    log(f"Готово: {summary}")
    return summary
