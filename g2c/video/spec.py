"""Спецификация сцены и сценарий ролика — данные, из которых ролик пересобирается одной командой.

Спецификация: объекты, параметры, инварианты (пункты pc.* чек-листа физики), допущения, преувеличения (с числом),
кадры (шаблон, состояние, класс A–D, метка происхождения, плашка) и фразы закадра (у каждой — id утверждения узла).
Проверки до рендера (ТЗ «самые дорогие ошибки — дошедшие до рендера»): структура ролика, длина фраз и подписей,
опора на утверждения, числа только из источника, запрещённые формулировки (заблуждения), заявленные инварианты.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from ..mdbook import numbers
from ..paths import DOMAIN

Role = Literal["hook", "problem", "solution", "term", "data", "predict", "answer", "honesty", "action", "transfer"]
ROLE_ORDER = ["hook", "problem", "solution", "term", "data", "predict", "answer", "honesty", "action", "transfer"]
REQUIRED_ROLES = {"hook", "predict", "answer", "honesty", "transfer"}
QUESTION_ROLES = {"hook", "predict", "transfer"}  # могут не опираться на утверждение (это вопросы, не факты)


class Line(BaseModel):
    id: str
    role: Role
    text: str = Field(description="фраза закадра: до 15–18 слов, одна мысль")
    on_screen: str = Field(description="подпись на экране: ключевые слова, не больше 7–8 слов, без формул и греческих букв")
    claim_id: str | None = None
    is_analogy: bool = False
    shot: str = ""
    # заполняются при озвучке
    t_start: float | None = None
    t_end: float | None = None


class Shot(BaseModel):
    id: str
    template: str
    state: dict = Field(default_factory=dict)
    frame_class: Literal["A", "B", "C", "D"]
    label: Literal["реальные данные", "симуляция", "схема"]
    plaque: str | None = None


class Exaggeration(BaseModel):
    what: str
    factor: float | None = None
    text: str


class Source(BaseModel):
    claim_id: str
    page_or_dataset: str
    quote: str = ""


class SceneSpec(BaseModel):
    video_id: str
    chapter: str
    node_id: str
    node_title: str = ""
    format: Literal["short", "long"] = "short"
    template: str
    objects: list[str]
    params: dict = Field(default_factory=dict)
    invariants: list[str] = Field(description="пункты чек-листа физики pc.*, которые сцена обязана соблюдать")
    assumptions: list[str]
    exaggerations: list[Exaggeration]
    shots: list[Shot]
    lines: list[Line]
    prediction_question: str
    transfer_question: str
    data_it_bridge: str
    sources: list[Source]
    references: list[str] = Field(default_factory=list, description="фото-референсы оборудования")
    school: str = "русская школа ГИС"
    voice: str = "ru-RU-DmitryNeural"
    mentor_approved: bool = False
    mentor_note: str = ""
    planned_by: str = ""
    layer_file: str = ""  # путь к слою, если не work/<глава>/layer.json

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "SceneSpec":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


# --- проверки до рендера ---------------------------------------------------------------
def forbidden_patterns(domain: str) -> list[tuple[str, str]]:
    p = DOMAIN / domain / "forbidden_patterns.json"
    if not p.exists():
        return []
    return [(d["misc"], d["regex"]) for d in json.loads(p.read_text(encoding="utf-8"))]


def check_spec(spec: SceneSpec, node: dict, domain: str = "geology") -> list[str]:
    errs: list[str] = []
    claims = {c["id"]: c for c in node["claims"]}
    roles = [l.role for l in spec.lines]
    for r in REQUIRED_ROLES - set(roles):
        errs.append(f"нет обязательной части сценария: {r}")
    if roles and roles[0] != "hook":
        errs.append("ролик должен начинаться с крючка (вопрос или парадокс), а не с определения")
    hook = next((l for l in spec.lines if l.role == "hook"), None)
    if hook and not re.search(r"\?|почему|как\s+так|разве|неужели", hook.text, re.I):
        errs.append("крючок — не вопрос и не парадокс")
    if hook and len(hook.text.split()) > 9:
        errs.append("крючок длиннее ~3 секунд (больше 9 слов)")
    src_numbers = set()
    for c in claims.values():
        src_numbers |= numbers(c["text"]) | numbers(c["quote"])
    src_numbers |= {str(v) for v in spec.params.values() if isinstance(v, (int, float))}
    src_numbers |= numbers(" ".join(str(v) for v in spec.params.values() if isinstance(v, str)))
    for e in spec.exaggerations:  # числа преувеличений и истинного масштаба — из самой спецификации сцены
        src_numbers |= numbers(e.text)
    for s in spec.shots:
        src_numbers |= numbers(s.plaque or "")
    shot_ids = {s.id for s in spec.shots}
    for l in spec.lines:
        if len(l.text.split()) > 18:
            errs.append(f"{l.id}: фраза закадра длиннее 18 слов")
        w = len(l.on_screen.split())
        if w > 8:
            errs.append(f"{l.id}: подпись на экране длиннее 8 слов")
        if re.search(r"[α-ωΑ-Ω]|\\\(|\$|=", l.on_screen):
            errs.append(f"{l.id}: формулы и греческие буквы в подписи запрещены")
        if l.role not in QUESTION_ROLES and not l.is_analogy:
            if not l.claim_id:
                errs.append(f"{l.id}: у фразы нет утверждения-опоры (голос не говорит больше источника)")
            elif l.claim_id not in claims:
                errs.append(f"{l.id}: утверждение {l.claim_id} не из этого узла")
        bad = [n for n in numbers(l.text) if n not in src_numbers]
        if bad:
            errs.append(f"{l.id}: числа {', '.join(bad)} нет в утверждениях узла")
        if l.shot and l.shot not in shot_ids:
            errs.append(f"{l.id}: кадр {l.shot} не описан")
        for misc, rx in forbidden_patterns(domain):
            if re.search(rx, l.text, re.I) or re.search(rx, l.on_screen, re.I):
                errs.append(f"{l.id}: формулировка закрепляет заблуждение {misc}")
    if not spec.exaggerations:
        errs.append("не названо, что преувеличено (плашка «не в масштабе» обязана говорить, что и во сколько раз)")
    if not any(s.state.get("true_scale") for s in spec.shots):
        errs.append("нет кадра в истинном масштабе (ТЗ: хотя бы один кадр серии)")
    for s in spec.shots:
        if s.frame_class in ("A", "B") and s.state.get("generated"):
            errs.append(f"{s.id}: генерация в кадре класса {s.frame_class} запрещена")
    return errs
