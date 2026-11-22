"""Beatmap metadata for the lazer server.

Sources, in order: bancho.py's `maps` table (so o!ily Ranked status wins),
then osu.direct's osu!api v1 mirror (the same one bancho.py uses without an
API key). Results are turned into the osu-web JSON shapes lazer expects.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import httpx

MIRROR = os.environ.get("MIRROR_API", "https://osu.direct/api/get_beatmaps")
OSU_FILES = [Path(p) for p in os.environ.get("OSU_FILE_DIRS", "/bancho-data/osu").split(":")]
CACHE = Path(os.environ.get("DATA_DIR", "/data")) / "osu"
CACHE.mkdir(parents=True, exist_ok=True)

RULESET_SHORT = ["osu", "taiko", "fruits", "mania"]
# bancho.py's status numbers -> osu-web's
BANCHO_TO_WEB = {-1: -2, 0: 0, 1: 0, 2: 1, 3: 2, 4: 3, 5: 4}
WEB_STATUS_NAME = {-2: "graveyard", -1: "wip", 0: "pending", 1: "ranked", 2: "approved", 3: "qualified", 4: "loved"}

http = httpx.AsyncClient(timeout=15, headers={"User-Agent": "o!ily-lazer/1.0 (+https://oily.ilystudios.net)"})
_memo: dict[str, tuple[float, list[dict]]] = {}


async def _mirror(**params) -> list[dict]:
    key = repr(sorted(params.items()))
    hit = _memo.get(key)
    if hit and time.time() - hit[0] < 600:
        return hit[1]
    try:
        r = await http.get(MIRROR, params=params)
        data = r.json() if r.status_code == 200 else []
    except Exception:
        data = []
    if not isinstance(data, list):
        data = []
    _memo[key] = (time.time(), data)
    return data


def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _i(v, default=0) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


def _iso(v) -> str | None:
    if not v:
        return None
    s = str(v).replace(" ", "T")
    return s if s.endswith("Z") or "+" in s[10:] else s + "+00:00"


class BeatmapStore:
    def __init__(self, db):
        self.db = db

    async def _bancho_row(self, *, id: int | None = None, md5: str | None = None) -> dict | None:
        if id:
            return await self.db.one("select * from maps where id = %s", (id,))
        if md5:
            return await self.db.one("select * from maps where md5 = %s", (md5,))
        return None

    async def status(self, beatmap_id: int, fallback: int) -> int:
        row = await self._bancho_row(id=beatmap_id)
        if row is not None:
            return BANCHO_TO_WEB.get(row["status"], 0)
        return fallback

    async def lookup(self, *, id: int | None = None, md5: str | None = None, filename: str | None = None) -> dict | None:
        """One difficulty as a v1-style dict (plus 'web_status'), or None."""
        # the mirror can answer with the whole set (or the wrong difficulty), so always pick the exact one
        def pick(rows: list[dict], *, want_id: int | None = None, want_md5: str | None = None) -> dict | None:
            for r in rows:
                if want_id and _i(r.get("beatmap_id")) == want_id:
                    return r
                if want_md5 and str(r.get("file_md5") or "").lower() == want_md5.lower():
                    return r
            return None

        m = None
        if id:
            m = pick(await _mirror(b=id), want_id=id)
        elif md5:
            m = pick(await _mirror(h=md5), want_md5=md5)
            if m is None:
                row = await self._bancho_row(md5=md5)
                if row is None and filename:
                    row = await self.db.one("select * from maps where filename = %s", (filename,))
                if row:
                    m = pick(await _mirror(b=row["id"]), want_id=row["id"]) or pick(await _mirror(s=row["set_id"]), want_id=row["id"])
        if m is None and id:
            row = await self._bancho_row(id=id)
            if row:
                m = pick(await _mirror(s=row["set_id"]), want_id=id)
        if m is None:
            return None
        m = dict(m)
        m["web_status"] = await self.status(_i(m.get("beatmap_id")), _i(m.get("approved")))
        return m

    async def set_rows(self, set_id: int) -> list[dict]:
        rows = [dict(r) for r in await _mirror(s=set_id) if _i(r.get("beatmapset_id")) == set_id]
        for m in rows:
            m["web_status"] = await self.status(_i(m.get("beatmap_id")), _i(m.get("approved")))
        return rows

    # ---------- osu-web shapes ----------
    @staticmethod
    def web_beatmap(m: dict, with_set: bool = True) -> dict:
        status = m.get("web_status", _i(m.get("approved")))
        mode = _i(m.get("mode"))
        out = {
            "id": _i(m.get("beatmap_id")),
            "beatmapset_id": _i(m.get("beatmapset_id")),
            "mode": RULESET_SHORT[mode] if 0 <= mode < 4 else "osu",
            "mode_int": mode,
            "convert": False,
            "status": WEB_STATUS_NAME.get(status, "pending"),
            "ranked": status,
            "checksum": m.get("file_md5"),
            "user_id": _i(m.get("creator_id")),
            "version": m.get("version") or "",
            "difficulty_rating": _f(m.get("difficultyrating")),
            "total_length": _i(m.get("total_length")),
            "hit_length": _i(m.get("hit_length")),
            "bpm": _f(m.get("bpm")),
            "cs": _f(m.get("diff_size")),
            "ar": _f(m.get("diff_approach")),
            "accuracy": _f(m.get("diff_overall")),
            "drain": _f(m.get("diff_drain")),
            "count_circles": _i(m.get("count_normal")),
            "count_sliders": _i(m.get("count_slider")),
            "count_spinners": _i(m.get("count_spinner")),
            "max_combo": _i(m.get("max_combo")),
            "playcount": _i(m.get("playcount")),
            "passcount": _i(m.get("passcount")),
            "last_updated": _iso(m.get("last_update")),
            "is_scoreable": status in (1, 2, 4),
            "url": f"https://osu.ppy.sh/b/{_i(m.get('beatmap_id'))}",
        }
        if with_set:
            out["beatmapset"] = BeatmapStore.web_set([m], with_beatmaps=False)
        return out

    @staticmethod
    def web_set(rows: list[dict], with_beatmaps: bool = True) -> dict:
        m = rows[0]
        set_id = _i(m.get("beatmapset_id"))
        status = max((r.get("web_status", _i(r.get("approved"))) for r in rows), default=0)
        covers = {k: f"https://assets.ppy.sh/beatmaps/{set_id}/covers/{v}" for k, v in {
            "cover": "cover.jpg", "cover@2x": "cover@2x.jpg", "card": "card.jpg", "card@2x": "card@2x.jpg",
            "list": "list.jpg", "list@2x": "list@2x.jpg", "slimcover": "slimcover.jpg", "slimcover@2x": "slimcover@2x.jpg"}.items()}
        out = {
            "id": set_id,
            "artist": m.get("artist") or "",
            "artist_unicode": m.get("artist_unicode") or m.get("artist") or "",
            "title": m.get("title") or "",
            "title_unicode": m.get("title_unicode") or m.get("title") or "",
            "creator": m.get("creator") or "",
            "user_id": _i(m.get("creator_id")),
            "source": m.get("source") or "",
            "tags": m.get("tags") or "",
            "status": WEB_STATUS_NAME.get(status, "pending"),
            "ranked": status,
            "bpm": _f(m.get("bpm")),
            "covers": covers,
            "preview_url": f"//b.ppy.sh/preview/{set_id}.mp3",
            "play_count": sum(_i(r.get("playcount")) for r in rows),
            "favourite_count": _i(m.get("favourite_count")),
            "nsfw": False,
            "spotlight": False,
            "video": bool(_i(m.get("video"))),
            "storyboard": bool(_i(m.get("storyboard"))),
            "submitted_date": _iso(m.get("submit_date")),
            "ranked_date": _iso(m.get("approved_date")),
            "last_updated": _iso(m.get("last_update")),
            "availability": {"download_disabled": bool(_i(m.get("download_unavailable"))), "more_information": None},
            "genre": {"id": _i(m.get("genre_id")), "name": ""},
            "language": {"id": _i(m.get("language_id")), "name": ""},
            "has_favourited": False,
        }
        if with_beatmaps:
            out["beatmaps"] = [BeatmapStore.web_beatmap(r, with_set=False) for r in rows]
        return out

    # ---------- .osu files (for pp) ----------
    async def osu_file(self, beatmap_id: int, md5: str | None) -> Path | None:
        for d in OSU_FILES + [CACHE]:
            p = d / f"{beatmap_id}.osu"
            if p.exists() and p.stat().st_size > 0:
                if md5 is None or d != CACHE:
                    return p
                import hashlib
                if hashlib.md5(p.read_bytes()).hexdigest() == md5:
                    return p
        try:
            r = await http.get(f"https://osu.ppy.sh/osu/{beatmap_id}")
            if r.status_code == 200 and r.content:
                p = CACHE / f"{beatmap_id}.osu"
                p.write_bytes(r.content)
                return p
        except Exception:
            pass
        return None
