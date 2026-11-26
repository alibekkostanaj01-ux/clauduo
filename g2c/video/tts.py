"""Озвучка: онлайн-голоса Microsoft (edge-tts; наружу уходит только текст сценария) со словарём произношений.

Подготовка текста для голоса (субтитры остаются как в сценарии):
- сокращения и обозначения читаются по словарю доменного пакета (ГК → «гэ ка», ВНК → «вэ эн ка»);
- ударения из словаря ставятся знаком U+0301 после ударной гласной.
Запасной путь (ТЗ: «нужен запасной путь»): если онлайн-синтез недоступен — тишина расчётной длины,
ролик собирается, а в паспорте пишется, что озвучка не выполнена.
"""
from __future__ import annotations

import asyncio
import re
import subprocess
import wave
from functools import lru_cache
from pathlib import Path

import numpy as np

from ..paths import DOMAIN

RATE = 24000
STRESS = "́"


@lru_cache(maxsize=4)
def pronunciation(domain: str = "geology") -> tuple[dict[str, str], dict[str, str]]:
    """(сокращение → как читать, слово → слово с ударением) из domain/<домен>/pronunciation.md."""
    text = (DOMAIN / domain / "pronunciation.md").read_text(encoding="utf-8")
    abbr, stress = {}, {}
    section = ""
    for line in text.splitlines():
        if line.startswith("#"):
            section = line.lower()
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2 or set(cells[0]) <= set("- :") or cells[0] in ("Пишется", "Слово"):
            continue
        if "удар" in section:
            if STRESS in cells[1]:
                stress[cells[0].lower()] = cells[1]
        elif cells[1] and not cells[0].islower():
            abbr[cells[0]] = cells[1].split(" или ")[0]
    return abbr, stress


def voice_text(text: str, domain: str = "geology") -> str:
    abbr, stress = pronunciation(domain)
    for a in sorted(abbr, key=len, reverse=True):
        text = re.sub(rf"(?<![\w]){re.escape(a)}(?![\w])", abbr[a], text)
    def st(m):
        w = m.group(0)
        s = stress.get(w.lower())
        return s if s else w
    return re.sub(r"[А-Яа-яЁё-]+", st, text)


def ffmpeg() -> str:
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def _to_wav(src: Path, dst: Path) -> None:
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(src), "-ac", "1", "-ar", str(RATE), str(dst)], check=True)


def read_wav(p: Path) -> np.ndarray:
    with wave.open(str(p)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768


async def _synth(text: str, voice: str, rate: str, out: Path) -> list[tuple[float, float, str]]:
    import edge_tts

    try:
        com = edge_tts.Communicate(text, voice, rate=rate, boundary="WordBoundary")
    except TypeError:  # старые версии без параметра boundary
        com = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with out.open("wb") as f:
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] == "WordBoundary":
                words.append((chunk["offset"] / 1e7, (chunk["offset"] + chunk["duration"]) / 1e7, chunk["text"]))
    return words


def synthesize(text: str, voice: str, out_dir: Path, key: str, rate: str = "-6%", domain: str = "geology") -> dict:
    """Синтез одной фразы → wav. Возвращает {wav, duration, words, ok, error}."""
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3, wav = out_dir / f"{key}.mp3", out_dir / f"{key}.wav"
    vt = voice_text(text, domain)
    try:
        if not wav.exists():
            last = None
            for attempt in range(3):  # онлайн-синтез иногда отвечает пусто — повторяем
                try:
                    words = asyncio.run(_synth(vt, voice, rate, mp3))
                    break
                except Exception as e:
                    last = e
                    import time
                    time.sleep(2 * (attempt + 1))
            else:
                raise last
            _to_wav(mp3, wav)
        else:
            words = []
        a = read_wav(wav)
        return {"wav": wav, "duration": len(a) / RATE, "words": words, "ok": True, "voice_text": vt}
    except Exception as e:  # нет сети, отказ сервиса — запасной путь: тишина расчётной длины
        dur = max(1.2, len(text.split()) / 2.2)
        silence = np.zeros(int(dur * RATE), dtype=np.int16)
        wav = out_dir / f"{key}_silent.wav"  # не под основным именем: при следующем запуске синтез повторится
        with wave.open(str(wav), "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE); w.writeframes(silence.tobytes())
        return {"wav": wav, "duration": dur, "words": [], "ok": False, "error": str(e)[:200], "voice_text": vt}
