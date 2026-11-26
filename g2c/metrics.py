"""Журнал ручных правок (manual_edits.csv) и метрики (metrics.csv) — в форматах шаблонов организаторов."""
from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime

from .llm import USAGE_FILE
from .paths import ROOT

EDITS_FILE = ROOT / "manual_edits.csv"
METRICS_FILE = ROOT / "metrics.csv"
EDITS_FIELDS = ["time", "chapter", "file", "what_changed", "person_minutes", "people", "run_no", "reason"]
METRICS_FIELDS = ["chapter", "stage", "nodes", "llm_tokens_in", "llm_tokens_out", "money_usd",
                  "person_minutes", "mentor_minutes", "video_seconds", "manual_share"]
STAGES = ["layer", "routing", "course", "video", "review"]


def add_edit(chapter: str, file: str, what: str, minutes: float, people: int, run_no: int, reason: str) -> dict:
    new = not EDITS_FILE.exists()
    row = {"time": datetime.now().isoformat(timespec="minutes"), "chapter": chapter, "file": file,
           "what_changed": what, "person_minutes": minutes, "people": people, "run_no": run_no, "reason": reason}
    with EDITS_FILE.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=EDITS_FIELDS)
        if new:
            w.writeheader()
        w.writerow(row)
    return row


def build_metrics(nodes_per_chapter: dict[str, int] | None = None,
                  mentor_minutes: dict[str, float] | None = None,
                  video_seconds: dict[str, float] | None = None) -> list[dict]:
    agg: dict[tuple[str, str], dict] = defaultdict(lambda: {"in": 0, "out": 0, "usd": 0.0, "usd_known": True})
    if USAGE_FILE.exists():
        for line in USAGE_FILE.read_text(encoding="utf-8").splitlines():
            r = json.loads(line)
            a = agg[(r["chapter"], r["stage"])]
            if r.get("cached"):
                continue
            a["in"] += r["tokens_in"]
            a["out"] += r["tokens_out"]
            if r.get("cost_usd") is None:
                a["usd_known"] = False
            else:
                a["usd"] += r["cost_usd"]
    edits: dict[tuple[str, str], float] = defaultdict(float)
    if EDITS_FILE.exists():
        with EDITS_FILE.open(encoding="utf-8") as f:
            for r in csv.DictReader(f):
                stage = next((s for s in STAGES if s in r["file"]), "course")
                edits[(r["chapter"], stage)] += float(r["person_minutes"] or 0)
    rows = []
    for (ch, stage) in sorted(set(agg) | set(edits)):
        a = agg.get((ch, stage), {"in": 0, "out": 0, "usd": 0.0, "usd_known": True})
        pm = edits.get((ch, stage), 0.0)
        rows.append({
            "chapter": ch, "stage": stage,
            "nodes": (nodes_per_chapter or {}).get(ch, ""),
            "llm_tokens_in": a["in"], "llm_tokens_out": a["out"],
            "money_usd": round(a["usd"], 4) if a["usd_known"] else "",
            "person_minutes": pm,
            "mentor_minutes": (mentor_minutes or {}).get(ch, 0) if stage == "video" else 0,
            "video_seconds": (video_seconds or {}).get(ch, 0) if stage == "video" else 0,
            # доля ручного труда считается по минутам: заполняется, когда есть замер минут работы конвейера
            "manual_share": "",
        })
    with METRICS_FILE.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=METRICS_FIELDS)
        w.writeheader()
        w.writerows(rows)
    return rows
