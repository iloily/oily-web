"""Shared boards with osu! stable.

Lazer scores that aren't relax are saved as normal bancho.py scores (the
`scores` table, mode 0-3), so stable, lazer and the website all show one
leaderboard per map and one pp ranking per mode. Fun mods have no stable
mod bit, so they count like no mod (assist mods give 0 pp, see scoring.py).
Lazer Relax stays on its own board in the lazer_* tables.
"""
from __future__ import annotations

import json
import math
import os
import secrets

import scoring

# lazer-facing ids for scores that were set on stable (lazer's own score ids stay small)
STABLE_ID = 1 << 40
MAX_SCORE = 1_000_000
BASIC = {"great", "ok", "meh", "miss", "perfect", "good"}
# osu-web status -> bancho.py status
WEB_TO_BANCHO = {-2: 0, -1: 0, 0: 0, 1: 2, 2: 3, 3: 4, 4: 5}
PP_STATUSES = (2, 3)  # bancho ranked, approved
BOARD_STATUSES = (2, 3, 5)  # ... and loved

_redis = None


async def redis():
    global _redis
    if _redis is None:
        import redis.asyncio as aioredis
        _redis = aioredis.Redis(
            host=os.environ.get("REDIS_HOST", "redis"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            username=os.environ.get("REDIS_USER") or None,
            password=os.environ.get("REDIS_PASS") or None,
            db=int(os.environ.get("REDIS_DB", "0")),
        )
    return _redis


# ---------- score conversions ----------
def basic_count(stats: dict) -> int:
    return sum(int(v or 0) for k, v in (stats or {}).items() if k in BASIC)


def classic_score(ruleset_id: int, total: int, max_stats: dict) -> int:
    """Lazer's 'classic' display score, close to what stable would give. Same formula as lazer."""
    n = basic_count(max_stats)
    if ruleset_id == 0:
        return round((n ** 2 * 32.57 + 100000) * total / MAX_SCORE)
    if ruleset_id == 1:
        return round((n * 1109 + 100000) * total / MAX_SCORE)
    if ruleset_id == 2:
        return round((total / MAX_SCORE * n) ** 2 * 21.62 + total / 10)
    return int(total)


def stable_objects(ruleset_id: int, r: dict) -> int:
    if ruleset_id == 0:
        return r["n300"] + r["n100"] + r["n50"] + r["nmiss"]
    if ruleset_id == 1:
        return r["n300"] + r["n100"] + r["nmiss"]
    if ruleset_id == 2:
        return r["n300"] + r["nmiss"]
    return r["n300"] + r["n100"] + r["n50"] + r["nmiss"] + r["ngeki"] + r["nkatu"]


def standardised_score(ruleset_id: int, classic: int, r: dict) -> int:
    """The other way round, so stable scores sit in the right place on lazer's board."""
    n = stable_objects(ruleset_id, r)
    if ruleset_id == 0:
        return round(classic * MAX_SCORE / (n ** 2 * 32.57 + 100000))
    if ruleset_id == 1:
        return round(classic * MAX_SCORE / (n * 1109 + 100000))
    if ruleset_id == 2:
        a = 21.62 * n * n / MAX_SCORE ** 2
        if a <= 0:
            return round(classic * 10)
        return round((-0.1 + math.sqrt(0.01 + 4 * a * classic)) / (2 * a))
    return int(classic)


def mods_from_bits(bits: int) -> list[dict]:
    out = []
    order = ["EZ", "NF", "HT", "HR", "SD", "PF", "DT", "NC", "HD", "FI", "FL", "RX", "AP", "SO", "TD", "MR",
             "1K", "2K", "3K", "4K", "5K", "6K", "7K", "8K", "9K"]
    for a in order:
        b = scoring.LEGACY_BITS[a]
        if bits & b == b:
            out.append(a)
    if "NC" in out:
        out.remove("DT")
    if "PF" in out:
        out.remove("SD")
    return [{"acronym": a} for a in out]


def statistics_from_counts(ruleset_id: int, r: dict) -> tuple[dict, dict]:
    """statistics and maximum_statistics (lazer names) from stable hit counts."""
    if ruleset_id == 0:
        s = {"great": r["n300"], "ok": r["n100"], "meh": r["n50"], "miss": r["nmiss"]}
    elif ruleset_id == 1:
        s = {"great": r["n300"], "ok": r["n100"], "miss": r["nmiss"]}
    elif ruleset_id == 2:
        s = {"great": r["n300"], "large_tick_hit": r["n100"], "small_tick_hit": r["n50"], "small_tick_miss": r["nkatu"], "miss": r["nmiss"]}
    else:
        s = {"perfect": r["ngeki"], "great": r["n300"], "good": r["nkatu"], "ok": r["n100"], "meh": r["n50"], "miss": r["nmiss"]}
    top = "perfect" if ruleset_id == 3 else "great"
    m = {top: stable_objects(ruleset_id, r)}
    if ruleset_id == 2:
        m["large_tick_hit"] = r["n100"]
        m["small_tick_hit"] = r["n50"] + r["nkatu"]
    return {k: int(v) for k, v in s.items() if v}, m


# ---------- writing lazer scores into bancho.py ----------
class Stable:
    def __init__(self, db):
        self.db = db

    async def ensure_map(self, m: dict) -> dict | None:
        """bancho.py's maps row for this difficulty, added from the mirror data if bancho hasn't seen it yet."""
        bid = int(m.get("beatmap_id") or 0)
        row = await self.db.one("select * from maps where id = %s", (bid,))
        if row:
            return row
        web = int(m.get("web_status", 0))
        artist, title, creator, version = (str(m.get(k) or "") for k in ("artist", "title", "creator", "version"))
        await self.db.execute(
            "insert ignore into maps (server, id, set_id, status, md5, artist, title, version, creator, filename, last_update, "
            "total_length, max_combo, frozen, plays, passes, mode, bpm, cs, ar, od, hp, diff) values "
            "('osu!', %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, 0, 0, %s, %s, %s, %s, %s, %s, %s)",
            (bid, int(m.get("beatmapset_id") or 0), WEB_TO_BANCHO.get(web, 0), str(m.get("file_md5") or ""),
             artist[:128], title[:128], version[:128], creator[:19], f"{artist} - {title} ({creator}) [{version}].osu"[:256],
             str(m.get("last_update") or "2007-01-01 00:00:00"), int(float(m.get("total_length") or 0)), int(float(m.get("max_combo") or 0)),
             int(float(m.get("mode") or 0)), float(m.get("bpm") or 0), float(m.get("diff_size") or 0), float(m.get("diff_approach") or 0),
             float(m.get("diff_overall") or 0), float(m.get("diff_drain") or 0), round(float(m.get("difficultyrating") or 0), 3)))
        return await self.db.one("select * from maps where id = %s", (bid,))

    async def save(self, lazer_row: dict, m: dict, pp: float) -> int | None:
        """Save one lazer score (not relax) as a bancho.py score and update the player's stable stats. Returns the bancho score id."""
        if lazer_row.get("bancho_score_id"):
            return lazer_row["bancho_score_id"]
        bmap = await self.ensure_map(m)
        if bmap is None or bmap["md5"].lower() != lazer_row["beatmap_md5"].lower():
            return None
        if bmap["status"] not in BOARD_STATUSES and lazer_row["passed"]:
            return None  # stable doesn't keep scores on maps without a leaderboard either
        ruleset_id = int(lazer_row["ruleset_id"])
        mods = json.loads(lazer_row["mods"]) if isinstance(lazer_row["mods"], str) else lazer_row["mods"]
        stats = json.loads(lazer_row["statistics"]) if isinstance(lazer_row["statistics"], str) else lazer_row["statistics"]
        max_stats = json.loads(lazer_row["maximum_statistics"]) if isinstance(lazer_row["maximum_statistics"], str) else lazer_row["maximum_statistics"]
        counts = scoring.legacy_counts(ruleset_id, stats)
        score = classic_score(ruleset_id, int(lazer_row["total_score"]), max_stats)
        passed = bool(lazer_row["passed"])
        user_id, md5 = lazer_row["user_id"], bmap["md5"]

        status, prev = 0, None
        if passed:
            prev = await self.db.one("select id, pp, score from scores where userid = %s and map_md5 = %s and mode = %s and status = 2",
                                     (user_id, md5, ruleset_id))
            better = prev is None or pp > float(prev["pp"]) or (pp == float(prev["pp"]) and score > int(prev["score"]))
            status = 2 if better else 1
        perfect = counts["nmiss"] == 0 and int(lazer_row["max_combo"]) >= int(bmap["max_combo"] or 0)
        ended = lazer_row["ended_at"]
        started = lazer_row.get("started_at")
        elapsed = int((ended - started).total_seconds() * 1000) if started else int(bmap["total_length"] or 0) * 1000
        score_id = await self.db.execute(
            "insert into scores (map_md5, score, pp, acc, max_combo, mods, n300, n100, n50, nmiss, ngeki, nkatu, grade, status, mode, "
            "play_time, time_elapsed, client_flags, userid, perfect, online_checksum) values "
            "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s)",
            (md5, min(score, 2**31 - 1), min(pp, 9999.999), round(float(lazer_row["accuracy"]) * 100, 3), int(lazer_row["max_combo"]),
             scoring.legacy_mods(mods), counts["n300"], counts["n100"], counts["n50"], counts["nmiss"], counts["ngeki"], counts["nkatu"],
             lazer_row["grade"] if passed else "F", status, ruleset_id, ended, max(elapsed, 0), user_id, perfect, secrets.token_hex(16)))
        if status == 2 and prev:
            await self.db.execute("update scores set status = 1 where id = %s", (prev["id"],))
        await self.db.execute("update lazer_scores set bancho_score_id = %s where id = %s", (score_id, lazer_row["id"]))
        await self.db.execute("update maps set plays = plays + 1, passes = passes + %s where id = %s", (int(passed), bmap["id"]))

        await self.db.execute("insert ignore into stats (id, mode) values (%s, %s)", (user_id, ruleset_id))
        hits = counts["n300"] + counts["n100"] + counts["n50"] + (counts["ngeki"] + counts["nkatu"] if ruleset_id in (1, 3) else 0)
        await self.db.execute(
            "update stats set plays = plays + 1, playtime = playtime + %s, tscore = tscore + %s, total_hits = total_hits + %s, "
            "max_combo = greatest(max_combo, %s) where id = %s and mode = %s",
            (max(elapsed, 0) // 1000, score, hits, int(lazer_row["max_combo"]) if passed else 0, user_id, ruleset_id))
        if status == 2 and bmap["status"] in PP_STATUSES:
            await self.recalc(user_id, ruleset_id)
        return score_id

    async def recalc(self, user_id: int, mode: int) -> None:
        """pp, accuracy, ranked score and grade counts from the player's best scores, like bancho.py does on submit."""
        best = await self.db.all(
            "select s.pp, s.acc from scores s join maps m on m.md5 = s.map_md5 where s.userid = %s and s.mode = %s and s.status = 2 "
            "and m.status in (2, 3) order by s.pp desc", (user_id, mode))
        pp = acc = 0.0
        if best:
            pp = sum(r["pp"] * 0.95 ** i for i, r in enumerate(best)) + 416.6667 * (1 - 0.9994 ** len(best))
            acc = sum(r["acc"] * 0.95 ** i for i, r in enumerate(best)) * (100.0 / (20 * (1 - 0.95 ** len(best)))) / 100
        g = await self.db.one(
            "select coalesce(sum(s.score), 0) rscore, sum(s.grade = 'XH') xh, sum(s.grade = 'X') x, sum(s.grade = 'SH') sh, "
            "sum(s.grade = 'S') s, sum(s.grade = 'A') a from scores s join maps m on m.md5 = s.map_md5 "
            "where s.userid = %s and s.mode = %s and s.status = 2 and m.status in (2, 3)", (user_id, mode))
        await self.db.execute(
            "update stats set pp = %s, acc = %s, rscore = %s, xh_count = %s, x_count = %s, sh_count = %s, s_count = %s, a_count = %s "
            "where id = %s and mode = %s",
            (round(pp), min(acc, 100.0), int(g["rscore"] or 0), int(g["xh"] or 0), int(g["x"] or 0), int(g["sh"] or 0),
             int(g["s"] or 0), int(g["a"] or 0), user_id, mode))
        u = await self.db.one("select priv, country from users where id = %s", (user_id,))
        if u and u["priv"] & 1:
            try:
                r = await redis()
                await r.zadd(f"bancho:leaderboard:{mode}", {str(user_id): round(pp)})
                await r.zadd(f"bancho:leaderboard:{mode}:{u['country']}", {str(user_id): round(pp)})
            except Exception as exc:
                print("redis rank update failed", exc, flush=True)
