"""Ручная проверка LLM: структурированный ответ по pydantic-модели и по JSON-схеме.
Запуск: uv run python tests/smoke_llm.py  (тратит токены, поэтому не входит в pytest)."""
import sys
import time

from pydantic import BaseModel

from g2c.llm import generate

sys.stdout.reconfigure(encoding="utf-8")


class Claim(BaseModel):
    text: str
    kind: str
    quote: str


t = time.time()
obj, u = generate(
    "Из фразы извлеки одно утверждение; kind = observation | inference | assumption; quote — дословная часть фразы.\n"
    "Фраза: «Нефтеносные коллекторы отмечаются высоким удельным сопротивлением по сравнению с водоносными».",
    role="structurer", schema=Claim, stage="ping", use_cache=False)
print("pydantic:", obj.model_dump(), f"токены {u.tokens_in}/{u.tokens_out}, {u.seconds} с")

obj, u = generate('Верни JSON с полем ok = true.', role="fast",
                  schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]},
                  stage="ping", use_cache=False)
print("json-schema:", obj, f"{u.seconds} с")
