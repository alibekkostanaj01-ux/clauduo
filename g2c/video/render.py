"""Рендер кадра: сцена шаблона (по состоянию модели) + слой монтажа (подпись, субтитры, метки, плашка).

Размеры и безопасные зоны — из styleguide.json организаторов: 9:16 — 1080×1920, верх 220, низ 380, бока 70 px;
подписи не мельче 44 px. Глубина растёт вниз. Цвета пород, кривых, меток происхождения — из легенды.
"""
from __future__ import annotations

import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from matplotlib.colors import to_rgb  # noqa: E402

from .data import load_well  # noqa: E402
from .spec import SceneSpec, Shot  # noqa: E402
from .templates import REGISTRY, style  # noqa: E402

plt.rcParams["font.family"] = ["Segoe UI", "DejaVu Sans"]
DPI = 100
SIZES = {"short": (1080, 1920), "long": (1920, 1080)}
PT = 72 / DPI  # пиксели → пункты


def px(n: float) -> float:
    return n * PT


class Frame:
    def __init__(self, spec: SceneSpec):
        self.spec = spec
        self.W, self.H = SIZES[spec.format]
        self.S = style()
        self.tpl = REGISTRY[spec.template]
        self.well = load_well() if spec.template == "T1_well_log" else None
        sz = self.S["safe_zone"]
        if spec.format == "short":
            self.top = 1 - sz["top"] / self.H          # всё содержимое — ниже верхней безопасной зоны
            self.bottom = sz["bottom"] / self.H         # и выше нижней
            self.side = sz["side"] / self.W
        else:
            self.top, self.bottom, self.side = 0.94, 0.08, 0.04

    # ---------------------------------------------------------------------------------
    def render(self, shot: Shot, p: float, line, t: float) -> np.ndarray:
        S = self.S
        fig = plt.figure(figsize=(self.W / DPI, self.H / DPI), dpi=DPI, facecolor=S["background"])
        st = self.tpl.state(shot, p, self.spec.params)
        if self.spec.template == "T1_well_log":
            self._t1(fig, shot, st)
        else:
            self._t0(fig, shot, st, line)
        self._overlay(fig, shot, line)
        fig.canvas.draw()
        img = np.asarray(fig.canvas.buffer_rgba())[:, :, :3].copy()
        plt.close(fig)
        return img

    # --- T1: ствол + кривая ГК ---------------------------------------------------------------
    def _t1(self, fig, shot: Shot, st: dict) -> None:
        S, P, w = self.S, self.spec.params, self.well
        d0, d1 = P["depth_from"], P["depth_to"]
        short = self.spec.format == "short"
        # полосы по вертикали (9:16): подпись | графики | плашка и школа | субтитры — не пересекаются
        y0, y1 = (self.bottom + 0.20, self.top - 0.105) if short else (0.24, 0.80)
        if short:
            boxes = {"bore": [self.side, y0, 0.36, y1 - y0], "curve": [self.side + 0.43, y0, 1 - 2 * self.side - 0.43, y1 - y0]}
        else:
            boxes = {"bore": [0.05, y0, 0.22, y1 - y0], "curve": [0.32, y0, 0.30, y1 - y0], "scale": [0.68, y0, 0.27, y1 - y0]}
        m = (w.depth >= d0) & (w.depth <= d1)
        dep, gr = w.depth[m], np.clip(w.curves["GR"][m], 0, P["gr_max"])
        sand, shale = P["gr_sand"], P["gr_shale"]
        lith = S["lithology"]

        def rock_colors(interp: bool, upto: float | None = None):
            gray = to_rgb("#5a6470")
            if not interp:
                return np.tile(gray, (len(dep), 1))
            c = np.empty((len(dep), 3))
            c[:] = to_rgb(lith["dense"]["color"])
            c[gr <= sand] = to_rgb(lith["sand"]["color"])
            c[gr >= shale] = to_rgb(lith["shale"]["color"])
            if upto is not None:  # ниже границы интерпретации ещё не было — порода серая
                c[dep > upto] = gray
            return c

        # ствол и порода
        ab = fig.add_axes(boxes["bore"])
        ab.set_facecolor(S["panel"])
        ab.set_ylim(d1, d0)  # глубина растёт вниз
        ab.set_xlim(0, 1)
        if st["true_scale"]:
            col = rock_colors(True)
            ab.imshow(col[:, None, :], extent=[0, 1, d1, d0], aspect="auto", origin="upper")
            # истинный масштаб: ширина ствола (0,22 м) относительно окна глубин — почти невидимая линия
            frac = 0.22 / (d1 - d0) * (boxes["bore"][3] * self.H) / (boxes["bore"][2] * self.W)
            frac = max(frac, 1.5 / (boxes["bore"][2] * self.W))
            ab.add_patch(plt.Rectangle((0.5 - frac / 2, d0), frac, d1 - d0, color=S["fluids"]["mud"]["color"]))
            ab.text(0.5, d0 + (d1 - d0) * 0.5, "ствол\n0,22 м", color=S["text"], ha="left", va="center",
                    fontsize=px(36), transform=ab.transData, bbox=dict(fc=S["background"], ec="none", alpha=.7))
        else:
            col = rock_colors(st["interp"], st.get("interp_to"))
            ab.imshow(col[:, None, :], extent=[0, 1, d1, d0], aspect="auto", origin="upper")
            ab.add_patch(plt.Rectangle((0.41, d0), 0.18, d1 - d0, color=S["fluids"]["mud"]["color"]))
            tool = st["tool_depth"] if st["phase"] != "intro" else d0 + (d1 - d0) * 0.03
            ab.plot([0.5, 0.5], [d0, tool], color=S["muted"], lw=2)  # кабель
            th = (d1 - d0) * 0.035
            ab.add_patch(plt.Rectangle((0.455, tool - th / 2), 0.09, th, color=S["measurement"]["color"], zorder=5))
            # собственное излучение породы: частицы летят ИЗ породы К прибору (не «отражение»)
            g = st["gr_at_tool"] / P["gr_max"]
            n = int(3 + 10 * g)
            rng = np.random.default_rng(int(tool * 10))
            for k in range(n):
                side = -1 if k % 2 else 1
                x_start = 0.5 + side * rng.uniform(0.25, 0.45)
                yy = tool + rng.uniform(-1, 1) * th * 2
                ab.annotate("", xy=(0.5 + side * 0.06, tool), xytext=(x_start, yy),
                            arrowprops=dict(arrowstyle="->", color=S["curves"]["GR"]["color"], lw=1.6, alpha=0.8))
        ab.set_xticks([])
        ab.tick_params(colors=S["muted"], labelsize=px(28))
        ab.set_ylabel("Глубина, м", color=S["muted"], fontsize=px(30))
        for sp in ab.spines.values():
            sp.set_color(S["muted"])
        self._chip(fig, boxes["bore"], "схема", S["provenance"]["схема"])

        # кривая ГК
        ac = fig.add_axes(boxes["curve"])
        ac.set_facecolor(S["panel"])
        ac.set_ylim(d1, d0)
        ac.set_xlim(0, P["gr_max"])
        sel = dep <= st["curve_to"]
        ac.plot(gr[sel], dep[sel], color=S["curves"]["GR"]["color"], lw=2.4)
        if st["phase"] not in ("intro", "true_scale"):
            ac.plot([st["gr_at_tool"]], [st["tool_depth"]], "o", color=S["measurement"]["color"], ms=14, zorder=6)
        if st["interp"] or st["phase"] == "true_scale":
            for v, name in ((sand, "порог «чистая порода»"), (shale, "порог «глина»")):
                ac.axvline(v, color=S["muted"], ls="--", lw=1.5)
            ac.text(sand, d0, f" {sand:.0f}", color=S["muted"], fontsize=px(26), va="bottom")
            ac.text(shale, d0, f" {shale:.0f}", color=S["muted"], fontsize=px(26), va="bottom")
        if st["mask_from"] is not None:
            ac.add_patch(plt.Rectangle((0, st["mask_from"]), P["gr_max"], d1 - st["mask_from"], color=S["uncertainty"]["color"], alpha=0.35, hatch="//"))
            ac.text(P["gr_max"] / 2, (st["mask_from"] + d1) / 2, "?", color=S["text"], fontsize=px(160), ha="center", va="center", weight="bold")
        ac.set_xlabel("ГК, API", color=S["curves"]["GR"]["color"], fontsize=px(32))
        ac.tick_params(colors=S["muted"], labelsize=px(28))
        ac.grid(color="#2a3440", lw=0.8)
        for sp in ac.spines.values():
            sp.set_color(S["muted"])
        self._chip(fig, boxes["curve"], "реальные данные", S["provenance"]["реальные данные"])
        if st["interp"] and not short:
            fig.text(0.29, 0.5, "сигнал → вывод", color=S["text"], fontsize=px(34), ha="center", rotation=90)
        if not short:  # шкала решения
            asc = fig.add_axes(boxes["scale"])
            asc.set_facecolor(S["panel"])
            asc.hist(gr, bins=30, orientation="horizontal", color=S["curves"]["GR"]["color"], alpha=0.8)
            asc.axhline(sand, color=lith["sand"]["color"], lw=2)
            asc.axhline(shale, color=lith["shale"]["color"], lw=2)
            asc.set_ylabel("ГК, API", color=S["muted"], fontsize=px(28))
            asc.set_title("шкала решения", color=S["text"], fontsize=px(30))
            asc.tick_params(colors=S["muted"], labelsize=px(24))
            self._chip(fig, boxes["scale"], "реальные данные", S["provenance"]["реальные данные"])

    # --- T0: универсальная схема ----------------------------------------------------------------
    def _t0(self, fig, shot: Shot, st: dict, line) -> None:
        S = self.S
        cards = self.spec.params.get("cards", [])
        y = self.top - 0.12
        k = {"intro": 0, "cards": max(1, int(len(cards) * min(1, st["p"] + 0.01) + 0.999)), "mask": len(cards) - 1}.get(st["phase"], len(cards))
        for c in cards[:k]:
            txt = textwrap.fill(c["text"], 34 if self.spec.format == "short" else 70)
            fig.text(self.side + 0.02, y, txt, color=S["text"], fontsize=px(40), va="top",
                     bbox=dict(fc=S["panel"], ec=S["accent"] if "accent" in S else S["muted"], boxstyle="round,pad=0.6"))
            fig.text(1 - self.side - 0.02, y, f"стр. {c['page']}", color=S["muted"], fontsize=px(30), va="top", ha="right")
            y -= 0.035 * (txt.count("\n") + 1) + 0.05
        if st["phase"] == "mask":
            fig.text(0.5, y - 0.05, "?", color=S["text"], fontsize=px(200), ha="center", va="top", weight="bold")
        self._chip(fig, [self.side, self.bottom + 0.05, 1 - 2 * self.side, 0.0], "схема", S["provenance"]["схема"])

    # --- слой монтажа ------------------------------------------------------------------------------
    def _chip(self, fig, box, text, color):
        fig.text(box[0] + box[2] - 0.005, box[1] + box[3] + 0.006, text, color="white", fontsize=px(26), ha="right", va="bottom",
                 bbox=dict(fc=color, ec="none", boxstyle="round,pad=0.35"))

    def _overlay(self, fig, shot: Shot, line) -> None:
        S = self.S
        short = self.spec.format == "short"
        if line is not None and line.on_screen:  # ключевые слова — крупно, в безопасной зоне, не больше 2 строк
            size, width = (56, 26) if short else (48, 50)
            wrapped = textwrap.wrap(line.on_screen, width)
            if len(wrapped) > 2:
                size, width = (46, 32) if short else (40, 60)
                wrapped = textwrap.wrap(line.on_screen, width)[:2]
            fig.text(0.5, self.top - 0.005, "\n".join(wrapped), color=S["text"],
                     fontsize=px(size), weight="bold", ha="center", va="top")
        band = self.bottom + 0.135 if short else 0.15  # полоса между графиками и субтитрами
        if shot.plaque:
            fig.text(self.side, band, shot.plaque, color=S["background"], fontsize=px(32),
                     ha="left", va="bottom", bbox=dict(fc=S["measurement"]["color"], ec="none", boxstyle="round,pad=0.35"))
        fig.text(1 - self.side, band + (0.03 if short else 0), self.spec.school, color=S["muted"], fontsize=px(26), ha="right", va="bottom")
        if line is not None:  # субтитры полного текста для просмотра без звука; не выше полосы плашки
            sub = textwrap.wrap(line.text, 34 if short else 80)
            size = 44
            if short and len(sub) > 3:
                sub, size = textwrap.wrap(line.text, 40), 44  # 44 px — минимум по стайлгайду, поэтому шире, а не мельче
            fig.text(0.5, self.bottom + 0.004, "\n".join(sub), color=S["text"], fontsize=px(size),
                     ha="center", va="bottom", linespacing=1.15,
                     bbox=dict(fc="black", ec="none", alpha=0.55, boxstyle="round,pad=0.4"))
