"""Замер задержки Gemini при разных настройках размышления. Запуск: uv run python tests/smoke_latency.py"""
import os
import sys
import time

from dotenv import load_dotenv
from google import genai
from google.genai import types

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(".env")
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
MODEL = "gemini-3.8-flash"

variants = {
    "по умолчанию": {},
    "thinking_level=low": {"thinking_config": types.ThinkingConfig(thinking_level="low")},
    "thinking_budget=0": {"thinking_config": types.ThinkingConfig(thinking_budget=0)},
}
for name, extra in variants.items():
    t = time.time()
    try:
        r = client.models.generate_content(model=MODEL, contents="Ответь одним словом: работает?",
                                           config=types.GenerateContentConfig(**extra))
        um = r.usage_metadata
        print(f"{name}: {time.time() - t:.1f} с, ответ «{(r.text or '').strip()[:30]}», "
              f"токены вход {um.prompt_token_count}, выход {um.candidates_token_count}, "
              f"размышление {getattr(um, 'thoughts_token_count', None)}")
    except Exception as e:
        print(f"{name}: ошибка через {time.time() - t:.1f} с — {str(e)[:200]}")
