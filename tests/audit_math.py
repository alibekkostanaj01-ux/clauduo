"""Аудит формул всех глав: переводятся ли в MathML и не остаётся ли непереведённых команд.
Запуск: .venv\\Scripts\\python.exe tests/audit_math.py"""
import collections
import json
import re
import sys

from g2c.course import MATH_RE, mathml
from g2c.paths import load_chapters

sys.stdout.reconfigure(encoding="utf-8")
total, leftovers, errors = 0, collections.Counter(), collections.Counter()
examples = {}
for ch in load_chapters().values():
    for f in ("units.json", "prereq_units.json"):
        p = ch.work_dir / f
        if not p.exists():
            continue
        for u in json.loads(p.read_text(encoding="utf-8")):
            for m in MATH_RE.finditer(u["text"]):
                s = m.group(0)
                tex = s[2:-2] if s.startswith(("\\[", "\\(", "$$")) else s[1:-1]
                total += 1
                out = mathml(tex, False)
                if out.startswith("<code"):
                    errors["не перевелась"] += 1
                    examples.setdefault("не перевелась", tex[:100])
                for cmd in re.findall(r"\\[a-zA-Z]+", re.sub(r"<[^>]+>", " ", out)):
                    leftovers[cmd] += 1
                    examples.setdefault(cmd, tex[:100])
print(f"формул: {total}; не перевелось: {sum(errors.values())}")
print("непереведённые команды внутри MathML:", sum(leftovers.values()))
for k, v in leftovers.most_common(20):
    print(f"  {v:4d} {k}  | {examples[k]}")
