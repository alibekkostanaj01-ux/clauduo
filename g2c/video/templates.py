"""Шаблоны сцен: модель состояния + план кадров по частям сценария + рендер кадра + инварианты.

T1 well_log — «прибор в стволе + кривая ГК» (типы И, Пр, Н): глубина прибора — единственная переменная состояния;
    из неё вычисляются и положение прибора, и текущая точка кривой (кривая — реальные данные Volve).
    Порода серая, пока не проведена интерпретация: наблюдение (кривая) отделено от вывода (литология по порогу).
T0 schema_cards — универсальная схема для любого узла: ключевые мысли автора карточками со страницами
    (закрывает узлы, для которых нет специального шаблона; в отчёте покрытия — «нужен новый»).
Текст на кадре рисует код при монтаже (g2c.video.render.overlay), никакой генерации изображений.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from ..paths import DOMAIN
from .data import find_contrast_window, load_well
from .spec import Exaggeration, Shot

STYLE_FILE = DOMAIN / "geology" / "styleguide.json"


def style() -> dict:
    import json

    return json.loads(STYLE_FILE.read_text(encoding="utf-8"))


@dataclass
class Template:
    id: str
    name: str
    types: list[str]
    keywords: list[str]
    roles_hint: dict[str, str]          # что показывает кадр на каждой части сценария — для ИИ-сценариста
    objects: list[str]
    invariants: list[str]
    assumptions: list[str] = field(default_factory=list)

    def params(self, node: dict, fmt: str) -> dict:
        return {}

    def shots(self, params: dict, fmt: str) -> tuple[list[Shot], list[Exaggeration]]:
        raise NotImplementedError

    def state(self, shot: Shot, p: float, params: dict) -> dict:
        return {}

    def check_state(self, st: dict, params: dict) -> list[str]:
        return []


# ===================================== T1 =====================================
class WellLog(Template):
    BORE_M = 0.22  # диаметр ствола ~8,5 дюйма (кавернограмма Volve 7–10 дюймов)

    def params(self, node: dict, fmt: str) -> dict:
        w = load_well()
        d0, d1 = find_contrast_window(w, 60.0 if fmt == "short" else 120.0)
        m = (w.depth >= d0) & (w.depth <= d1)
        gr = w.curves["GR"][m]
        return {"well": w.name, "depth_from": d0, "depth_to": d1,
                "gr_max": float(np.ceil(np.nanpercentile(gr, 99) / 10) * 10),
                "gr_sand": float(round(np.nanpercentile(gr, 35), 1)),   # порог интерпретации: ниже — «чистая» порода
                "gr_shale": float(round(np.nanpercentile(gr, 70), 1)),  # выше — глинистая
                "dataset": f"Volve, скважина {w.name}, кривая ГК (LFP_GR), {d0:.0f}–{d1:.0f} м"}

    def shots(self, params: dict, fmt: str):
        window = params["depth_to"] - params["depth_from"]
        # на экране ствол занимает ~18% ширины колонки высотой window → во сколько раз ширина ствола преувеличена
        shown_ratio = 0.18 * (0.34 if fmt == "short" else 0.5)
        factor = round(shown_ratio / (self.BORE_M / window))
        ex = [Exaggeration(what="ширина ствола и размер прибора", factor=factor,
                           text=f"ствол шире в {factor} раз, чем в масштабе глубин")]
        plaque = f"не в масштабе: ствол шире ×{factor}"
        S = lambda i, ph, cls="A", label="реальные данные", pl=plaque, **kw: Shot(
            id=i, template=self.id, frame_class=cls, label=label, plaque=pl, state={"phase": ph, **kw})
        shots = [
            S("s_hook", "intro", cls="B", label="схема"),
            S("s_log1", "log", a=0.0, b=0.5),
            S("s_log2", "log", a=0.5, b=1.0),
            S("s_interp", "interp"),
            S("s_predict", "mask"),
            S("s_reveal", "reveal"),
            S("s_scale", "true_scale", cls="B", label="схема", pl=f"истинный масштаб: ствол {str(self.BORE_M).replace('.', ',')} м на {window:.0f} м", true_scale=True),
            S("s_final", "final"),
        ]
        return shots, ex

    role_shot = {"hook": "s_hook", "problem": "s_log1", "solution": "s_log2", "term": "s_interp", "data": "s_interp",
                 "predict": "s_predict", "answer": "s_reveal", "honesty": "s_scale", "action": "s_final", "transfer": "s_final"}

    def state(self, shot: Shot, p: float, params: dict) -> dict:
        d0, d1 = params["depth_from"], params["depth_to"]
        ph = shot.state["phase"]
        if ph == "intro":
            frac = 0.0
        elif ph == "log":
            frac = shot.state["a"] + (shot.state["b"] - shot.state["a"]) * min(1.0, p)
        else:
            frac = 1.0
        mask_from = d0 + (d1 - d0) * 0.62 if ph == "mask" else None
        if mask_from is not None:  # на предсказании прибор стоит на границе скрытого участка: ответ не подсказываем
            frac = 0.62
        tool = d0 + (d1 - d0) * frac
        w = load_well()
        return {"phase": ph, "tool_depth": tool, "curve_to": tool,
                "gr_at_tool": w.at("GR", tool),
                "interp": ph in ("interp", "reveal", "final", "mask"), "mask_from": mask_from,
                "interp_to": mask_from if mask_from is not None else d1,   # порода раскрашена только выше скрытого участка
                "true_scale": ph == "true_scale"}

    def check_state(self, st: dict, params: dict) -> list[str]:
        """Инварианты: прибор внутри окна, точка кривой = данные на глубине прибора, глубина растёт вниз (ось инвертирована при рендере)."""
        errs = []
        if not (params["depth_from"] - 1e-6 <= st["tool_depth"] <= params["depth_to"] + 1e-6):
            errs.append("прибор вне окна глубин")
        w = load_well()
        if abs(st["gr_at_tool"] - w.at("GR", st["tool_depth"])) > 1e-6:
            errs.append("точка кривой не совпадает с данными на глубине прибора")
        return errs


T1 = WellLog(
    id="T1_well_log", name="Прибор в стволе + кривая ГК (одна модель состояния)", types=["И", "Пр", "Н"],
    keywords=["гк", "гамма", "каротаж", "диаграмм", "кривая", "прибор", "глинист", "коллектор", "литолог", "радиоактив", "гис"],
    roles_hint={
        "hook": "прибор над стволом, кривой ещё нет; на экране вопрос-крючок",
        "problem": "прибор опускается, кривая ГК рисуется по мере движения (реальные данные Volve)",
        "solution": "прибор проходит окно до конца; видно чередование высоких и низких значений",
        "term": "порода раскрашивается по порогу ГК: ниже порога — песчаник, выше — глина; стрелка «сигнал → вывод»",
        "data": "то же: интервалы и пороги как признаки для модели",
        "predict": "нижняя часть кривой скрыта знаком вопроса — зритель предсказывает",
        "answer": "кривая открыта, интерпретация подтверждается или нет",
        "honesty": "кадр в истинном масштабе: ствол — тонкая линия на всю глубину; плашка про преувеличение; контрпример к правилу",
        "action": "итоговое состояние: специалист выделяет интервалы, отмечает спорные",
        "transfer": "итоговое состояние держится; вопрос на перенос",
    },
    objects=["ствол скважины с буровым раствором", "скважинный прибор ГК на кабеле", "порода (серая до интерпретации)",
             "кривая ГК (реальные данные Volve)", "пороги интерпретации", "интервалы: песчаник / глина"],
    invariants=["pc.tool_mechanism", "pc.signal_vs_property", "pc.curve_sync", "pc.depth_down", "pc.label_matches",
                "pc.exaggeration_named", "pc.objects_stable", "pc.applicability"],
    assumptions=["литология по одному методу ГК условна: высокий ГК обычно глина, но бывает радиоактивный песчаник",
                 "пороги интерпретации взяты по распределению ГК в окне (35-й и 70-й процентили), а не из книги",
                 "прибор показан схематично; конструкция не меняется между кадрами"],
)


# ===================================== T0 =====================================
class SchemaCards(Template):
    def shots(self, params: dict, fmt: str):
        ex = [Exaggeration(what="схема без масштаба", factor=None, text="схема: размеры условны")]
        mk = lambda i, ph, **kw: Shot(id=i, template=self.id, frame_class="B", label="схема",
                                      plaque="схема, не в масштабе", state={"phase": ph, **kw})
        return [mk("s_hook", "intro"), mk("s_cards", "cards"), mk("s_predict", "mask"), mk("s_reveal", "reveal"),
                mk("s_scale", "scale", true_scale=True), mk("s_final", "final")], ex

    role_shot = {"hook": "s_hook", "problem": "s_cards", "solution": "s_cards", "term": "s_cards", "data": "s_cards",
                 "predict": "s_predict", "answer": "s_reveal", "honesty": "s_scale", "action": "s_final", "transfer": "s_final"}

    def state(self, shot: Shot, p: float, params: dict) -> dict:
        return {"phase": shot.state["phase"], "p": p, "true_scale": shot.state.get("true_scale", False)}


T0 = SchemaCards(
    id="T0_schema_cards", name="Универсальная схема: мысли автора карточками", types=["М", "В", "И", "Ф", "П", "Пр", "Н", "Р"],
    keywords=[], roles_hint={r: "карточка с ключевой мыслью этой части и страницей книги" for r in
                             ["hook", "problem", "solution", "term", "data", "predict", "answer", "honesty", "action", "transfer"]},
    objects=["карточки с мыслями автора", "стрелки причин и следствий"], invariants=["pc.label_matches", "pc.causality"],
    assumptions=["схема без масштаба, показывает только логику узла"],
)

REGISTRY: dict[str, Template] = {t.id: t for t in (T1, T0)}


def pick_template(node: dict, fmt: str) -> tuple[Template, str]:
    """Шаблон для узла: по типу трудности и словам узла. Возвращает (шаблон, обоснование)."""
    typ = (node.get("difficulty") or {}).get("primary", "")
    text = " ".join(c["text"] for c in node["claims"]).lower() + " " + node["title"].lower()
    best, why = T0, "специального шаблона нет — универсальная схема (в отчёте покрытия: нужен новый)"
    for t in REGISTRY.values():
        if t is T0 or typ not in t.types:
            continue
        hits = sum(k in text for k in t.keywords)
        if hits >= 2:
            best, why = t, f"тип {typ}, совпадений по ключевым словам шаблона: {hits}"
            break
    return best, why
