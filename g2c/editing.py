"""Ручная правка факта в учебном слое: правка → проверка → журнал с минутами → коммит.

ТЗ: правится слой (единственный источник правды), а не готовый курс; каждая правка — строка в manual_edits.csv
с человеко-минутами, и журнал должен совпадать с историей коммитов.
"""
from __future__ import annotations

import json
from pathlib import Path

from .metrics import add_edit
from .paths import DOMAIN, ROOT, Chapter, parse_pages
from .validate import Issue, check_claims, check_field_numbers, check_schema


def load_layer(ch: Chapter) -> dict:
    return json.loads((ch.work_dir / "layer.json").read_text(encoding="utf-8"))


def find_node(layer: dict, node_id: str) -> dict:
    return next(n for n in layer["nodes"] if n["id"] == node_id)


def validate_node(ch: Chapter, node: dict) -> list[Issue]:
    from .layer import ClaimStore  # книга + карточка-предпосылка

    store = ClaimStore(ch, [], parse_pages(ch.pages) + parse_pages(ch.prereq_pages))
    one = {"nodes": [node]}
    return check_claims(one, store.book, store.allowed) + check_field_numbers(one, store.book)


def save_edit(ch: Chapter, layer: dict, node_id: str, what: str, minutes: float, reason: str,
              people: int = 1, run_no: int = 1, commit: bool = True) -> dict:
    """Сохранить слой, записать правку в журнал, закоммитить слой и журнал. Вернуть итог."""
    issues = check_schema(layer, "node")
    if issues:
        return {"ok": False, "issues": [i.as_dict() for i in issues]}
    path = ch.work_dir / "layer.json"
    path.write_text(json.dumps(layer, ensure_ascii=False, indent=1), encoding="utf-8")
    rel = str(path.relative_to(ROOT)).replace("\\", "/")
    row = add_edit(ch.id, rel, f"{node_id}: {what}", minutes, people, run_no, reason)
    result = {"ok": True, "journal": row, "commit": None}
    if commit:
        result["commit"] = git_commit([rel, "manual_edits.csv"], f"Ручная правка {ch.id}: {node_id} — {what} ({minutes} чел.-мин)")
    return result


def git_commit(paths: list[str], message: str) -> str:
    """Коммит только указанных файлов. Возвращает короткий хэш или текст ошибки."""
    try:
        from git import Repo

        repo = Repo(ROOT)
        repo.index.add(paths)
        c = repo.index.commit(message)
        return c.hexsha[:8]
    except Exception as e:  # нет git user.name/email и т. п.
        return f"коммит не сделан: {str(e).splitlines()[0][:200]}"
