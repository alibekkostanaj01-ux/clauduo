"""Фоновые задачи для панели: команда g2c запускается отдельным процессом, вывод пишется в лог.

Панель не держит долгую работу в себе: её можно закрыть и открыть снова — задача продолжит идти,
а лог и статус останутся в work/jobs/.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .paths import ROOT, WORK

JOBS_DIR = WORK / "jobs"
EXIT_MARK = "### EXIT "


def start(args: list[str], title: str) -> dict:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    jid = time.strftime("%Y%m%d-%H%M%S")
    log = JOBS_DIR / f"{jid}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen([sys.executable, "-m", "g2c.jobs", str(log), *args], cwd=ROOT, env=env,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    job = {"id": jid, "title": title, "args": args, "log": str(log), "started": time.strftime("%H:%M:%S")}
    reg = JOBS_DIR / "jobs.json"
    jobs = json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else []
    jobs.insert(0, job)
    reg.write_text(json.dumps(jobs[:50], ensure_ascii=False, indent=1), encoding="utf-8")
    return job


def list_jobs() -> list[dict]:
    reg = JOBS_DIR / "jobs.json"
    jobs = json.loads(reg.read_text(encoding="utf-8")) if reg.exists() else []
    for j in jobs:
        j["status"], j["tail"] = status(j)
    return jobs


def status(job: dict) -> tuple[str, str]:
    p = Path(job["log"])
    if not p.exists():
        return "запускается", ""
    text = p.read_text(encoding="utf-8", errors="replace")
    tail = "\n".join(text.splitlines()[-40:])
    if EXIT_MARK + "0" in text:
        return "готово", tail
    if EXIT_MARK in text:
        return "ошибка", tail
    return "идёт", tail


def _run(log: Path, args: list[str]) -> int:
    """Запуск команды g2c с записью вывода в лог и кода выхода в конце."""
    with log.open("w", encoding="utf-8") as f:
        f.write(f"$ g2c {' '.join(args)}\n")
        f.flush()
        proc = subprocess.Popen([sys.executable, "-m", "g2c.cli", *args], cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
                                env={**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "COLUMNS": "160"})
        for line in proc.stdout:
            f.write(line)
            f.flush()
        code = proc.wait()
        f.write(f"\n{EXIT_MARK}{code}\n")
    return code


if __name__ == "__main__":
    sys.exit(_run(Path(sys.argv[1]), sys.argv[2:]))
