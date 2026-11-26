"""Единый вход к языковой модели.

Код конвейера вызывает generate(role=..., ...) и не знает, какая модель стоит за ролью.
Каждый вызов пишет строку в work/usage.jsonl (токены, деньги, этап, глава, узел) — из неё строится metrics.csv.
Ответы кэшируются по хэшу запроса: повторный прогон не тратит деньги.
Замена провайдера: добавить класс с методом _call и выбрать его в config.yaml → llm.provider.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from .paths import WORK
from .settings import config, model_for, price

CACHE_DIR = WORK / ".cache" / "llm"
USAGE_FILE = WORK / "usage.jsonl"


@dataclass
class Usage:
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float | None
    cached: bool
    seconds: float


class LLMError(RuntimeError):
    pass


def _hash(*parts: Any) -> str:
    h = hashlib.sha256()
    for p in parts:
        if isinstance(p, bytes):
            h.update(p)
        else:
            h.update(json.dumps(p, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8"))
    return h.hexdigest()[:32]


def _schema_of(schema: type[BaseModel] | dict | None) -> dict | None:
    if schema is None:
        return None
    if isinstance(schema, dict):
        return schema
    return schema.model_json_schema()


class GeminiBackend:
    def __init__(self, key: str | None = None) -> None:
        from google import genai  # импорт здесь: без ключа остальной конвейер работает

        key = (key or os.getenv("GEMINI_API_KEY", "") or os.getenv("LLM_API_KEY", "")).strip()
        if not key:
            raise LLMError("Нет ключа API: задайте его в настройках приложения (шестерёнка)")
        from google.genai import types

        # без таймаута повисший запрос ждёт бесконечно; 5 минут с запасом для длинных ответов
        timeout_ms = int(config()["llm"].get("timeout_s", 300) * 1000)
        self.client = genai.Client(api_key=key, http_options=types.HttpOptions(timeout=timeout_ms))

    def call(self, model: str, prompt: str, schema: type[BaseModel] | dict | None,
             images: list[Path], temperature: float) -> tuple[str, int, int]:
        from google.genai import types

        parts: list[Any] = [prompt]
        for img in images:
            mime = "image/png" if img.suffix.lower() == ".png" else "image/jpeg"
            parts.append(types.Part.from_bytes(data=img.read_bytes(), mime_type=mime))
        cfg: dict[str, Any] = {"temperature": temperature}
        if schema is not None:
            cfg["response_mime_type"] = "application/json"
            if isinstance(schema, dict):
                cfg["response_json_schema"] = schema
            else:
                cfg["response_schema"] = schema
        resp = self.client.models.generate_content(
            model=model, contents=parts, config=types.GenerateContentConfig(**cfg))
        um = resp.usage_metadata
        return (resp.text or "", int(getattr(um, "prompt_token_count", 0) or 0),
                int(getattr(um, "candidates_token_count", 0) or 0))


class OpenRouterBackend:
    """OpenRouter (OpenAI-совместимый API): одна точка доступа к сотням моделей, в том числе бесплатным (:free)."""

    URL = "https://openrouter.ai/api/v1/chat/completions"

    def __init__(self, key: str | None = None) -> None:
        import httpx

        self.key = (key or os.getenv("OPENROUTER_API_KEY", "") or os.getenv("LLM_API_KEY", "")).strip()
        if not self.key:
            raise LLMError("Нет ключа API: задайте его в настройках приложения (шестерёнка)")
        self.http = httpx.Client(timeout=config()["llm"].get("timeout_s", 300))

    def call(self, model: str, prompt: str, schema: type[BaseModel] | dict | None,
             images: list[Path], temperature: float) -> tuple[str, int, int]:
        import base64

        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images:
            mime = "image/png" if img.suffix.lower() == ".png" else "image/jpeg"
            b64 = base64.b64encode(img.read_bytes()).decode()
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})
        body: dict[str, Any] = {"model": model, "temperature": temperature,
                                "messages": [{"role": "user", "content": content if images else prompt}]}
        if schema is not None:
            body["response_format"] = {"type": "json_schema",
                                       "json_schema": {"name": "answer", "strict": False, "schema": _schema_of(schema)}}
        r = self.http.post(self.URL, json=body, headers={"Authorization": f"Bearer {self.key}",
                                                         "X-Title": "glava-kurs"})
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"{data['error']}")
        text = data["choices"][0]["message"].get("content") or ""
        if schema is not None:
            text = _extract_json(text)
        u = data.get("usage") or {}
        return text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0))


def _extract_json(text: str) -> str:
    """Модели иногда оборачивают JSON в ```json … ``` или пишут текст вокруг — вырезаем сам объект."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    i, j = t.find("{"), t.rfind("}")
    return t[i:j + 1] if i >= 0 and j > i else t


_BACKENDS = {"gemini": GeminiBackend, "openrouter": OpenRouterBackend}
_backends: dict[str, Any] = {}


def provider_of(model: str) -> str:
    """Модель с «/» в названии (vendor/model[:free]) — OpenRouter, иначе — Gemini."""
    return "openrouter" if "/" in model else "gemini"


def _get_backend(model: str):
    p = provider_of(model)
    if p not in _backends:
        _backends[p] = _BACKENDS[p]()
    return _backends[p]


def check_model(key: str, model: str) -> dict:
    """Проверка из окна настроек: работает ли ИИ с этим ключом и моделью (без сохранения и без кэша)."""
    t0 = time.time()
    try:
        backend = _BACKENDS[provider_of(model)](key=key or None)
        text, tin, tout = backend.call(model, "Ответь одним словом: работает?", None, [], 0.0)
        return {"ok": bool(text.strip()), "answer": text.strip()[:80], "seconds": round(time.time() - t0, 1)}
    except Exception as e:
        msg = str(e)
        hint = ("неверный ключ" if any(s in msg for s in ("401", "403", "API key", "Unauthorized", "invalid")) else
                "модель не найдена — проверьте название" if any(s in msg for s in ("404", "not found", "No endpoints", "not a valid model")) else
                "исчерпан лимит запросов" if any(s in msg for s in ("429", "quota", "RESOURCE_EXHAUSTED", "Rate limit")) else
                "модель не подходит для текста" if "decisions model" in msg else "")
        return {"ok": False, "error": msg[:300], "hint": hint, "seconds": round(time.time() - t0, 1)}


def generate(prompt: str, *, role: str, schema: type[BaseModel] | dict | None = None,
             images: list[Path] | None = None, stage: str = "", chapter: str = "",
             node: str = "", temperature: float | None = None, use_cache: bool = True) -> tuple[Any, Usage]:
    """Вызвать модель роли `role`. Если задана schema — вернуть разобранный объект (pydantic или dict)."""
    model = model_for(role)
    images = images or []
    temperature = config()["llm"].get("temperature", 0.2) if temperature is None else temperature
    key = _hash(model, prompt, _schema_of(schema), temperature, *[i.read_bytes() for i in images])
    cache_file = CACHE_DIR / f"{key}.json"

    t0 = time.time()
    cached = False
    if use_cache and cache_file.exists():
        rec = json.loads(cache_file.read_text(encoding="utf-8"))
        text, tin, tout, cached = rec["text"], rec["tokens_in"], rec["tokens_out"], True
    else:
        last_err: Exception | None = None
        retries = config()["llm"].get("max_retries", 3)
        for attempt in range(retries):
            try:
                text, tin, tout = _get_backend(model).call(model, prompt, schema, images, temperature)
                if schema is not None:
                    json.loads(text)  # проверка, что пришёл JSON
                break
            except LLMError:
                raise
            except Exception as e:  # сеть, 503 «high demand», 429 лимиты, невалидный JSON
                last_err = e
                msg = str(e)
                if ("RESOURCE_EXHAUSTED" in msg and ("PerDay" in msg or "per day" in msg)) or "free-models-per-day" in msg:
                    # дневная квота кончилась — повторы бесполезны
                    raise LLMError(f"Дневная квота модели {model} исчерпана (бесплатный тариф). "
                                   "Нужен платный тариф или другая модель (config.yaml → llm.roles). "
                                   "Готовые ответы сохранены в кэше, повторный запуск продолжит с места остановки.") from e
                if "is a decisions model" in msg or "No endpoints found" in msg or "not a valid model" in msg:
                    raise LLMError(f"Модель {model} не подходит или не существует: {msg[:300]}") from e
                if attempt + 1 < retries:
                    time.sleep(min(60, 3 * 2 ** attempt))  # 3, 6, 12, 24, 48, 60 с
        else:
            raise LLMError(f"Модель {model} не ответила после {retries} попыток: {last_err}")
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps({"text": text, "tokens_in": tin, "tokens_out": tout}, ensure_ascii=False), encoding="utf-8")

    pin, pout = price(model)
    cost = None if (pin is None or pout is None) else (tin * pin + tout * pout) / 1e6
    usage = Usage(model, tin, tout, 0.0 if cached else cost, cached, round(time.time() - t0, 2))
    _log_usage(usage, stage, chapter, node)

    if schema is None:
        return text, usage
    data = json.loads(text)
    if isinstance(schema, dict):
        return data, usage
    return schema.model_validate(data), usage


def _log_usage(u: Usage, stage: str, chapter: str, node: str) -> None:
    USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
    rec = {"time": datetime.now().isoformat(timespec="seconds"), "stage": stage, "chapter": chapter,
           "node": node, "model": u.model, "tokens_in": u.tokens_in, "tokens_out": u.tokens_out,
           "cost_usd": u.cost_usd, "cached": u.cached, "seconds": u.seconds}
    with USAGE_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
