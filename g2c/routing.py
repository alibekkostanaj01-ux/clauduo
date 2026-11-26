"""Маршрутизация форматов (часть 2 сдачи) — правилами, без модели: воспроизводимо, бесплатно, обосновано.

Для каждого узла:
  video_scores (0–2) по пяти критериям видеоёмкости из ТЗ — по признакам в тексте узла;
  format — по типу трудности («Подача доменов геологии»: ведущий формат типа) и сумме оценок;
  жёсткие правила ТЗ: таблицы и формулы — не в шортс; устаревшие приборы — не в ролик;
  хотя бы один «практикум на данных» на главу; template — шаблон сцены для роликов и интерактивов.
Выход — work/<глава>/routing.json по routing.schema.json.
"""
from __future__ import annotations

import json
import re

from .mdbook import norm
from .paths import Chapter
from .validate import check_schema

CUES = {
    "time": r"во\s+времени|со\s+временем|расформир|формирован|нестационар|неустановивш|рост|развит|эволюц|постепенн|повторн\w+\s+замер|временн",
    "invisible": r"зона\s+проникнов|пор[аыу]|поров|флюид|давлен|насыщ|фильтрат|излучен|радиоактив|электропровод|сопротивлен|нейтрон|проницаем|волн",
    "scale": r"\bкм\b|\bмкм\b|\bмм\b|масштаб|керн|шлиф|разрез[аеу]?\b|пласт\w*\s+и\s+скважин|региональн|месторожден",
    "counterintuitive": r"однако|в\s+отличие|несмотря|не\s+означает|не\s+всегда|ошибочн|ложн|парадокс|неоднозначн|наоборот|кажущ",
    "cause_effect": r"приводит|вследствие|поэтому|из-за|следовательно|вызыва|обусловлен|в\s+результате|зависит\s+от|тем\s+больше",
}

# ведущий формат по основному типу трудности (документ «Подача доменов геологии»)
LEAD = {"М": "шортс", "В": "длинное видео", "И": "шортс", "Ф": "интерактив", "П": "интерактив",
        "Пр": "практикум на данных", "Н": "интерактив", "Р": "карточки"}
TEMPLATES = {  # (тип, формат) → шаблон сцены (docs/ИНСТРУКЦИЯ.md, WP5)
    "И": "T1 прибор в стволе + планшет (одна модель состояния)", "Пр": "T2 решение на данных (Volve LAS)",
    "Н": "T2 решение на данных: диапазон и цена ошибки", "Ф": "T4 диффузия давления / T5 смена стационарных состояний",
    "М": "T3 пласт с флюидами и контактами", "В": "T6 рост тела во времени", "П": "T7 геометрия тела и ловушка",
    "Р": "фото → признаки → схема (карточки-квизы)",
}
OLD_DEVICE = r"прибор|аппаратур|станци|зонд|палетк"


def _text(n: dict) -> str:
    parts = [c["text"] for c in n["claims"]]
    parts += [(n.get("layers", {}).get(k) or {}).get("text", "") for k in ("main", "mechanism", "practice", "nuances")]
    return " ".join(parts).lower()


def scores(n: dict) -> dict[str, int]:
    t = _text(n)
    out = {}
    for k, rx in CUES.items():
        c = len(re.findall(rx, t))
        out[k] = 2 if c >= 4 else (1 if c >= 1 else 0)
    if n.get("misconception"):
        out["counterintuitive"] = max(out["counterintuitive"], 1)
    return out


def route_node(n: dict) -> dict:
    s = scores(n)
    total = sum(s.values())
    typ = (n.get("difficulty") or {}).get("primary", "И")
    fmt = LEAD.get(typ, "текст")
    reasons = [f"тип трудности {typ}: ведущий формат — {LEAD.get(typ, 'текст')}"]
    claims = n.get("claims", [])
    formula_heavy = sum(c.get("source_kind") in ("formula", "table") for c in claims) >= max(2, len(claims) // 3)
    old = bool(n.get("now_practice")) and re.search(OLD_DEVICE, _text(n))
    is_pre = ".pre_" in n["id"]
    has_data = bool(n.get("decision_on_data")) or any(l["type"] == "to_data" for l in n.get("links", []))

    if is_pre:
        fmt, reasons = "текст", ["предпосылка из предыдущих глав: короткий текст и карточка"]
    elif fmt in ("шортс", "длинное видео") and total < 3:
        fmt = "интерактив" if typ in ("Ф", "П", "Н") else "текст"
        reasons.append(f"видеоёмкость низкая ({total}/10) — видео не оправдано")
    elif fmt == "шортс" and total >= 7 and s["scale"] >= 1:
        fmt = "длинное видео"
        reasons.append("процесс на нескольких масштабах — разбор 16:9")
    if fmt in ("шортс", "длинное видео") and formula_heavy:
        fmt = "интерактив"
        reasons.append("таблицы и формулы — в интерактив, не в ролик (ТЗ)")
    if fmt in ("шортс", "длинное видео") and old:
        fmt = "текст"
        reasons.append("устаревшая техника книги — нюанс «как сейчас», не ролик (ТЗ)")
    if has_data and fmt in ("текст", "карточки", "интерактив") and typ in ("И", "Пр", "Н"):
        fmt = "практикум на данных"
        reasons.append("узел ведёт к данным: признаки → правило → вывод")
    secondary = None
    if fmt != "карточки" and (n.get("threshold") or n.get("misconception")):
        secondary = "карточки"
    if fmt == "интерактив" and total >= 6:
        secondary = "шортс"
    crit = ", ".join(f"{k}={v}" for k, v in s.items())
    return {"node_id": n["id"], "format": fmt, "secondary_format": secondary, "video_scores": s,
            "hook_short": False,
            "template": TEMPLATES.get(typ) if fmt in ("шортс", "длинное видео", "интерактив", "практикум на данных") else None,
            "why": "; ".join(reasons) + f". Критерии видеоёмкости: {crit} (сумма {total})."}


def build_routing(ch: Chapter) -> dict:
    layer = json.loads((ch.work_dir / "layer.json").read_text(encoding="utf-8"))
    nodes = sorted(layer["nodes"], key=lambda n: n["order"])
    routes = [route_node(n) for n in nodes]
    # хотя бы один шаг «решение на данных» на главу (ТЗ)
    if not any(r["format"] == "практикум на данных" for r in routes):
        cand = max((r for r, n in zip(routes, nodes) if ".pre_" not in n["id"]),
                   key=lambda r: (bool(next(n for n in nodes if n["id"] == r["node_id"]).get("decision_on_data")),
                                  r["video_scores"]["invisible"] + r["video_scores"]["cause_effect"]), default=None)
        if cand:
            cand["secondary_format"] = cand["format"] if cand["format"] != "практикум на данных" else cand["secondary_format"]
            cand["format"] = "практикум на данных"
            cand["why"] += " Назначен шагом «решение на данных» главы (ТЗ: хотя бы один на главу)."
    out = {"chapter": ch.id, "routes": routes}
    issues = check_schema(out, "routing")
    (ch.work_dir / "routing.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    counts: dict[str, int] = {}
    for r in routes:
        counts[r["format"]] = counts.get(r["format"], 0) + 1
    return {"nodes": len(routes), "formats": counts, "schema_errors": len(issues)}
