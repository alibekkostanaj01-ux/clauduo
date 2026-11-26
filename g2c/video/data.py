"""Открытые данные для кадров класса A: каротаж Volve из архива пакета вводных.

Скважина берётся из data/raw/spwla_volve_logs.zip, чистится (-999.25 → нет, выбросы ГК > 1000 API и NPHI > 1 —
артефакты каверн) и кэшируется компактно в data/volve/<скважина>.json (шаг 0,5 м).
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass

import numpy as np

from ..paths import ROOT

RAW = ROOT / "data" / "raw" / "spwla_volve_logs.zip"
CACHE = ROOT / "data" / "volve"
WELLS = {"15/9-19 A": "spwla_volve-main/15_9-19A/159-19A_LFP.las"}
CURVES = {"GR": "LFP_GR", "RT": "LFP_RT", "RHOB": "LFP_RHOB", "NPHI": "LFP_NPHI", "CALI": "LFP_CALI"}
SOURCE = "Данные Volve, Equinor ASA и бывшие партнёры по лицензии Volve"


@dataclass
class Well:
    name: str
    depth: np.ndarray          # м, растёт вниз
    curves: dict[str, np.ndarray]
    source: str = SOURCE

    def at(self, curve: str, d: float) -> float:
        return float(np.interp(d, self.depth, self.curves[curve]))


def load_well(name: str = "15/9-19 A", step: float = 0.5) -> Well:
    CACHE.mkdir(parents=True, exist_ok=True)
    cache = CACHE / (name.replace("/", "_").replace(" ", "") + ".json")
    if cache.exists():
        d = json.loads(cache.read_text(encoding="utf-8"))
        return Well(name, np.array(d["depth"]), {k: np.array(v, dtype=float) for k, v in d["curves"].items()})
    import lasio

    with zipfile.ZipFile(RAW) as z:
        text = z.read(WELLS[name]).decode("utf-8", errors="replace")
    las = lasio.read(io.StringIO(text), engine="normal")
    df = las.df()
    df = df[[c for c in CURVES.values() if c in df.columns]].copy()
    df.index = df.index.astype(float)
    if "LFP_GR" in df:
        df.loc[df["LFP_GR"] > 1000, "LFP_GR"] = np.nan
    if "LFP_NPHI" in df:
        df.loc[df["LFP_NPHI"] > 1, "LFP_NPHI"] = np.nan
    df = df.dropna(subset=["LFP_GR"])
    grid = np.arange(np.ceil(df.index.min()), np.floor(df.index.max()), step)
    curves = {}
    for short, col in CURVES.items():
        if col in df:
            s = df[col].dropna()
            curves[short] = np.round(np.interp(grid, s.index.values, s.values), 4) if len(s) else np.full(len(grid), np.nan)
    cache.write_text(json.dumps({"depth": grid.tolist(), "curves": {k: v.tolist() for k, v in curves.items()},
                                 "source": SOURCE}), encoding="utf-8")
    return Well(name, grid, curves)


def find_contrast_window(w: Well, length: float = 60.0) -> tuple[float, float]:
    """Окно глубин с наибольшим контрастом ГК (чтобы на коротком ролике были и глины, и песчаники)."""
    gr = w.curves["GR"]
    n = int(length / (w.depth[1] - w.depth[0]))
    best, best_i = -1.0, 0
    for i in range(0, len(gr) - n, max(1, n // 6)):
        seg = gr[i:i + n]
        score = np.nanpercentile(seg, 90) - np.nanpercentile(seg, 10)
        if score > best:
            best, best_i = score, i
    return float(w.depth[best_i]), float(w.depth[best_i + n - 1])
