"""Инварианты шаблона T1 и проверки спецификации — без модели и без рендера ролика."""
import json

import pytest

from conftest import HAS_SCHEMAS
import pytest as _pt
if not HAS_SCHEMAS:
    _pt.skip("нет schemas/ организаторов (не публикуются)", allow_module_level=True)
from g2c.paths import ROOT, SCHEMAS
from g2c.video.data import RAW
from g2c.video.spec import Line, check_spec

needs_data = pytest.mark.skipif(not RAW.exists(), reason="нет архива Volve (data/raw не в git)")
NODE = json.loads((SCHEMAS / "example_layer.json").read_text(encoding="utf-8"))["nodes"][0]


@needs_data
def test_t1_state_model_invariants():
    from g2c.video.templates import T1

    params = T1.params(NODE, "short")
    shots, ex = T1.shots(params, "short")
    assert params["depth_from"] < params["depth_to"] and ex[0].factor > 1
    for s in shots:
        for p in (0.0, 0.5, 1.0):
            st = T1.state(s, p, params)
            assert T1.check_state(st, params) == []          # точка кривой = данные на глубине прибора
            if st["mask_from"] is not None:                   # на предсказании ответ не подсказан
                assert st["tool_depth"] <= st["mask_from"] and st["interp_to"] == st["mask_from"]
    assert any(s.state.get("true_scale") for s in shots)       # есть кадр в истинном масштабе


@needs_data
def test_spec_checks_catch_errors():
    from g2c.video.planner import ScriptDraft, LineDraft, _assemble
    from g2c.video.templates import T1

    params = T1.params(NODE, "short")
    shots, ex = T1.shots(params, "short")
    cid = NODE["claims"][0]["id"]
    good = ScriptDraft(lines=[
        LineDraft(role="hook", text="Почему одна кривая не скажет, где коллектор?", on_screen="Где коллектор?"),
        LineDraft(role="solution", text="В смешанном разрезе нужны расчёты и геология.", on_screen="Нужен расчёт", claim_id=cid),
        LineDraft(role="predict", text="Как думаете, что ниже?", on_screen="Ваш прогноз?"),
        LineDraft(role="answer", text="Без расчёта коллектор не выделить.", on_screen="Только расчёт", claim_id=cid),
        LineDraft(role="honesty", text="Ствол на экране шире настоящего.", on_screen="Не в масштабе", claim_id=cid),
        LineDraft(role="transfer", text="Где ваша модель видит один признак?", on_screen="А в ваших данных?"),
    ], prediction_question="?", transfer_question="?", data_it_bridge="…")
    spec = _assemble(good, NODE, T1, params, shots, ex, "v", "latyshova_gl5_par17-20", "short", "тест")
    assert check_spec(spec, NODE) == []
    spec.lines.append(Line(id="LX", role="data", text="Гамма-излучение отражается от глины, порог 999.", on_screen="ГК отражается", claim_id=cid))
    errs = " ".join(check_spec(spec, NODE))
    assert "misc.gr_reflects" in errs and "999" in errs
