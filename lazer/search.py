"""In-game beatmap listing for o!ily lazer.

lazer's beatmap browser asks /api/v2/beatmapsets/search. o!ily has no copy of
every beatmap, so the search goes to a beatmap mirror (osu.direct first, then
catboy.best) and the answer is reshaped into what lazer expects. Downloads
already go to the mirror (see /beatmapsets/{id}/download in main.py).

Mirrors answer in either osu!api v2 shape (sets with a "beatmaps" list) or
osu!api v1 shape (one row per difficulty); both are handled.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable

import httpx

from beatmaps import WEB_STATUS_NAME, BeatmapStore

PAGE_SIZE = 50

# Mirrors to try, in order. Each is (url, how to build its query parameters).
MIRRORS = [m for m in os.environ.get("MIRROR_SEARCH", "https://osu.direct/api/v2/search,https://catboy.best/api/v2/search").split(",") if m]

# lazer's status filter -> mirror status number (None = any).
STATUS_FILTER = {
    "": None, "leaderboard": None, "ranked": 1, "qualified": 3, "loved": 4,
    "favourites": None, "pending": 0, "wip": -1, "graveyard": -2, "mine": None, "any": None,
}
STATUS_NUMBER = {v: k for k, v in WEB_STATUS_NAME.items()}

http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "o!ily-lazer/1.0 (+https://oily.ilystudios.net)"})
_memo: dict[str, tuple[float, dict]] = {}


def mirror_params(query: str, mode: int | None, status: int | None, sort: str, offset: int) -> dict[str, Any]:
    """Parameters for the mirror. Both spellings are sent, as mirrors name them differently."""
    params: dict[str, Any] = {"q": query, "query": query, "amount": PAGE_SIZE, "limit": PAGE_SIZE, "offset": offset}
    if mode is not None:
        params["mode"] = mode
        params["m"] = mode
    if status is not None:
        params["status"] = status
        params["s"] = status
    if sort:
        params["sort"] = sort
    return params


def _sets_from(data: Any) -> list[dict] | None:
    """The beatmap sets in a mirror answer, or None if it isn't a list of sets."""
    if isinstance(data, dict):
        for key in ("beatmapsets", "data", "results"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
    if not isinstance(data, list):
        return None
    return [d for d in data if isinstance(d, dict)]


def _covers(set_id: int) -> dict:
    return {k: f"https://assets.ppy.sh/beatmaps/{set_id}/covers/{v}" for k, v in {
        "cover": "cover.jpg", "cover@2x": "cover@2x.jpg", "card": "card.jpg", "card@2x": "card@2x.jpg",
        "list": "list.jpg", "list@2x": "list@2x.jpg", "slimcover": "slimcover.jpg", "slimcover@2x": "slimcover@2x.jpg"}.items()}


def _status_number(value: Any) -> int:
    if isinstance(value, str):
        return STATUS_NUMBER.get(value, 0) if not value.lstrip("-").isdigit() else int(value)
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def normalise(entries: list[dict]) -> list[dict]:
    """Turns mirror results (v1 rows or v2 sets) into osu!api v2 sets that lazer can read."""
    # v1 rows: one per difficulty, grouped into sets.
    if entries and "beatmaps" not in entries[0] and "beatmapset_id" in entries[0] and "difficultyrating" in entries[0]:
        groups: dict[int, list[dict]] = {}
        for row in entries:
            groups.setdefault(int(row.get("beatmapset_id") or 0), []).append(row)
        return [BeatmapStore.web_set(rows) for set_id, rows in groups.items() if set_id > 0]

    out = []
    for s in entries:
        set_id = int(s.get("id") or s.get("beatmapset_id") or 0)
        if set_id <= 0:
            continue
        status = _status_number(s.get("ranked", s.get("status")))
        s = dict(s)
        s["id"] = set_id
        s["status"] = WEB_STATUS_NAME.get(status, "pending")
        s["ranked"] = status
        s.setdefault("artist", "")
        s.setdefault("title", "")
        s.setdefault("artist_unicode", s["artist"])
        s.setdefault("title_unicode", s["title"])
        s.setdefault("creator", "")
        s.setdefault("source", "")
        s.setdefault("tags", "")
        s.setdefault("play_count", 0)
        s.setdefault("favourite_count", 0)
        s.setdefault("nsfw", False)
        s.setdefault("video", False)
        s.setdefault("storyboard", False)
        s.setdefault("has_favourited", False)
        if not isinstance(s.get("covers"), dict) or not s["covers"]:
            s["covers"] = _covers(set_id)
        s.setdefault("preview_url", f"//b.ppy.sh/preview/{set_id}.mp3")
        s.setdefault("availability", {"download_disabled": False, "more_information": None})

        beatmaps = []
        for b in s.get("beatmaps") or []:
            if not isinstance(b, dict):
                continue
            b = dict(b)
            b_status = _status_number(b.get("ranked", b.get("status", status)))
            b["beatmapset_id"] = set_id
            b["status"] = WEB_STATUS_NAME.get(b_status, "pending")
            b["ranked"] = b_status
            mode_int = int(b.get("mode_int", 0) or 0)
            if isinstance(b.get("mode"), int):
                mode_int = b["mode"]
                b["mode"] = ["osu", "taiko", "fruits", "mania"][mode_int] if 0 <= mode_int < 4 else "osu"
            b["mode_int"] = mode_int
            b.setdefault("convert", False)
            beatmaps.append(b)
        s["beatmaps"] = beatmaps
        out.append(s)
    return out


async def search(query: str, mode: int | None, status_filter: str, sort: str, cursor: str | None,
                 fetch: Callable[[str, dict], Any] | None = None) -> dict:
    """lazer's beatmap search, answered from a mirror. `fetch` replaces the HTTP call in tests."""
    try:
        offset = max(0, int(cursor or 0))
    except ValueError:
        offset = 0

    status = STATUS_FILTER.get(status_filter or "", None)
    params = mirror_params(query or "", mode, status, sort or "", offset)
    key = repr(sorted(params.items()))
    hit = _memo.get(key)
    if hit and time.time() - hit[0] < 300:
        return hit[1]

    sets: list[dict] | None = None
    for url in MIRRORS:
        try:
            if fetch is not None:
                data = await fetch(url, params)
            else:
                r = await http.get(url, params=params)
                if r.status_code != 200:
                    continue
                data = r.json()
        except Exception:
            continue
        sets = _sets_from(data)
        if sets is not None:
            break

    if sets is None:
        return {"beatmapsets": [], "cursor_string": None, "total": 0, "error": "The beatmap mirror didn't answer. Try again in a moment."}

    result = normalise(sets)
    answer = {
        "beatmapsets": result,
        # A full page means there may be more.
        "cursor_string": str(offset + len(sets)) if len(sets) >= PAGE_SIZE else None,
        "total": offset + len(result) + (1 if len(sets) >= PAGE_SIZE else 0),
        "search": {"sort": sort or "relevance_desc"},
        "recommended_difficulty": None,
        "error": None,
    }
    if len(_memo) > 500:
        _memo.clear()
    _memo[key] = (time.time(), answer)
    return answer
