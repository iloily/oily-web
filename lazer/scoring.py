"""Mods, boards and pp for lazer scores."""
from __future__ import annotations

import math
from pathlib import Path

LEGACY_BITS = {
    "NF": 1, "EZ": 2, "TD": 4, "HD": 8, "HR": 16, "SD": 32, "DT": 64, "RX": 128, "HT": 256,
    "NC": 512 | 64, "FL": 1024, "SO": 4096, "AP": 8192, "PF": 16384 | 32,
    "4K": 32768, "5K": 65536, "6K": 131072, "7K": 262144, "8K": 524288, "FI": 1048576,
    "9K": 16777216, "1K": 67108864, "3K": 134217728, "2K": 268435456, "MR": 1073741824,
    "DC": 256,  # daycore plays like HT for pp
}
# mods allowed on the normal boards (with default settings, except Classic)
VANILLA_OK = {"NF", "EZ", "HD", "HR", "SD", "PF", "DT", "NC", "HT", "DC", "FL", "SO", "TD", "CL", "FI",
              "1K", "2K", "3K", "4K", "5K", "6K", "7K", "8K", "9K", "10K"}
SETTINGS_OK = {"CL"}

BOARD_VANILLA, BOARD_RELAX, BOARD_MODS = "vn", "rx", "mods"


def acronyms(mods: list[dict]) -> list[str]:
    return [str(m.get("acronym", "")).upper() for m in mods or []]


def board_for(mods: list[dict], ruleset_id: int) -> str:
    """Which leaderboard a score belongs on: vanilla, relax, or the 'anything goes' mods board."""
    names = set(acronyms(mods))
    for m in mods or []:
        if m.get("settings") and str(m.get("acronym", "")).upper() not in SETTINGS_OK:
            return BOARD_MODS  # custom rates and other tweaked mods
    if "RX" in names and ruleset_id in (0, 1, 2):
        return BOARD_RELAX if names - {"RX"} <= VANILLA_OK else BOARD_MODS
    return BOARD_VANILLA if names <= VANILLA_OK else BOARD_MODS


def legacy_mods(mods: list[dict]) -> int:
    bits = 0
    for a in acronyms(mods):
        bits |= LEGACY_BITS.get(a, 0)
    return bits


def hit_counts(ruleset_id: int, stats: dict) -> dict:
    s = {k: int(v or 0) for k, v in (stats or {}).items()}
    if ruleset_id == 0:
        return {"n300": s.get("great", 0), "n100": s.get("ok", 0), "n50": s.get("meh", 0), "n_misses": s.get("miss", 0)}
    if ruleset_id == 1:
        return {"n300": s.get("great", 0), "n100": s.get("ok", 0), "n_misses": s.get("miss", 0)}
    if ruleset_id == 2:
        return {"n300": s.get("great", 0), "n100": s.get("large_tick_hit", 0), "n50": s.get("small_tick_hit", 0),
                "n_katu": s.get("small_tick_miss", 0), "n_misses": s.get("miss", 0) + s.get("large_tick_miss", 0)}
    return {"n_geki": s.get("perfect", 0), "n300": s.get("great", 0), "n_katu": s.get("good", 0),
            "n100": s.get("ok", 0), "n50": s.get("meh", 0), "n_misses": s.get("miss", 0)}


def legacy_counts(ruleset_id: int, stats: dict) -> dict:
    """count300/100/50/miss/geki/katu as osu! stable shows them."""
    h = hit_counts(ruleset_id, stats)
    return {"n300": h.get("n300", 0), "n100": h.get("n100", 0), "n50": h.get("n50", 0),
            "nmiss": h.get("n_misses", 0), "ngeki": h.get("n_geki", 0), "nkatu": h.get("n_katu", 0)}


# Lazer mods whose "speed_change" setting changes the song speed, with lazer's default speed.
RATE_MODS = {"DT": 1.5, "NC": 1.5, "HT": 0.75, "DC": 0.75}
# Difficulty Adjust setting name -> (Beatmap kwarg, lowest, highest). Highest/lowest are lazer's extended limits.
DA_SETTINGS = {
    "circle_size": ("cs", 0.0, 11.0),
    "approach_rate": ("ar", -10.0, 11.0),
    "overall_difficulty": ("od", 0.0, 11.0),
    "drain_rate": ("hp", 0.0, 11.0),
}


def _number(value) -> float | None:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def clock_rate(mods: list[dict]) -> float | None:
    """Exact song speed from lazer's DT/NC/HT/DC, the way official lazer pp uses it. None = no speed mod."""
    for m in mods or []:
        a = str(m.get("acronym", "")).upper()
        if a in RATE_MODS:
            rate = _number((m.get("settings") or {}).get("speed_change"))
            if rate is None:
                rate = RATE_MODS[a]
            return min(max(rate, 0.5), 2.0)
    return None


def difficulty_overrides(mods: list[dict]) -> dict:
    """Map stats set by lazer's Difficulty Adjust (DA). They replace the map's own values before speed is applied,
    like official lazer pp. Settings the player left alone keep the map's value."""
    out = {}
    for m in mods or []:
        if str(m.get("acronym", "")).upper() != "DA":
            continue
        for key, (kwarg, lo, hi) in DA_SETTINGS.items():
            v = _number((m.get("settings") or {}).get(key))
            if v is not None:
                out[kwarg] = min(max(v, lo), hi)
    return out


def calculate_pp(osu_file: Path, ruleset_id: int, mods: list[dict], stats: dict, combo: int) -> float:
    from akatsuki_pp_py import Beatmap, Calculator

    extra = {}
    rate = clock_rate(mods)
    if rate is not None:
        extra["clock_rate"] = rate
    calc = Calculator(mode=ruleset_id, mods=legacy_mods(mods), combo=combo, **hit_counts(ruleset_id, stats), **extra)
    beatmap = Beatmap(path=str(osu_file), **difficulty_overrides(mods))
    pp = calc.performance(beatmap).pp
    if math.isnan(pp) or math.isinf(pp):
        return 0.0
    return round(pp, 3)


def grade(ruleset_id: int, accuracy: float, mods: list[dict], stats: dict, passed: bool) -> str:
    """Fallback grade if the client didn't send one."""
    if not passed:
        return "F"
    hidden = bool({"HD", "FL", "FI"} & set(acronyms(mods)))
    misses = int((stats or {}).get("miss", 0))
    if accuracy >= 1.0:
        return "XH" if hidden else "X"
    if accuracy >= 0.95 and misses == 0:
        return "SH" if hidden else "S"
    if accuracy >= 0.9:
        return "A"
    if accuracy >= 0.8:
        return "B"
    if accuracy >= 0.7:
        return "C"
    return "D"
