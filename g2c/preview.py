"""Страница просмотра главы для команды (не курс!): страницы со сканами, единицы, утверждения и их проверка.

Работает без запросов к модели: берёт units.json, сканы и то, что уже есть —
layer.json, если слой готов, иначе черновые утверждения прохода 1 из кэша LLM.
Файл локальный (work/<глава>/preview.html): содержит текст книги, публиковать нельзя.
"""
from __future__ import annotations

import html
import json
import os
from pathlib import Path

from .ingest import Unit
from .layer import ClaimStore
from .llm import CACHE_DIR
from .paths import Chapter, parse_pages

KIND_RU = {"paragraph": "абзац", "figure": "рисунок", "formula": "формула", "table": "таблица",
           "worked_example": "пример", "task": "задача", "heading": "заголовок"}


def _cached_claims(ch: Chapter, units: list[Unit]) -> list[dict]:
    """Черновые утверждения прохода 1 из кэша: ответы, где есть claims с unit_id этой главы."""
    ids = {u.id for u in units}
    out = []
    for f in CACHE_DIR.glob("*.json"):
        try:
            data = json.loads(json.loads(f.read_text(encoding="utf-8"))["text"])
        except Exception:
            continue
        if isinstance(data, dict) and isinstance(data.get("claims"), list) and data["claims"] and \
                data["claims"][0].get("unit_id") in ids:
            out.extend(data["claims"])
    return out


def build_preview(ch: Chapter) -> Path:
    wd = ch.work_dir
    units = [Unit(**d) for d in json.loads((wd / "units.json").read_text(encoding="utf-8"))]
    store = ClaimStore(ch, units, parse_pages(ch.pages) + parse_pages(ch.prereq_pages))
    layer_path = wd / "layer.json"
    source = ""
    by_unit: dict[str, list[dict]] = {}
    node_of_claim: dict[str, str] = {}
    if layer_path.exists():
        source = "учебный слой (layer.json)"
        for n in json.loads(layer_path.read_text(encoding="utf-8"))["nodes"]:
            for c in n["claims"]:
                ref = store.add({k: c[k] for k in ("unit_id", "page_pdf", "text", "kind", "quote", "source_kind")})
                node_of_claim[ref] = n["title"]
    else:
        source = "черновик прохода 1 из кэша (слой ещё не собран)"
        for c in _cached_claims(ch, units):
            store.add(c)
    stats = {"ok": 0, "fixed_page": 0, "bad": 0}
    for ref in list(store.claims):
        c = store.claims[ref]
        page_before = c["page_pdf"]
        errs = store.check(ref)
        c["_errs"] = errs
        c["_ref"] = ref
        stats["bad" if errs else ("fixed_page" if c["page_pdf"] != page_before else "ok")] += 1
        by_unit.setdefault(c["unit_id"], []).append(c)

    pages = sorted({u.page_pdf for u in units})
    covered = {u.id for u in units if by_unit.get(u.id)}
    e = html.escape
    parts = [f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<title>Просмотр: {e(ch.id)}</title>
<style>
:root{{--bg:#0F1419;--panel:#1A2129;--text:#ECEFF4;--muted:#8A96A3;--ok:#27AE60;--bad:#E74C3C;--warn:#F1C40F;--acc:#7FB3D5}}
*{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 "Segoe UI",system-ui,sans-serif}}
header{{position:sticky;top:0;background:var(--panel);padding:12px 20px;border-bottom:1px solid #2a3440;z-index:5}}
header h1{{font-size:18px;margin:0 0 4px}} .muted{{color:var(--muted)}}
.stats span{{margin-right:16px}} nav{{margin-top:6px;display:flex;flex-wrap:wrap;gap:4px}}
nav a{{color:var(--acc);text-decoration:none;font-size:13px;padding:1px 6px;border:1px solid #2a3440;border-radius:4px}}
.page{{display:grid;grid-template-columns:minmax(280px,38%) 1fr;gap:20px;padding:20px;border-bottom:1px solid #2a3440}}
.scan img{{width:100%;border-radius:4px;background:#fff;position:sticky;top:110px}}
.unit{{background:var(--panel);border-radius:6px;padding:10px 12px;margin-bottom:10px;border-left:4px solid #3a4652}}
.unit.cov{{border-left-color:var(--ok)}} .unit.heading{{border-left-color:var(--acc)}}
.tag{{font-size:12px;color:var(--muted);margin-bottom:4px}} .tag b{{color:var(--acc)}}
.txt{{white-space:pre-wrap;word-break:break-word}} .desc{{white-space:pre-wrap;color:var(--muted);font-size:13px;margin-top:6px;border-top:1px dashed #3a4652;padding-top:6px}}
.claim{{margin:8px 0 0 10px;padding:6px 10px;border-radius:4px;background:#12181f;font-size:14px}}
.claim .q{{color:var(--muted);font-style:italic}} .claim.bad{{outline:1px solid var(--bad)}}
.badge{{font-size:11px;padding:1px 6px;border-radius:8px;margin-right:6px}} .b-ok{{background:#1e4d33}} .b-bad{{background:#5c1f1a}} .b-model{{background:#4a3b12}}
.ocr{{color:var(--warn)}}
@media (max-width:900px){{.page{{grid-template-columns:1fr}} .scan img{{position:static}}}}
</style></head><body>
<header><h1>{e(ch.title)}</h1>
<div class="muted">Источник утверждений: {e(source)}. Зелёная полоса — единица покрыта утверждениями.</div>
<div class="stats"><span>Единиц: {len(units)}</span><span>Покрыто: {len(covered)}</span>
<span>Утверждений: {len(store.claims)}</span><span style="color:var(--ok)">цитата верна: {stats['ok']}</span>
<span style="color:var(--warn)">верна, но не та страница: {stats['fixed_page']}</span><span style="color:var(--bad)">ошибка: {stats['bad']}</span></div>
<nav>{''.join(f'<a href="#p{p}">{p}</a>' for p in pages)}</nav></header>"""]
    for p in pages:
        scan = wd / "scans" / f"p{p:03d}.png"
        img = f'<img src="{html.escape(os.path.relpath(scan, wd).replace(os.sep, "/"))}" alt="скан стр. {p}" loading="lazy">' if scan.exists() else '<div class="muted">скана нет: g2c scans</div>'
        parts.append(f'<section class="page" id="p{p}"><div class="scan"><div class="tag">стр. PDF {p}</div>{img}</div><div>')
        for u in [x for x in units if x.page_pdf == p]:
            cls = "unit" + (" cov" if u.id in covered else "") + (" heading" if u.kind == "heading" else "")
            ocr = ' <span class="ocr">⚠ брак распознавания</span>' if u.ocr_suspect else ""
            parts.append(f'<div class="{cls}"><div class="tag"><b>{u.id}</b> · {KIND_RU.get(u.kind, u.kind)}'
                         f'{" · №" + u.task_number if u.task_number else ""}{ocr}</div>'
                         f'<div class="txt">{e(u.text) or "<i>(без подписи автора)</i>"}</div>')
            if u.model_description:
                parts.append(f'<details class="desc"><summary>описание рисунка, сделанное моделью при извлечении</summary>{e(u.model_description)}</details>')
            for c in by_unit.get(u.id, []):
                bad = bool(c["_errs"])
                badge = f'<span class="badge {"b-bad" if bad else "b-ok"}">{"✗ " + e("; ".join(c["_errs"])) if bad else "✓ цитата на стр. " + str(c["page_pdf"])}</span>'
                if c["source_kind"] == "figure_description_model":
                    badge += '<span class="badge b-model">из описания моделью</span>'
                node = f' <span class="muted">→ {e(node_of_claim[c["_ref"]])}</span>' if c["_ref"] in node_of_claim else ""
                parts.append(f'<div class="claim{" bad" if bad else ""}">{badge}<span class="muted">{c["kind"]}</span>{node}<br>'
                             f'{e(c["text"])}<div class="q">«{e(c["quote"])}»</div></div>')
            parts.append("</div>")
        parts.append("</div></section>")
    parts.append("</body></html>")
    out = wd / "preview.html"
    out.write_text("".join(parts), encoding="utf-8")
    return out
