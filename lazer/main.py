"""o!ily lazer server.

Speaks the subset of the osu-web API (and its OAuth) that osu!lazer needs for
solo play: login, profiles, beatmap lookups, leaderboards and score
submission. Accounts are bancho.py's, so one o!ily account works on stable and
lazer. Lazer scores go to three boards: vanilla, relax and mods.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import bcrypt
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import scoring
from beatmaps import RULESET_SHORT, BeatmapStore
from db import DB

DOMAIN = os.environ.get("DOMAIN", "oily.ilystudios.net")
SITE = f"https://{DOMAIN}"
AVATARS = f"https://a.{DOMAIN}"
DOWNLOAD = os.environ.get("MIRROR_DOWNLOAD", "https://osu.direct/api/d")
TOKEN_LIFETIME = 86400
UNRESTRICTED = 1
STAFF_BITS = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 14)

db = DB()
maps = BeatmapStore(db)
pp_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_: FastAPI):
    await db.connect()
    yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


def now() -> int:
    return int(time.time())


def iso(ts: int | datetime | None) -> str | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts.replace(tzinfo=timezone.utc).isoformat()
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def ruleset_id_from(short: str | None) -> int:
    if short in RULESET_SHORT:
        return RULESET_SHORT.index(short)
    return 0


# ---------- auth ----------
@app.post("/oauth/token")
async def oauth_token(request: Request):
    form = await request.form()
    grant = form.get("grant_type")
    if grant == "password":
        name = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))
        user = await db.one("select * from users where safe_name = %s", (name.lower().replace(" ", "_"),))
        pw_md5 = hashlib.md5(password.encode()).hexdigest().encode()
        if not user or not bcrypt.checkpw(pw_md5, user["pw_bcrypt"].encode()):
            return JSONResponse({"error": "invalid_grant", "error_description": "Wrong username or password.",
                                 "hint": "Use your o!ily account (the same one as on osu!stable).", "message": "Wrong username or password."}, 400)
        if not user["priv"] & UNRESTRICTED:
            return JSONResponse({"error": "invalid_grant", "error_description": "This account is restricted.", "message": "This account is restricted."}, 400)
    elif grant == "refresh_token":
        row = await db.one("select * from lazer_tokens where refresh_token = %s", (str(form.get("refresh_token", "")),))
        if not row:
            return JSONResponse({"error": "invalid_grant", "error_description": "Please log in again.", "message": "Please log in again."}, 400)
        await db.execute("delete from lazer_tokens where refresh_token = %s", (row["refresh_token"],))
        user = {"id": row["user_id"]}
    else:
        return JSONResponse({"error": "unsupported_grant_type", "message": "Unsupported login type."}, 400)

    access, refresh = secrets.token_hex(32), secrets.token_hex(32)
    await db.execute(
        "insert into lazer_tokens (access_token, refresh_token, user_id, expires_at, created_at) values (%s, %s, %s, %s, %s)",
        (access, refresh, user["id"], now() + TOKEN_LIFETIME, now()),
    )
    await db.execute("delete from lazer_tokens where expires_at < %s", (now() - 30 * 86400,))
    return {"token_type": "Bearer", "expires_in": TOKEN_LIFETIME, "access_token": access, "refresh_token": refresh}


async def current_user(authorization: str | None = Header(None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Unauthorized")
    row = await db.one(
        "select u.* from lazer_tokens t join users u on u.id = t.user_id where t.access_token = %s and t.expires_at > %s",
        (authorization[7:].strip(), now()),
    )
    if not row:
        raise HTTPException(401, "Unauthorized")
    return row


async def optional_user(authorization: str | None = Header(None)) -> dict | None:
    try:
        return await current_user(authorization)
    except HTTPException:
        return None


# ---------- users ----------
def level_for(total_score: int) -> dict:
    """osu!'s level curve: score needed to reach level n."""
    def need(n: int) -> float:
        if n <= 100:
            return 5000 / 3 * (4 * n ** 3 - 3 * n ** 2 - n) + 1.25 * 1.8 ** (n - 60)
        return 26931190827 + 99999999999 * (n - 100)
    level = 1
    while level < 120 and need(level + 1) <= total_score:
        level += 1
    lo, hi = need(level), need(level + 1)
    return {"current": level, "progress": int(max(0, min(99, (total_score - lo) / (hi - lo) * 100)))}


async def ranks_for(user_id: int, ruleset_id: int, board: str, country: str) -> tuple[int | None, int | None]:
    me = await db.one("select pp from lazer_stats where user_id = %s and ruleset_id = %s and board = %s", (user_id, ruleset_id, board))
    if not me or me["pp"] <= 0:
        return None, None
    above = await db.one(
        "select count(*) n from lazer_stats s join users u on u.id = s.user_id "
        "where s.ruleset_id = %s and s.board = %s and s.pp > %s and (u.priv & 1) = 1",
        (ruleset_id, board, me["pp"]),
    )
    above_cc = await db.one(
        "select count(*) n from lazer_stats s join users u on u.id = s.user_id "
        "where s.ruleset_id = %s and s.board = %s and s.pp > %s and (u.priv & 1) = 1 and u.country = %s",
        (ruleset_id, board, me["pp"], country),
    )
    return above["n"] + 1, above_cc["n"] + 1


async def statistics(user: dict, ruleset_id: int, board: str = scoring.BOARD_VANILLA) -> dict:
    s = await db.one("select * from lazer_stats where user_id = %s and ruleset_id = %s and board = %s", (user["id"], ruleset_id, board)) or {}
    rank, country_rank = await ranks_for(user["id"], ruleset_id, board, user["country"])
    return {
        "level": level_for(int(s.get("total_score", 0))),
        "global_rank": rank,
        "country_rank": country_rank,
        "pp": float(s.get("pp", 0)),
        "ranked_score": int(s.get("ranked_score", 0)),
        "hit_accuracy": float(s.get("accuracy", 0)) * 100,
        "play_count": int(s.get("play_count", 0)),
        "play_time": int(s.get("play_time", 0)),
        "total_score": int(s.get("total_score", 0)),
        "total_hits": int(s.get("total_hits", 0)),
        "maximum_combo": int(s.get("max_combo", 0)),
        "replays_watched_by_others": 0,
        "is_ranked": rank is not None,
        "grade_counts": {"ss": s.get("grade_ss", 0), "ssh": s.get("grade_ssh", 0), "s": s.get("grade_s", 0),
                         "sh": s.get("grade_sh", 0), "a": s.get("grade_a", 0)},
    }


def compact_user(u: dict) -> dict:
    return {
        "id": u["id"],
        "username": u["name"],
        "country_code": (u.get("country") or "xx").upper(),
        "avatar_url": f"{AVATARS}/{u['id']}",
        "is_supporter": False,
        "is_bot": u["id"] == 1,
        "is_active": True,
        "is_online": False,
        "pm_friends_only": False,
        "profile_colour": None,
        "default_group": "default",
        "last_visit": iso(u.get("latest_activity")),
    }


async def full_user(u: dict, ruleset_id: int | None = None) -> dict:
    rs = u.get("preferred_mode", 0) % 4 if ruleset_id is None else ruleset_id
    out = compact_user(u)
    groups = []
    if u["priv"] & STAFF_BITS:
        groups.append({"id": 4, "identifier": "gmt", "name": "o!ily staff", "short_name": "STAFF", "colour": "#ff3b5c",
                       "has_listing": True, "has_playmodes": False, "is_probationary": False})
    out.update({
        "country": {"code": out["country_code"], "name": out["country_code"]},
        "cover_url": f"{SITE}/assets/bg-1.jpg",
        "cover": {"url": f"{SITE}/assets/bg-1.jpg", "custom_url": None, "id": None},
        "join_date": iso(u.get("creation_time")),
        "playmode": RULESET_SHORT[rs],
        "playstyle": [],
        "title": None,
        "location": None,
        "interests": None,
        "occupation": None,
        "twitter": None,
        "discord": None,
        "website": SITE,
        "post_count": 0,
        "comments_count": 0,
        "follower_count": 0,
        "mapping_follower_count": 0,
        "kudosu": {"total": 0, "available": 0},
        "statistics": await statistics(u, rs),
        "groups": groups,
        "badges": [],
        "monthly_playcounts": [],
        "replays_watched_counts": [],
        "user_achievements": [],
        "rank_history": None,
        "session_verification_method": None,
        "page": {"html": "", "raw": u.get("userpage_content") or ""},
        "profile_order": ["me", "recent_activity", "top_ranks", "historical", "beatmaps", "medals", "kudosu"],
        "scores_best_count": 0,
        "scores_first_count": 0,
        "scores_recent_count": 0,
        "scores_pinned_count": 0,
        "beatmap_playcounts_count": 0,
        "favourite_beatmapset_count": 0,
        "graveyard_beatmapset_count": 0,
        "loved_beatmapset_count": 0,
        "ranked_beatmapset_count": 0,
        "pending_beatmapset_count": 0,
        "guest_beatmapset_count": 0,
        "nominated_beatmapset_count": 0,
        "previous_usernames": [],
        "is_restricted": not bool(u["priv"] & UNRESTRICTED),
    })
    return out


async def find_user(lookup: str, key: str | None) -> dict | None:
    lookup = lookup.lstrip("@")
    if key != "username" and lookup.isdigit():
        u = await db.one("select * from users where id = %s", (int(lookup),))
        if u or key == "id":
            return u
    return await db.one("select * from users where safe_name = %s", (lookup.lower().replace(" ", "_"),))


@app.get("/api/v2/me")
@app.get("/api/v2/me/")
@app.get("/api/v2/me/{mode}")
async def me(mode: str | None = None, user: dict = Depends(current_user)):
    return await full_user(user, ruleset_id_from(mode) if mode else None)


@app.get("/api/v2/users/lookup")
@app.get("/api/v2/users/lookup/")
@app.get("/api/v2/users")
@app.get("/api/v2/users/")
async def users_lookup(request: Request):
    ids = [i for i in request.query_params.getlist("ids[]") if i]
    out = []
    for i in ids[:50]:
        u = await find_user(i, None)
        if u and u["priv"] & UNRESTRICTED:
            out.append(compact_user(u))
    return {"users": out}


@app.get("/api/v2/users/{lookup}")
@app.get("/api/v2/users/{lookup}/{mode}")
async def user(lookup: str, mode: str | None = None, key: str | None = None):
    u = await find_user(lookup, key)
    if not u or not u["priv"] & UNRESTRICTED:
        raise HTTPException(404, "User not found.")
    return await full_user(u, ruleset_id_from(mode) if mode else None)


# ---------- beatmaps ----------
@app.get("/api/v2/beatmaps/lookup")
async def beatmap_lookup(id: int | None = None, checksum: str | None = None, filename: str | None = None):
    m = await maps.lookup(id=id, md5=checksum, filename=filename)
    if not m:
        raise HTTPException(404, "Beatmap not found.")
    return maps.web_beatmap(m)


@app.get("/api/v2/beatmaps")
@app.get("/api/v2/beatmaps/")
async def beatmaps_many(request: Request):
    out = []
    for i in request.query_params.getlist("ids[]")[:50]:
        if i.isdigit():
            m = await maps.lookup(id=int(i))
            if m:
                out.append(maps.web_beatmap(m))
    return {"beatmaps": out}


@app.get("/api/v2/beatmaps/{beatmap_id}")
async def beatmap(beatmap_id: int):
    return await beatmap_lookup(id=beatmap_id)


@app.get("/api/v2/beatmapsets/lookup")
async def beatmapset_lookup(beatmap_id: int):
    m = await maps.lookup(id=beatmap_id)
    if not m:
        raise HTTPException(404, "Beatmap not found.")
    return await beatmapset(int(m["beatmapset_id"]))


@app.get("/api/v2/beatmapsets/{set_id}")
async def beatmapset(set_id: int):
    rows = await maps.set_rows(set_id)
    if not rows:
        raise HTTPException(404, "Beatmap set not found.")
    return maps.web_set(rows)


@app.get("/api/v2/beatmapsets/{set_id}/download")
async def download(set_id: int, noVideo: int = 0):
    return RedirectResponse(f"{DOWNLOAD}/{set_id}{'?noVideo=1' if noVideo else ''}", status_code=302)


@app.get("/api/v2/beatmapsets/search")
@app.get("/api/v2/beatmapsets/search/")
async def beatmapset_search():
    # in-game beatmap browser: not hooked up yet (osu!direct on stable works)
    return {"beatmapsets": [], "cursor_string": None, "total": 0}


# ---------- scores ----------
def score_json(row: dict, user_row: dict | None, position: int | None = None, beatmap: dict | None = None) -> dict:
    mods = row["mods"] if isinstance(row["mods"], list) else json.loads(row["mods"])
    out = {
        "id": row["id"],
        "user_id": row["user_id"],
        "beatmap_id": row["beatmap_id"],
        "ruleset_id": row["ruleset_id"],
        "build_id": None,
        "passed": bool(row["passed"]),
        "total_score": int(row["total_score"]),
        "total_score_without_mods": int(row["total_score"]),
        "accuracy": float(row["accuracy"]),
        "max_combo": int(row["max_combo"]),
        "rank": row["grade"],
        "mods": mods,
        "statistics": row["statistics"] if isinstance(row["statistics"], dict) else json.loads(row["statistics"]),
        "maximum_statistics": row["maximum_statistics"] if isinstance(row["maximum_statistics"], dict) else json.loads(row["maximum_statistics"]),
        "started_at": iso(row.get("started_at")),
        "ended_at": iso(row["ended_at"]),
        "pp": float(row["pp"]) if row["ranked"] and row["pp"] else None,
        "has_replay": False,
        "ranked": bool(row["ranked"]),
        "preserve": True,
        "processed": True,
        "legacy_score_id": None,
        "legacy_total_score": 0,
        "type": "solo_score",
    }
    if position is not None:
        out["position"] = position
    if user_row is not None:
        out["user"] = compact_user(user_row)
    if beatmap is not None:
        out["beatmap"] = maps.web_beatmap(beatmap, with_set=False)
        out["beatmapset"] = maps.web_set([beatmap], with_beatmaps=False)
    return out


def board_from_request_mods(mod_list: list[str]) -> str:
    return scoring.board_for([{"acronym": m} for m in mod_list], 0)


@app.get("/api/v2/beatmaps/{beatmap_id}/scores")
async def beatmap_scores(beatmap_id: int, request: Request, user: dict | None = Depends(optional_user)):
    q = request.query_params
    ruleset_id = ruleset_id_from(q.get("mode"))
    mod_filter = [m.upper() for m in q.getlist("mods[]") if m]
    board = board_from_request_mods(mod_filter) if "RX" in mod_filter or any(m not in scoring.VANILLA_OK | {"RX"} for m in mod_filter) else scoring.BOARD_VANILLA
    # relax players filter by RX in song select; everyone else sees the vanilla board
    if "RX" in mod_filter:
        board = scoring.BOARD_RELAX
    limit = max(1, min(int(q.get("limit") or 50), 100))
    scope = (q.get("type") or "global").lower()

    where = "s.beatmap_id = %s and s.ruleset_id = %s and s.board = %s and s.best = 1 and (u.priv & 1) = 1"
    args: list = [beatmap_id, ruleset_id, board]
    if scope == "country" and user:
        where += " and u.country = %s"
        args.append(user["country"])
    elif scope == "friend" and user:
        where += " and (s.user_id = %s or s.user_id in (select user2 from relationships where user1 = %s and type = 'friend'))"
        args += [user["id"], user["id"]]
    rows = await db.all(
        f"select s.*, u.name, u.country, u.priv, u.latest_activity from lazer_scores s join users u on u.id = s.user_id "
        f"where {where} order by s.total_score desc, s.id asc limit %s",
        tuple(args + [limit]),
    )
    scores = [score_json(r, {"id": r["user_id"], "name": r["name"], "country": r["country"], "latest_activity": r["latest_activity"]}, i + 1)
              for i, r in enumerate(rows)]
    total = await db.one(
        "select count(*) n from lazer_scores s join users u on u.id = s.user_id where s.beatmap_id = %s and s.ruleset_id = %s "
        "and s.board = %s and s.best = 1 and (u.priv & 1) = 1", (beatmap_id, ruleset_id, board))
    user_score = None
    if user:
        mine = await db.one("select * from lazer_scores where beatmap_id = %s and ruleset_id = %s and board = %s and best = 1 and user_id = %s",
                            (beatmap_id, ruleset_id, board, user["id"]))
        if mine:
            better = await db.one(
                "select count(*) n from lazer_scores s join users u on u.id = s.user_id where s.beatmap_id = %s and s.ruleset_id = %s "
                "and s.board = %s and s.best = 1 and (u.priv & 1) = 1 and s.total_score > %s",
                (beatmap_id, ruleset_id, board, mine["total_score"]))
            pos = better["n"] + 1
            user_score = {"position": pos, "score": score_json(mine, user, pos)}
    return {"scores": scores, "user_score": user_score, "score_count": total["n"]}


@app.post("/api/v2/beatmaps/{beatmap_id}/solo/scores")
async def create_score_token(beatmap_id: int, request: Request, user: dict = Depends(current_user)):
    form = await request.form()
    ruleset_id = int(form.get("ruleset_id", 0))
    md5 = str(form.get("beatmap_hash", ""))
    token = await db.execute(
        "insert into lazer_score_tokens (user_id, beatmap_id, beatmap_md5, ruleset_id, created_at) values (%s, %s, %s, %s, %s)",
        (user["id"], beatmap_id, md5, ruleset_id, now()),
    )
    return {"id": token}


async def recalc_stats(user_id: int, ruleset_id: int, board: str) -> None:
    best = await db.all(
        "select pp, accuracy from lazer_scores where user_id = %s and ruleset_id = %s and board = %s and best = 1 and ranked = 1 "
        "order by pp desc limit 1000", (user_id, ruleset_id, board))
    weighted_pp = sum(r["pp"] * 0.95 ** i for i, r in enumerate(best))
    bonus = 416.6667 * (1 - 0.9994 ** len(best)) if best else 0
    acc = 0.0
    if best:
        acc = sum(r["accuracy"] * 0.95 ** i for i, r in enumerate(best)) * (100.0 / (20 * (1 - 0.95 ** len(best)))) / 100
    grades = await db.one(
        "select sum(grade = 'X') ss, sum(grade = 'XH') ssh, sum(grade = 'S') s, sum(grade = 'SH') sh, sum(grade = 'A') a, "
        "coalesce(sum(total_score), 0) ranked_score from lazer_scores where user_id = %s and ruleset_id = %s and board = %s and best = 1 and ranked = 1",
        (user_id, ruleset_id, board))
    await db.execute(
        "update lazer_stats set pp = %s, accuracy = %s, ranked_score = %s, grade_ss = %s, grade_ssh = %s, grade_s = %s, grade_sh = %s, grade_a = %s "
        "where user_id = %s and ruleset_id = %s and board = %s",
        (round(weighted_pp + bonus, 3), min(acc, 1.0), int(grades["ranked_score"] or 0), int(grades["ss"] or 0), int(grades["ssh"] or 0),
         int(grades["s"] or 0), int(grades["sh"] or 0), int(grades["a"] or 0), user_id, ruleset_id, board))


@app.put("/api/v2/beatmaps/{beatmap_id}/solo/scores/{token_id}")
async def submit_score(beatmap_id: int, token_id: int, request: Request, user: dict = Depends(current_user)):
    token = await db.one("select * from lazer_score_tokens where id = %s and user_id = %s", (token_id, user["id"]))
    if not token or token["beatmap_id"] != beatmap_id:
        raise HTTPException(404, "Score token not found.")
    if token["score_id"]:
        row = await db.one("select * from lazer_scores where id = %s", (token["score_id"],))
        return score_json(row, user)
    body = await request.json()
    ruleset_id = token["ruleset_id"]
    mods = body.get("mods") or []
    stats = body.get("statistics") or {}
    passed = bool(body.get("passed"))
    accuracy = float(body.get("accuracy") or 0)
    combo = int(body.get("max_combo") or 0)
    grade = body.get("rank")
    if not isinstance(grade, str) or grade not in ("XH", "X", "SH", "S", "A", "B", "C", "D", "F"):
        grade = scoring.grade(ruleset_id, accuracy, mods, stats, passed)
    board = scoring.board_for(mods, ruleset_id)

    m = await maps.lookup(id=beatmap_id)
    status = m["web_status"] if m else 0
    ranked = passed and status in (1, 2, 4) and (not m or m.get("file_md5") == token["beatmap_md5"])
    pp = 0.0
    if ranked and status in (1, 2):
        osu_file = await maps.osu_file(beatmap_id, token["beatmap_md5"])
        if osu_file:
            async with pp_lock:
                try:
                    pp = await asyncio.to_thread(scoring.calculate_pp, osu_file, ruleset_id, mods, stats, combo)
                except Exception as exc:
                    print("pp failed", beatmap_id, exc, flush=True)

    ended = datetime.now(timezone.utc).replace(tzinfo=None)
    score_id = await db.execute(
        "insert into lazer_scores (user_id, beatmap_id, beatmap_md5, ruleset_id, board, total_score, accuracy, max_combo, grade, pp, mods, "
        "statistics, maximum_statistics, passed, ranked, best, ended_at) values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s)",
        (user["id"], beatmap_id, token["beatmap_md5"], ruleset_id, board, int(body.get("total_score") or 0), accuracy, combo, grade, pp,
         json.dumps(mods), json.dumps(stats), json.dumps(body.get("maximum_statistics") or {}), passed, ranked, ended),
    )
    await db.execute("update lazer_score_tokens set score_id = %s where id = %s", (score_id, token_id))

    # keep one best score per map/board: highest pp on pp maps, highest score otherwise
    if ranked:
        prev = await db.one("select id, pp, total_score from lazer_scores where user_id = %s and beatmap_id = %s and ruleset_id = %s "
                            "and board = %s and best = 1", (user["id"], beatmap_id, ruleset_id, board))
        new_total = int(body.get("total_score") or 0)
        better = prev is None or ((pp, new_total) > (float(prev["pp"]), int(prev["total_score"])) if status in (1, 2)
                                  else new_total > int(prev["total_score"]))
        if better:
            if prev:
                await db.execute("update lazer_scores set best = 0 where id = %s", (prev["id"],))
            await db.execute("update lazer_scores set best = 1 where id = %s", (score_id,))

    hits = sum(int(v) for k, v in stats.items() if k not in ("miss", "large_tick_miss", "small_tick_miss", "ignore_miss", "combo_break"))
    await db.execute(
        "insert into lazer_stats (user_id, ruleset_id, board, play_count, total_score, total_hits, max_combo) values (%s, %s, %s, 1, %s, %s, %s) "
        "on duplicate key update play_count = play_count + 1, total_score = total_score + values(total_score), "
        "total_hits = total_hits + values(total_hits), max_combo = greatest(max_combo, values(max_combo))",
        (user["id"], ruleset_id, board, int(body.get("total_score") or 0), hits, combo),
    )
    if ranked:
        await recalc_stats(user["id"], ruleset_id, board)
    row = await db.one("select * from lazer_scores where id = %s", (score_id,))
    return score_json(row, user, beatmap=m)


@app.get("/api/v2/scores/{score_id}")
@app.get("/api/v2/scores/{mode}/{score_id}")
async def get_score(score_id: int, mode: str | None = None):
    row = await db.one("select s.*, u.name, u.country, u.latest_activity from lazer_scores s join users u on u.id = s.user_id where s.id = %s", (score_id,))
    if not row:
        raise HTTPException(404, "Score not found.")
    m = await maps.lookup(id=row["beatmap_id"])
    return score_json(row, {"id": row["user_id"], "name": row["name"], "country": row["country"], "latest_activity": row["latest_activity"]}, beatmap=m)


@app.get("/api/v2/users/{user_id}/scores/{kind}")
async def user_scores(user_id: int, kind: str, mode: str | None = None, limit: int = 50, offset: int = 0):
    ruleset_id = ruleset_id_from(mode)
    limit = max(1, min(limit, 100))
    if kind == "best":
        rows = await db.all("select * from lazer_scores where user_id = %s and ruleset_id = %s and board = 'vn' and best = 1 and ranked = 1 "
                            "order by pp desc limit %s offset %s", (user_id, ruleset_id, limit, offset))
    elif kind == "recent":
        rows = await db.all("select * from lazer_scores where user_id = %s and ruleset_id = %s order by id desc limit %s offset %s",
                            (user_id, ruleset_id, limit, offset))
    else:
        rows = []
    u = await db.one("select * from users where id = %s", (user_id,))
    out = []
    for r in rows:
        m = await maps.lookup(id=r["beatmap_id"])
        out.append(score_json(r, u, beatmap=m))
    return out


# ---------- rankings ----------
@app.get("/api/v2/rankings/{mode}/performance")
async def rankings(mode: str, request: Request):
    ruleset_id = ruleset_id_from(mode)
    page = max(1, int(request.query_params.get("cursor[page]") or request.query_params.get("page") or 1))
    country = request.query_params.get("country")
    where = "s.ruleset_id = %s and s.board = 'vn' and s.pp > 0 and (u.priv & 1) = 1"
    args: list = [ruleset_id]
    if country:
        where += " and u.country = %s"
        args.append(country.lower())
    rows = await db.all(f"select s.*, u.* from lazer_stats s join users u on u.id = s.user_id where {where} order by s.pp desc limit 50 offset %s",
                        tuple(args + [(page - 1) * 50]))
    total = await db.one(f"select count(*) n from lazer_stats s join users u on u.id = s.user_id where {where}", tuple(args))
    ranking = []
    for i, r in enumerate(rows):
        st = await statistics(r, ruleset_id)
        st["global_rank"] = (page - 1) * 50 + i + 1
        st["user"] = compact_user(r)
        ranking.append(st)
    return {"ranking": ranking, "cursor": {"page": page + 1} if len(rows) == 50 else None, "total": total["n"]}


# ---------- things lazer asks for that o!ily doesn't have (yet) ----------
@app.get("/api/v2/friends")
async def friends(user: dict = Depends(current_user)):
    rows = await db.all("select u.* from relationships r join users u on u.id = r.user2 where r.user1 = %s and r.type = 'friend'", (user["id"],))
    return [{"target_id": r["id"], "relation_type": "friend", "mutual": False, "target": compact_user(r)} for r in rows]


@app.get("/api/v2/blocks")
async def blocks(user: dict = Depends(current_user)):
    return []


@app.get("/api/v2/notifications")
async def notifications(user: dict = Depends(current_user)):
    return {"has_more": False, "notifications": [], "unread_count": 0, "notification_endpoint": ""}


@app.get("/api/v2/chat/channels")
async def chat_channels(user: dict = Depends(current_user)):
    return []


@app.get("/api/v2/chat/updates")
async def chat_updates(user: dict = Depends(current_user)):
    return {"presence": [], "silences": [], "messages": []}


@app.post("/api/v2/chat/ack")
async def chat_ack(user: dict = Depends(current_user)):
    return {"silences": []}


@app.get("/api/v2/seasonal-backgrounds")
async def seasonal_backgrounds():
    return {"ends_at": iso(now() + 365 * 86400),
            "backgrounds": [{"url": f"{SITE}/assets/bg-1.jpg", "user": None}, {"url": f"{SITE}/assets/bg-2.jpg", "user": None}]}


@app.get("/api/v2/news")
async def news():
    return {"news_posts": [], "news_sidebar": {"current_year": datetime.now().year, "years": [], "news_posts": []}, "search": {"limit": 12, "sort": "published_desc"}, "cursor": None}


@app.get("/api/v2/me/beatmapset-favourites")
async def favourites(user: dict = Depends(current_user)):
    return {"beatmapset_ids": []}


@app.get("/api/v2/tags")
async def tags():
    return {"tags": []}


@app.get("/api/v2/spotlights")
async def spotlights():
    return {"spotlights": []}


@app.get("/api/v2/users/{user_id}/beatmapsets/{kind}")
async def user_beatmapsets(user_id: int, kind: str):
    return []


@app.get("/api/v2/users/{user_id}/recent_activity")
async def recent_activity(user_id: int):
    return []


# ---------- for the o!ily website ----------
@app.get("/web/leaderboard")
async def web_leaderboard(board: str = "vn", ruleset: int = 0, page: int = 1, country: str | None = None, sort: str = "pp"):
    if board not in (scoring.BOARD_VANILLA, scoring.BOARD_RELAX, scoring.BOARD_MODS):
        raise HTTPException(400, "Unknown board.")
    order = {"pp": "s.pp", "rscore": "s.ranked_score", "acc": "s.accuracy", "plays": "s.play_count", "tscore": "s.total_score"}.get(sort, "s.pp")
    where = "s.ruleset_id = %s and s.board = %s and (u.priv & 1) = 1 and s.play_count > 0"
    args: list = [ruleset, board]
    if country:
        where += " and u.country = %s"
        args.append(country.lower())
    page = max(1, page)
    rows = await db.all(
        f"select s.*, u.name, u.country from lazer_stats s join users u on u.id = s.user_id where {where} order by {order} desc limit 50 offset %s",
        tuple(args + [(page - 1) * 50]))
    return {"status": "success", "data": [{
        "player_id": r["user_id"], "name": r["name"], "country": r["country"], "pp": r["pp"], "acc": r["accuracy"] * 100,
        "plays": r["play_count"], "rscore": r["ranked_score"], "tscore": r["total_score"], "max_combo": r["max_combo"],
        "xh_count": r["grade_ssh"], "x_count": r["grade_ss"], "sh_count": r["grade_sh"], "s_count": r["grade_s"], "a_count": r["grade_a"],
    } for r in rows]}


@app.get("/web/players/{user_id}")
async def web_player(user_id: int, board: str = "vn", ruleset: int = 0):
    u = await db.one("select * from users where id = %s", (user_id,))
    if not u or not u["priv"] & UNRESTRICTED:
        raise HTTPException(404, "Player not found.")
    st = await statistics(u, ruleset, board)
    best = await db.all("select * from lazer_scores where user_id = %s and ruleset_id = %s and board = %s and best = 1 and ranked = 1 "
                        "order by pp desc, total_score desc limit 50", (user_id, ruleset, board))
    recent = await db.all("select * from lazer_scores where user_id = %s and ruleset_id = %s and board = %s order by id desc limit 10",
                          (user_id, ruleset, board))
    async def with_map(rows):
        out = []
        for r in rows:
            m = await maps.lookup(id=r["beatmap_id"])
            out.append({**score_json(r, None), "beatmap": maps.web_beatmap(m) if m else None})
        return out
    return {"status": "success", "data": {"stats": st, "best": await with_map(best), "recent": await with_map(recent)}}


@app.get("/web/maps/{beatmap_id}/scores")
async def web_map_scores(beatmap_id: int, board: str = "vn", ruleset: int = 0):
    if board not in (scoring.BOARD_VANILLA, scoring.BOARD_RELAX, scoring.BOARD_MODS):
        raise HTTPException(400, "Unknown board.")
    rows = await db.all(
        "select s.*, u.name, u.country from lazer_scores s join users u on u.id = s.user_id "
        "where s.beatmap_id = %s and s.ruleset_id = %s and s.board = %s and s.best = 1 and (u.priv & 1) = 1 "
        "order by s.pp desc, s.total_score desc limit 50", (beatmap_id, ruleset, board))
    return {"status": "success", "data": [
        {**score_json(r, None), "player": {"id": r["user_id"], "name": r["name"], "country": r["country"]}} for r in rows]}


@app.get("/health")
async def health():
    return {"ok": True}
