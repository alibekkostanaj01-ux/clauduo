"""Настройки: config.yaml + .env. Модель выбирается по роли; LLM_MODEL в .env переопределяет все роли.

Ключи API хранятся только в .env (в git не попадает). Модели ролей — в config.yaml (в git, замораживаются тегом).
Окно настроек приложения (шестерёнка) пишет сюда через save_env / set_role_models.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from functools import lru_cache

import yaml
from dotenv import load_dotenv

from .paths import CONFIG_FILE, ROOT

ENV_FILE = ROOT / ".env"
ROLES_RU = {"structurer": "учебный слой и план", "writer": "тексты шагов и сценарии роликов", "fast": "задания, оценки, проверки",
            "judge": "модуль проверки", "vision": "распознавание сканов и кадров", "image": "иллюстрации C/D"}
KEYS = {"GEMINI_API_KEY": "Google Gemini (модели без «/»: gemini-3.8-flash)",
        "OPENROUTER_API_KEY": "OpenRouter (модели с «/»: nvidia/…, google/…)"}

load_dotenv(ENV_FILE, override=True)  # override: правка ключа в окне настроек сразу видна новым процессам


@lru_cache(maxsize=1)
def config() -> dict:
    return yaml.safe_load(CONFIG_FILE.read_text(encoding="utf-8"))


def model_for(role: str) -> str:
    override = os.getenv("LLM_MODEL", "").strip()
    if override:
        return override
    roles = config()["llm"]["roles"]
    if role not in roles:
        raise KeyError(f"Роль модели «{role}» не описана в config.yaml (есть: {', '.join(roles)})")
    return roles[role]


def price(model: str) -> tuple[float | None, float | None]:
    p = (config().get("prices_usd_per_1m") or {}).get(model) or {}
    return p.get("input"), p.get("output")


# --- запись настроек (окно «шестерёнка») ------------------------------------------------------
def env_values() -> dict[str, str]:
    from dotenv import dotenv_values

    return {k: v or "" for k, v in dotenv_values(ENV_FILE).items()} if ENV_FILE.exists() else {}


def mask(key: str) -> str:
    return "" if not key else (key[:4] + "…" + key[-4:] if len(key) > 12 else "••••")


def save_env(name: str, value: str) -> None:
    """Записать переменную в .env (остальные строки не трогаем) и в окружение текущего процесса."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines() if ENV_FILE.exists() else []
    lines = [l for l in lines if not l.startswith(f"{name}=")]
    if value:
        lines.append(f"{name}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)


def set_role_models(models: dict[str, str]) -> None:
    """Поменять модели ролей в config.yaml, сохранив комментарии файла (правка строк, а не перезапись YAML)."""
    text = CONFIG_FILE.read_text(encoding="utf-8")
    for role, model in models.items():
        if not model:
            continue
        text, n = re.subn(rf"(?m)^(\s+{re.escape(role)}:\s*)(\S+)", lambda m: m.group(1) + model, text, count=1)
        if n == 0:
            text = re.sub(r"(?m)^(\s+roles:\s*\n)", lambda m: m.group(1) + f"    {role}: {model}\n", text, count=1)
    CONFIG_FILE.write_text(text, encoding="utf-8")
    config.cache_clear()


def provider_of(model: str) -> str:
    return "openrouter" if "/" in model else "gemini"


def current_ai() -> tuple[str, str]:
    """(ключ, модель), которыми сейчас работает конвейер."""
    config.cache_clear()
    model = config()["llm"]["roles"].get("structurer", "")
    env = env_values()
    key = env.get("OPENROUTER_API_KEY" if provider_of(model) == "openrouter" else "GEMINI_API_KEY") or env.get("LLM_API_KEY", "")
    return key, model


def save_ai(key: str, model: str) -> None:
    """Окно настроек: одна модель на все этапы; ключ — в переменную провайдера этой модели (пользователь её не выбирает)."""
    model = model.strip()
    if model:
        set_role_models({r: model for r in ROLES_RU if r != "image"})
    if key.strip():
        var = "OPENROUTER_API_KEY" if provider_of(model or current_ai()[1]) == "openrouter" else "GEMINI_API_KEY"
        save_env(var, key.strip())


def list_models(provider: str) -> list[str]:
    """Модели, доступные по ключу: Gemini — генерация текста; OpenRouter — со структурированным ответом (нужен конвейеру)."""
    try:
        if provider == "gemini":
            key = os.getenv("GEMINI_API_KEY", "")
            if not key:
                return []
            from google import genai

            c = genai.Client(api_key=key)
            return sorted(m.name.split("/")[-1] for m in c.models.list()
                          if "generateContent" in (getattr(m, "supported_actions", None) or []) and "gemini" in m.name
                          and "image" not in m.name and "banana" not in m.name)
        req = urllib.request.Request("https://openrouter.ai/api/v1/models")
        data = json.load(urllib.request.urlopen(req, timeout=30))["data"]
        return sorted(m["id"] for m in data if "structured_outputs" in (m.get("supported_parameters") or []))
    except Exception:
        return []
