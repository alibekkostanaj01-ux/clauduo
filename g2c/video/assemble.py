"""Сборка ролика из спецификации — одной командой (ТЗ: «ролик пересобирается из спецификации, а не из файла монтажа»).

1. проверки спецификации до рендера (check_spec) — с ошибками рендер не идёт (кроме --force);
2. озвучка каждой фразы → тайминг: фраза звучит, пока на экране её кадр (рассинхрон ≤ 1 кадра);
   пауза 1,3 с после вопроса-предсказания, финальное состояние держится 1,5 с;
3. кадры кодом из модели состояния шаблона, инварианты проверяются на каждом кадре;
4. звук: склейка, нормализация к −16 LUFS, замер громкости; темп (слов/мин); субтитры .srt;
5. паспорт ролика по passport.schema.json.
"""
from __future__ import annotations

import json
import re
import subprocess
import wave
from pathlib import Path

import numpy as np

from ..paths import OUT, ROOT
from ..validate import check_schema
from .render import SIZES, Frame
from .spec import SceneSpec, check_spec
from .templates import REGISTRY
from .tts import RATE, ffmpeg, read_wav, synthesize

FPS = 12
GAP, PREDICT_PAUSE, HEAD, TAIL = 0.35, 1.3, 0.3, 1.5


def _node(spec: SceneSpec) -> dict:
    lp = ROOT / spec.layer_file if spec.layer_file else ROOT / "work" / spec.chapter / "layer.json"
    layer = json.loads(lp.read_text(encoding="utf-8"))
    return next(n for n in layer["nodes"] if n["id"] == spec.node_id)


def timeline(spec: SceneSpec, out_dir: Path) -> tuple[np.ndarray, list[dict]]:
    """Озвучить фразы и расставить их по времени. Возвращает (аудио, сведения о фразах)."""
    tpl = REGISTRY[spec.template]
    audio = [np.zeros(int(HEAD * RATE), dtype=np.float32)]
    t = HEAD
    info = []
    for i, l in enumerate(spec.lines):
        if not l.shot:
            l.shot = tpl.role_shot[l.role]
        import hashlib

        rate = "+12%"
        h = hashlib.md5(f"{l.text}|{spec.voice}|{rate}".encode()).hexdigest()[:8]  # правка фразы → новая озвучка
        r = synthesize(l.text, spec.voice, out_dir / "tts", f"{i:02d}_{l.role}_{h}", rate=rate)
        a = read_wav(r["wav"])
        l.t_start, l.t_end = round(t, 2), round(t + len(a) / RATE, 2)
        audio.append(a)
        pause = GAP + (PREDICT_PAUSE if l.role == "predict" else 0)
        audio.append(np.zeros(int(pause * RATE), dtype=np.float32))
        info.append({"id": l.id, "ok": r["ok"], "error": r.get("error"), "words": len(l.text.split()),
                     "voice_text": r["voice_text"], "duration": round(len(a) / RATE, 2)})
        t = l.t_end + pause
    audio.append(np.zeros(int(TAIL * RATE), dtype=np.float32))
    return np.concatenate(audio), info


def _write_wav(a: np.ndarray, p: Path) -> None:
    with wave.open(str(p), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(RATE)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype(np.int16).tobytes())


def _loudness(p: Path) -> float | None:
    r = subprocess.run([ffmpeg(), "-hide_banner", "-nostats", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    m = re.findall(r"I:\s+(-?[\d.]+) LUFS", r.stderr)
    return float(m[-1]) if m else None


def _srt(spec: SceneSpec, p: Path) -> None:
    def ts(x):
        h, rem = divmod(x, 3600); m, s = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(s):02d},{int((s % 1) * 1000):03d}"
    p.write_text("\n".join(f"{i}\n{ts(l.t_start)} --> {ts(l.t_end)}\n{l.text}\n" for i, l in enumerate(spec.lines, 1)), encoding="utf-8")


def render_video(spec_path: Path, force: bool = False, require_mentor: bool = False) -> dict:
    spec = SceneSpec.load(spec_path)
    node = _node(spec)
    errs = check_spec(spec, node)
    if errs and not force:
        return {"ok": False, "stage": "проверка спецификации до рендера", "errors": errs}
    if require_mentor and not spec.mentor_approved:
        return {"ok": False, "stage": "ментор", "errors": ["спецификация не подписана ментором (mentor_approved=false)"]}
    work = spec_path.parent
    out_dir = OUT / "video"
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. звук и тайминг
    audio, line_info = timeline(spec, work)
    raw, norm_wav = work / "voice_raw.wav", work / "voice_norm.wav"
    _write_wav(audio, raw)
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(raw), "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                    "-ar", str(RATE), str(norm_wav)], check=True)
    duration = len(audio) / RATE

    # 2. кадры
    frame = Frame(spec)
    tpl = REGISTRY[spec.template]
    shots = {s.id: s for s in spec.shots}
    order = []  # интервалы кадров: от начала первой его фразы до начала следующего кадра
    for l in spec.lines:
        if not order or order[-1][0] != l.shot:
            order.append([l.shot, l.t_start, l.t_end])
        else:
            order[-1][2] = l.t_end
    for i in range(len(order)):
        order[i][2] = order[i + 1][1] if i + 1 < len(order) else duration
    order[0][1] = 0.0
    W, H = SIZES[spec.format]
    silent = work / "video_silent.mp4"
    import imageio_ffmpeg

    writer = imageio_ffmpeg.write_frames(str(silent), (W, H), fps=FPS, codec="libx264", pix_fmt_out="yuv420p",
                                         output_params=["-crf", "23"], macro_block_size=8)
    writer.send(None)
    inv_errors: set[str] = set()
    n_frames = int(duration * FPS)
    for k in range(n_frames):
        t = k / FPS
        sid, a, b = next(o for o in order if o[1] <= t < o[2] or o is order[-1])
        shot = shots[sid]
        p = (t - a) / max(1e-6, b - a)
        line = next((l for l in spec.lines if l.t_start <= t < (l.t_end + GAP)), None)
        st = tpl.state(shot, min(1.0, p), spec.params)
        inv_errors.update(tpl.check_state(st, spec.params))
        writer.send(frame.render(shot, min(1.0, p), line, t))
    writer.close()

    # 3. склейка
    final = out_dir / f"{spec.video_id}.mp4"
    subprocess.run([ffmpeg(), "-y", "-loglevel", "error", "-i", str(silent), "-i", str(norm_wav), "-c:v", "copy",
                    "-c:a", "aac", "-b:a", "128k", "-shortest", str(final)], check=True)
    _srt(spec, out_dir / f"{spec.video_id}.srt")
    lufs = _loudness(final)
    speech = sum(l.t_end - l.t_start for l in spec.lines)
    words = sum(len(l.text.split()) for l in spec.lines)
    wpm = round(words / (speech / 60), 1) if speech else None

    audio_check = {
        "voice": spec.voice, "synthesized": all(i["ok"] for i in line_info),
        "errors": [i["error"] for i in line_info if not i["ok"]],
        "tempo_wpm": wpm, "tempo_ok": bool(wpm and 110 <= wpm <= 150),
        "loudness_lufs": lufs, "loudness_ok": lufs is not None and abs(lufs + 16) <= 1.5,
        "sync_max_s": round(1 / FPS, 3), "sync_method": "кадр показывается, пока звучит его фраза (тайминг по длине синтеза)",
        "wer": None, "wer_note": "распознавание речи не подключено — термины проверить на слух",
        "listen_list": sorted({w for i in line_info for w in re.findall(r"[А-Яа-яЁё]+\u0301[а-яё]*", i["voice_text"])}),
        "lines": line_info,
    }
    passport = build_passport(spec, node, audio_check, sorted(inv_errors))
    pp = out_dir / f"{spec.video_id}.passport.json"
    pp.write_text(json.dumps(passport, ensure_ascii=False, indent=1), encoding="utf-8")
    spec.save(spec_path)  # с таймкодами
    schema_issues = check_schema(passport, "passport")
    return {"ok": True, "video": str(final), "duration_s": round(duration, 1), "frames": n_frames, "tempo_wpm": wpm,
            "loudness_lufs": lufs, "spec_errors_ignored": errs if force else [], "invariant_errors": sorted(inv_errors),
            "passport": str(pp), "passport_schema_errors": [i.message for i in schema_issues],
            "voice_ok": audio_check["synthesized"]}


def build_passport(spec: SceneSpec, node: dict, audio_check: dict, inv_errors: list[str]) -> dict:
    import json as _j

    from ..paths import DOMAIN

    checklist = {c["id"]: c for c in _j.loads((DOMAIN / "geology" / "physics_checklist.json").read_text(encoding="utf-8"))}
    tpl = REGISTRY[spec.template]
    pc = []
    for cid, c in checklist.items():
        if cid in spec.invariants or cid in tpl.invariants:
            ans, note = ("нет", "; ".join(inv_errors)) if inv_errors and cid in ("pc.curve_sync", "pc.depth_down") else \
                ("да", "проверено кодом шаблона на каждом кадре" if cid in ("pc.curve_sync", "pc.depth_down", "pc.objects_stable") else "заявлено в спецификации")
        else:
            ans, note = "н/п", ""
        pc.append({"item": c["item"], "answer": ans, "note": note})
    frames = []
    seen = set()
    for l in spec.lines:
        if l.shot in seen:
            continue
        seen.add(l.shot)
        s = next(s for s in spec.shots if s.id == l.shot)
        frames.append({"t": l.t_start, "class": s.frame_class, "label": s.label, "shot": s.id, "plaque": s.plaque})
    return {
        "video_id": spec.video_id, "node_id": spec.node_id,
        "node_card": {"title": node["title"], "difficulty": node.get("difficulty"), "pages": node.get("pages"),
                      "main": (node.get("layers", {}).get("main") or {}).get("text", "")},
        "format": "шортс 9:16" if spec.format == "short" else "разбор 16:9",
        "template": f"{tpl.id} — {tpl.name}",
        "script": [{"t_start": l.t_start, "t_end": l.t_end, "text": l.text, "claim_id": l.claim_id,
                    "is_analogy": l.is_analogy, "on_screen": l.on_screen, "role": l.role} for l in spec.lines],
        "sources": [s.model_dump() for s in spec.sources],
        "scene_spec": f"work/{spec.chapter}/video/{spec.video_id}/spec.json",
        "scene_objects": spec.objects, "invariants": spec.invariants, "assumptions": spec.assumptions,
        "frames": frames,
        "exaggerations": [e.text for e in spec.exaggerations],
        "prediction_question": spec.prediction_question, "transfer_question": spec.transfer_question,
        "data_it_bridge": spec.data_it_bridge,
        "physics_checklist": pc,
        "audio_check": audio_check,
        "references": spec.references,
        "mentor": {"approved": spec.mentor_approved, "note": spec.mentor_note},
        "planned_by": spec.planned_by,
        "rebuild": f"uv run g2c video-render work/{spec.chapter}/video/{spec.video_id}/spec.json",
    }
