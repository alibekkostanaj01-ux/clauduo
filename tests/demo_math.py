"""Демо-страница формул (out/_mathdemo/index.html): матрица, система, текстовая запись модели и реальные формулы глав."""
import json
import shutil
import sys
from pathlib import Path

from g2c.course import rich

sys.stdout.reconfigure(encoding="utf-8")
d = Path("out/_mathdemo")
d.mkdir(parents=True, exist_ok=True)
shutil.copy("course_template/course.css", d / "course.css")
items = [
    r"Матрица: \[ A = \begin{pmatrix} a & b \\ c & d \end{pmatrix} \tag{2.1} \]",
    r"Система: \[ f(x) = \begin{cases} 1, & x > 0 \\ 0, & x \le 0 \end{cases} \]",
    r"Выравнивание: \[ \begin{aligned} a &= b + c \\ d &= \dfrac{e}{f} \end{aligned} \]",
    "Простым текстом от модели: ρ_п → ∞, δ_p = 0, K*пр = 1 мД, 10^-3, a * b, x >= 2",
]
for ch in ("basniev_gl23_gl24par1", "barenblatt_par4", "latyshova_gl5_par17-20"):
    units = json.loads(Path(f"work/{ch}/units.json").read_text(encoding="utf-8"))
    items += [u["text"] for u in units if u["kind"] == "formula"][:4]
body = "".join(f"<section class='blk'>{rich(t)}</section>" for t in items)
(d / "index.html").write_text(
    f"<!doctype html><html lang='ru'><head><meta charset='utf-8'><title>Формулы</title>"
    f"<link rel='stylesheet' href='course.css'></head><body><main style='padding:20px'>{body}</main></body></html>",
    encoding="utf-8")
print("ok", len(items))
