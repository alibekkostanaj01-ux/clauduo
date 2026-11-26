"""Проверка ключей заданий и сборки курса — без обращения к модели."""
import json
import re

from g2c.course import build_course, map_svg, rich, table_html
from conftest import HAS_SCHEMAS
import pytest as _pt
if not HAS_SCHEMAS:
    _pt.skip("нет schemas/ организаторов (не публикуются)", allow_module_level=True)
from g2c.paths import SCHEMAS, get_chapter
from g2c.tasks import OptionDraft, TaskDraft, check_task

LAYER = json.loads((SCHEMAS / "example_layer.json").read_text(encoding="utf-8"))
NODE = LAYER["nodes"][0]
CID = NODE["claims"][0]["id"]
MISC = {"misc.high_res_oil", "misc.log_is_rock"}


def opt(i, t, m=""):
    return OptionDraft(id=i, text=t, misconception=m)


def test_single_ok_and_bad_misconception_cleared():
    t = TaskDraft(step_id=NODE["id"], type="single", prompt="Как выделяют коллектор в смешанном разрезе?",
                  options=[opt("a", "по одной кривой", "misc.unknown"), opt("b", "количественной интерпретацией геофизических данных"), opt("c", "по керну")],
                  answer_option="b", explanation="…", claim_ids=[CID])
    assert check_task(t, NODE, MISC) == []
    assert t.options[0].misconception == ""


def test_single_answer_not_supported_by_claims():
    t = TaskDraft(step_id=NODE["id"], type="single", prompt="?",
                  options=[opt("a", "x"), opt("b", "сейсморазведка трёхмерная высокого разрешения"), opt("c", "z")],
                  answer_option="b", explanation="…", claim_ids=[CID])
    assert any("слабо опирается" in e for e in check_task(t, NODE, MISC))


def test_gap_word_must_be_in_step():
    good = TaskDraft(step_id=NODE["id"], type="gap", prompt="Коллектор выделяют {{1}} интерпретацией.",
                     answer_gaps=["количественной"], explanation="…", claim_ids=[CID])
    bad = TaskDraft(step_id=NODE["id"], type="gap", prompt="Коллектор выделяют {{1}} методом.",
                    answer_gaps=["сейсмическим"], explanation="…", claim_ids=[CID])
    assert check_task(good, NODE, MISC) == []
    assert check_task(bad, NODE, MISC)


def test_order_and_foreign_claim():
    t = TaskDraft(step_id=NODE["id"], type="order", prompt="?", options=[opt("a", "1"), opt("b", "2"), opt("c", "3")],
                  answer_order=["a", "b"], explanation="…", claim_ids=["other#c1"])
    errs = check_task(t, NODE, MISC)
    assert any("порядок" in e for e in errs) and any("утверждения" in e for e in errs)


def test_rich_math_and_escape():
    h = rich("<b>x</b> и \\(\\rho_п\\) **важно**")
    assert "&lt;b&gt;" in h and "<math" in h and "<strong>важно</strong>" in h


def test_plain_text_math_to_symbols():
    h = rich("ρ_п → ∞, δ_p = 0; K*пр = 1 мД; δ_{сп} -> 0; 10^3 и 10^-3; a * b; x >= 2")
    assert "ρ<sub>п</sub>" in h and "δ<sub>p</sub>" in h and "K<sup>*</sup><sub>пр</sub>" in h
    assert "δ<sub>сп</sub> →" in h and "10<sup>3</sup>" in h and "10<sup>-3</sup>" in h
    assert "a · b" in h and "≥" in h
    assert "k<sub>n</sub><sup>*</sup>" in rich("граничное k_n^* растёт") and "k<sub>n</sub><sup>*</sup>" in rich("k_{n}^{*}")
    # идентификаторы и обычный текст не трогаем
    keep = rich("кривая LFP_GR из data.volve_las и snake_case_name")
    assert "<sub>" not in keep
    # голый LaTeX без обрамления → MathML
    assert "<math" in rich(r"при \rho_{п} > \rho_{в}")


def test_matrices_systems_and_complex_formulas():
    cases = [
        r"\[ A = \begin{pmatrix} a & b \\ c & d \end{pmatrix} \tag{2.1} \]",
        r"\[ \begin{bmatrix} 1 & 0 \\ 0 & 1 \end{bmatrix} \]",
        r"\[ f(x) = \begin{cases} 1, & x > 0 \\ 0, & x \le 0 \end{cases} \]",
        r"\[ \begin{aligned} a &= b + c \\ d &= \dfrac{e}{f} \end{aligned} \]",
        r"\( \frac{\partial p}{\partial t} = \varkappa \left( \frac{\partial^2 p}{\partial r^2} + \frac{1}{r}\frac{\partial p}{\partial r} \right) \)",
        r"\( \sqrt{k_{п}} \int_0^{\infty} e^{-x^2}\,dx \sum_{i=1}^{n} \text{дебит}_i \)",
    ]
    for c in cases:
        h = rich(c)
        assert "<math" in h and "<code" not in h, c
        assert "\\" not in re.sub(r"<[^>]+>", "", h), c   # не осталось сырых команд
    assert "mtable" in rich(cases[0]) and "(2.1)" in rich(cases[0])   # матрица и номер формулы


def test_plain_text_fractions_and_roots():
    h = rich("Коэффициент (ρ_п - ρ_в)/(ρ_п + ρ_в) и sqrt(k_п / φ), а также (a+b)/2")
    assert h.count("<mfrac") == 2 and "<msqrt" in h
    units = rich("водоотдача 4—8 см³/ч, интервальное время 600 мкс/м, сопротивление 2,2 Ом·м, А/Б")
    assert "<math" not in units   # единицы и обычные косые черты не трогаем


def test_unknown_command_is_reported_not_lost(tmp_path, monkeypatch):
    """Новая книга с незнакомой командой: формула не теряется, а попадает в отчёт."""
    from g2c import course as C

    h = rich(r"\( a \mysterymacro{b} \)")
    assert "mysterymacro" in re.sub(r"<[^>]+>", " ", h)  # исходник виден
    ch = get_chapter("latyshova_gl5_par17-20")
    wd = tmp_path / "w"
    wd.mkdir()
    (wd / "units.json").write_text(json.dumps([{"id": "p001.01", "page_pdf": 1, "text": r"\( a \mysterymacro{b} \) и \( \frac{1}{2} \)"}]), encoding="utf-8")
    monkeypatch.setattr(type(ch), "work_dir", property(lambda self: wd))
    rep = C.formula_report(ch)
    assert rep["formulas"] == 2 and (rep["not_converted"] + rep["with_leftover_commands"]) == 1


def test_table_html_markdown():
    h = table_html("Таблица 1\n| a | b |\n|---|---|\n| 1 | 2 |")
    assert "<th>a</th>" in h and "<td>2</td>" in h and "<caption>" in h


def test_build_example_course(tmp_path):
    ch = get_chapter("latyshova_gl5_par17-20")
    idx = build_course(ch, SCHEMAS / "example_layer.json", SCHEMAS.parent / "tests" / "fixtures" / "example_tasks.json", tmp_path / "c")
    data = (tmp_path / "c" / "data.js").read_text(encoding="utf-8")
    assert idx.exists() and data.startswith("window.COURSE = ")
    course = json.loads(data[len("window.COURSE = "):].rstrip().rstrip(";"))
    assert len(course["steps"]) == 1 and len(course["tasks"]) == 5
    keys = {b["key"] for b in course["steps"][0]["blocks"]}
    assert {"main", "claims", "data", "analogy"} <= keys
    assert "<svg" in map_svg(course["steps"])
