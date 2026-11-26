"""Проверка ключа OpenRouter и модели. Запуск: uv run python tests/smoke_openrouter.py <модель>"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(".env")
KEY = os.environ["OPENROUTER_API_KEY"]
MODEL = sys.argv[1] if len(sys.argv) > 1 else "perplexity/pplx-decider-flash-v1.3:free"


def call(url, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:600]


st, info = call("https://openrouter.ai/api/v1/key")
if st == 200:
    d = info["data"]
    print(f"Ключ: работает. Лимит: {d.get('limit')}, потрачено: {d.get('usage')}, бесплатный уровень: {d.get('is_free_tier')}, "
          f"лимит запросов: {d.get('rate_limit')}")
else:
    print("Ключ:", st, info)

t = time.time()
st, r = call("https://openrouter.ai/api/v1/chat/completions",
             {"model": MODEL, "messages": [{"role": "user", "content": "Ответь одним словом: работает?"}], "max_tokens": 20})
print(f"Модель {MODEL}: HTTP {st}, {time.time() - t:.1f} с")
print(r["choices"][0]["message"]["content"] if st == 200 else r)

if st == 200:
    t = time.time()
    st, r = call("https://openrouter.ai/api/v1/chat/completions", {
        "model": MODEL,
        "messages": [{"role": "user", "content": "Из фразы «Нефтеносные коллекторы отмечаются высоким удельным сопротивлением» извлеки утверждение: text, kind (observation|inference|assumption), quote (дословно)."}],
        "response_format": {"type": "json_schema", "json_schema": {"name": "claim", "strict": True, "schema": {
            "type": "object", "additionalProperties": False, "required": ["text", "kind", "quote"],
            "properties": {"text": {"type": "string"}, "kind": {"type": "string", "enum": ["observation", "inference", "assumption"]},
                           "quote": {"type": "string"}}}}}})
    print(f"Структурированный ответ: HTTP {st}, {time.time() - t:.1f} с")
    print(r["choices"][0]["message"]["content"] if st == 200 else r)
