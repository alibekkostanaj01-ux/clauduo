"""Модели ответов LLM на проходах учебного слоя (structured output).

Это промежуточные форматы: модели проще заполнить их, чем всю схему организаторов сразу.
Итоговый слой собирается кодом в формате node.schema.json (g2c/layer.py → assemble).
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Kind = Literal["observation", "inference", "assumption"]
SourceKind = Literal["text", "formula", "table", "task", "figure_caption", "figure_description_model"]
Difficulty = Literal["М", "В", "И", "Ф", "П", "Пр", "Н", "Р"]


# --- проход 1: утверждения, примеры, задачи ------------------------------------
class ClaimDraft(BaseModel):
    unit_id: str = Field(description="id единицы главы, из которой взято утверждение, напр. p095.05")
    page_pdf: int = Field(description="страница этой единицы")
    text: str = Field(description="утверждение своими словами, одна мысль, полностью, с условиями и числами автора")
    kind: Kind
    quote: str = Field(description="ДОСЛОВНЫЙ непрерывный фрагмент текста единицы, 8–300 символов, без изменений")
    source_kind: SourceKind


class WorkedExampleDraft(BaseModel):
    unit_ids: list[str]
    page_pdf: int
    condition: str
    steps: list[str]
    result: str = Field(description="итог с числами автора")
    figure: str = Field(default="", description="рисунок, на котором держится пример: «рис. N, стр. M» или пусто")


class BookTaskDraft(BaseModel):
    unit_id: str
    number: str
    page_pdf: int
    text: str = Field(description="формулировка задачи как в книге")
    figure: str = Field(default="", description="какой рисунок нужен для решения: «рис. N» или пусто")


class Pass1Out(BaseModel):
    claims: list[ClaimDraft]
    worked_examples: list[WorkedExampleDraft]
    book_tasks: list[BookTaskDraft]


class QuoteFix(BaseModel):
    ref: str = Field(description="номер утверждения из списка ошибок, напр. k17")
    action: Literal["fix", "drop"]
    quote: str = Field(default="", description="исправленная дословная цитата (для fix)")
    page_pdf: int = Field(default=0, description="страница, где эта цитата стоит (для fix)")
    text: str = Field(default="", description="исправленный текст утверждения, если в нём было число не из книги")


class QuoteFixOut(BaseModel):
    fixes: list[QuoteFix]


# --- проход 2: план узлов ---------------------------------------------------------
class NodePlanItem(BaseModel):
    slug: str = Field(description="латиница, цифры и _, 2–5 слов, напр. sp_anomaly_sign")
    title: str = Field(description="заголовок для новичка, 3–7 слов")
    book_title: str = Field(description="полное название раздела или мысли из книги")
    claim_refs: list[str] = Field(description="номера утверждений k…, вошедших в узел")
    why_here: str = Field(description="почему узел стоит на этом месте пути")
    difficulty_primary: Difficulty
    difficulty_secondary: str = Field(default="", description="второй тип трудности или пусто")
    threshold: bool = Field(description="пороговая идея: меняет взгляд на тему, без неё дальше не понять")
    terms_introduced: list[str] = Field(description="термины, которые узел объясняет впервые")
    terms_used: list[str] = Field(description="термины, которыми узел пользуется, но не объясняет")
    prerequisites: list[str] = Field(description="slug узлов этого плана, которые нужно пройти раньше")


class NodePlan(BaseModel):
    nodes: list[NodePlanItem]


# --- проход 3: поля узла ------------------------------------------------------------
class Inefficiency(BaseModel):
    inputs: str = ""
    manual_steps: str = ""
    judgment_points: str = ""
    time: str = ""
    typical_errors: str = ""


class DecisionOnData(BaseModel):
    features: list[str] = Field(default_factory=list)
    rule: str = ""
    conclusion: str = ""
    counterexample: str = ""


class NodeFields(BaseModel):
    slug: str
    topic: str = Field(description="тема узла одной фразой")
    main: str = Field(description="суть: 2–3 фразы, первая — главное")
    mechanism: str = Field(description="механизм: почему так, физика процесса или измерения — все мысли автора")
    practice: str = Field(description="практика: как это делает специалист, по шагам, с числами и порогами автора")
    nuances: str = Field(description="нюансы: условия применимости, оговорки автора, где правило ломается")
    deeper: list[str] = Field(description="ветки «глубже»: 0–3 абзаца для любопытных")
    misconception_ref: str = Field(default="", description="id заблуждения misc.* из списка или пусто")
    misconception_text: str = Field(default="", description="заблуждение, которое узел исправляет, или пусто")
    data_from_chapter: str = Field(description="как знание выглядит в данных — только то, что прямо следует из главы")
    data_breaks: str = Field(description="что в данных ломается, где правило даёт сбой — по главе")
    data_hypotheses: list[str] = Field(description="идеи для ИТ сверх главы — это гипотезы")
    it_analogy: str
    it_breaks_where: str = Field(description="где ИТ-аналогия перестаёт работать")
    now_practice: str = Field(default="", description="как это делается сейчас, если книга описывает устаревшую технику; иначе пусто")
    inefficiency: Inefficiency | None = Field(default=None, description="линза неэффективности для практических узлов")
    decision_on_data: DecisionOnData | None = Field(default=None, description="признаки → правило → вывод, если узел это позволяет")
    to_data: list[str] = Field(default_factory=list, description="датасеты data.*, к которым ведёт узел (напр. data.volve_las)")


class NodeFieldsOut(BaseModel):
    nodes: list[NodeFields]


# --- проход 4: предпосылки -----------------------------------------------------------
class PrereqNode(BaseModel):
    slug: str
    title: str
    term: str = Field(description="термин или понятие, которое узел объясняет")
    claims: list[ClaimDraft]
    main: str
    mechanism: str


class PrereqOut(BaseModel):
    nodes: list[PrereqNode]


# --- проход 5: покрытие ----------------------------------------------------------------
class CoverageDecision(BaseModel):
    unit_id: str
    node_slug: str = Field(default="", description="узел, в который входит единица, или пусто")
    reason: str = Field(default="", description="если узла нет — конкретная причина (не «непоказуемо»)")


class CoverageOut(BaseModel):
    decisions: list[CoverageDecision]
