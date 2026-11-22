"""o!ily extras: small add-on service next to bancho.py.

Stores what bancho.py doesn't: profile banners and colours, when maps were
ranked by the o!ily team, and map requests. Sign-in is checked by asking
bancho.py who owns the browser's bancho_session cookie, so bancho.py itself
stays unmodified.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

BANCHO = os.environ.get("BANCHO_URL", "http://bancho:10000")
API_HOST = os.environ["API_HOST"]  # e.g. api.oily.ilystudios.net
DATA = Path(os.environ.get("DATA_DIR", "/data"))
BANNERS = DATA / "banners"
MAX_BANNER = 4 * 1024 * 1024
NOMINATOR = 1 << 11
STAFF = (1 << 11) | (1 << 12) | (1 << 13) | (1 << 14)
COLOUR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")

BANNERS.mkdir(parents=True, exist_ok=True)
db = sqlite3.connect(DATA / "extras.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
    create table if not exists profiles (
        user_id integer primary key,
        colour text,
        banner text,
        updated integer
    );
    create table if not exists ranked (
        map_id integer primary key,
        set_id integer not null,
        status integer not null,
        first_seen integer not null,
        info text
    );
    create table if not exists requests (
        id integer primary key autoincrement,
        map_id integer not null,
        set_id integer,
        title text,
        user_id integer not null,
        user_name text not null,
        note text,
        created integer not null
    );
    """
)
db.commit()
client = httpx.AsyncClient(base_url=BANCHO, headers={"Host": API_HOST}, timeout=10)


async def bancho(path: str, **params) -> dict | list | None:
    r = await client.get(path, params=params)
    if r.status_code != 200:
        return None
    return r.json().get("data")


async def current_user(request: Request) -> dict:
    token = request.cookies.get("bancho_session")
    if not token:
        raise HTTPException(401, "Please log in first.")
    r = await client.get("/v2/sessions/current", cookies={"bancho_session": token})
    if r.status_code != 200:
        raise HTTPException(401, "Please log in again.")
    return r.json()["data"]


def ok(data=None) -> JSONResponse:
    return JSONResponse({"status": "success", "data": data})


def image_type(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    return None


# ---------- ranked map tracking ----------
async def track_ranked_forever() -> None:
    """Every 5 minutes, note maps the team ranked/loved (bancho marks them frozen)."""
    while True:
        try:
            now = int(time.time())
            for status in (2, 5):
                page = 1
                while True:
                    maps = await bancho("/v2/maps", frozen="true", status=status, page=page, page_size=100)
                    if not maps:
                        break
                    for m in maps:
                        info = json.dumps({k: m.get(k) for k in ("artist", "title", "version", "creator", "mode", "diff", "bpm", "total_length", "plays")})
                        db.execute(
                            "insert into ranked (map_id, set_id, status, first_seen, info) values (?, ?, ?, ?, ?) "
                            "on conflict(map_id) do update set status = excluded.status, info = excluded.info",
                            (m["id"], m["set_id"], status, now, info),
                        )
                    if len(maps) < 100:
                        break
                    page += 1
            db.commit()
        except Exception as exc:  # keep tracking even if bancho is restarting
            print("ranked tracking failed:", exc, flush=True)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_: FastAPI):
    task = asyncio.create_task(track_ranked_forever())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"status": "error", "error": exc.detail}, status_code=exc.status_code)


# ---------- profiles ----------
@app.get("/profiles/{user_id}")
async def get_profile(user_id: int):
    row = db.execute("select colour, banner, updated from profiles where user_id = ?", (user_id,)).fetchone()
    if not row:
        return ok({"colour": None, "banner_url": None})
    banner = f"/extras/banners/{row['banner']}?v={row['updated']}" if row["banner"] else None
    return ok({"colour": row["colour"], "banner_url": banner})


def save_profile(user_id: int, **fields) -> None:
    db.execute("insert or ignore into profiles (user_id) values (?)", (user_id,))
    for key, value in fields.items():
        db.execute(f"update profiles set {key} = ?, updated = ? where user_id = ?", (value, int(time.time()), user_id))
    db.commit()


@app.put("/profiles/me")
async def update_profile(request: Request):
    user = await current_user(request)
    body = await request.json()
    colour = body.get("colour")
    if colour is not None and not COLOUR_RE.match(str(colour)):
        raise HTTPException(400, "Colour must look like #ff3b5c.")
    save_profile(user["id"], colour=colour)
    return await get_profile(user["id"])


@app.put("/profiles/me/banner")
async def upload_banner(request: Request, banner_file: UploadFile = File(...)):
    user = await current_user(request)
    data = await banner_file.read(MAX_BANNER + 1)
    if len(data) > MAX_BANNER:
        raise HTTPException(400, "Banner is too large (max 4 MB).")
    ext = image_type(data)
    if not ext:
        raise HTTPException(400, "Banners must be PNG, JPG, WEBP or GIF images.")
    for old in BANNERS.glob(f"{user['id']}.*"):
        old.unlink(missing_ok=True)
    (BANNERS / f"{user['id']}.{ext}").write_bytes(data)
    save_profile(user["id"], banner=f"{user['id']}.{ext}")
    return await get_profile(user["id"])


@app.delete("/profiles/me/banner")
async def delete_banner(request: Request):
    user = await current_user(request)
    for old in BANNERS.glob(f"{user['id']}.*"):
        old.unlink(missing_ok=True)
    save_profile(user["id"], banner=None)
    return await get_profile(user["id"])


@app.get("/banners/{name}")
async def banner(name: str):
    if not re.fullmatch(r"\d+\.(jpg|png|webp|gif)", name) or not (BANNERS / name).exists():
        raise HTTPException(404, "No banner.")
    return FileResponse(BANNERS / name, headers={"Cache-Control": "public, max-age=86400"})


# ---------- o!ily ranked ----------
@app.get("/ranked")
async def ranked(page: int = 1, page_size: int = 50):
    page_size = max(1, min(page_size, 100))
    rows = db.execute(
        "select map_id, set_id, status, first_seen, info from ranked order by first_seen desc, map_id desc limit ? offset ?",
        (page_size, (max(page, 1) - 1) * page_size),
    ).fetchall()
    return ok([{**json.loads(r["info"] or "{}"), "map_id": r["map_id"], "set_id": r["set_id"], "status": r["status"], "ranked_at": r["first_seen"]} for r in rows])


@app.get("/requests")
async def list_requests():
    rows = db.execute("select * from requests order by created desc limit 100").fetchall()
    return ok([dict(r) for r in rows])


@app.post("/requests")
async def add_request(request: Request):
    user = await current_user(request)
    body = await request.json()
    link = str(body.get("map", "")).strip()
    note = str(body.get("note", "")).strip()[:300]
    m = re.search(r"(?:#\w+/|/b/|/beatmaps/)(\d+)", link) or re.fullmatch(r"(\d+)", link)
    if not m:
        raise HTTPException(400, "Paste a beatmap link (with the difficulty) or a beatmap id.")
    map_id = int(m.group(1))
    if db.execute("select 1 from requests where map_id = ?", (map_id,)).fetchone():
        raise HTTPException(400, "That map has already been requested.")
    if db.execute("select count(*) from requests where user_id = ? and created > ?", (user["id"], int(time.time()) - 86400)).fetchone()[0] >= 5:
        raise HTTPException(429, "You can request up to 5 maps a day.")
    info = await bancho(f"/v2/maps/{map_id}")
    if not info:
        raise HTTPException(404, "Couldn't find that map. Check the link has the difficulty in it.")
    if info["status"] in (2, 3, 5):
        raise HTTPException(400, "That map is already ranked or loved.")
    title = f"{info['artist']} - {info['title']} [{info['version']}]"
    db.execute(
        "insert into requests (map_id, set_id, title, user_id, user_name, note, created) values (?, ?, ?, ?, ?, ?, ?)",
        (map_id, info["set_id"], title, user["id"], user["name"], note, int(time.time())),
    )
    db.commit()
    return ok({"map_id": map_id, "title": title})


@app.delete("/requests/{request_id}")
async def remove_request(request_id: int, request: Request):
    user = await current_user(request)
    row = db.execute("select user_id from requests where id = ?", (request_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Request not found.")
    if row["user_id"] != user["id"] and not user["priv"] & STAFF:
        raise HTTPException(403, "Only staff can remove other people's requests.")
    db.execute("delete from requests where id = ?", (request_id,))
    db.commit()
    return ok()
