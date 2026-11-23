#!/usr/bin/env python3
"""Find maps that Akatsuki ranked/loved itself (not ranked on official osu!) and rank them on o!ily.

Run on the VPS as root. Only uses Python's standard library.

  python3 /opt/oily-web/server/akatsuki-import.py scan
      Step 1. Lists every map whose status Akatsuki's staff set by hand ("frozen" maps),
      keeps the ranked/approved/loved ones, then asks the osu.direct mirror for each
      map's official status and drops the ones official osu! already ranks.
      Slow on purpose (about 1 request a second) so we stay polite to both sites.
      Can be stopped and started again: it carries on where it stopped.
      Writes /opt/oily-akatsuki/report.txt (count + sample list) and plan.json.

  python3 /opt/oily-web/server/akatsuki-import.py apply [--loved-as loved|ranked]
      Step 2, only after ilo has looked at the report. Writes the maps into o!ily's
      database as o!ily Ranked/Loved (frozen, like the website's Rank button),
      then restarts bancho so it picks them up. Maps o!ily staff already set by hand
      are never changed.
"""
from __future__ import annotations

import http.client
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

WORK = Path("/opt/oily-akatsuki")
AKATSUKI = "https://akatsuki.gg/api/v1/beatmaps"
MIRROR = "https://osu.direct/api/get_beatmaps"
MIRROR_SEARCH = "https://osu.direct/api/v2/search"
UA = "oily-akatsuki-import/1.0 (o!ily private server; one-off map list import)"
AKATSUKI_DELAY = 1.0  # seconds between requests
MIRROR_DELAY = 1.0
BANCHO = "/opt/bancho"
COMPOSE = ["docker", "compose", "--env-file", ".env", "-f", "docker-compose.yml", "-f", "oily.override.yml"]

# Akatsuki (ripple) and bancho.py use the same numbers: 2 ranked, 3 approved, 5 loved
AKATSUKI_WANTED = {2: "ranked", 3: "approved", 5: "loved"}
# official osu! api v1 "approved" -> bancho.py status
OSUAPI_TO_BANCHO = {-2: 0, -1: 0, 0: 0, 1: 2, 2: 3, 3: 4, 4: 5}
IGNORED_FILENAME_CHARS = dict.fromkeys(map(ord, r':\/*<>?"|'), None)


def get_json(url: str, params: dict, tries: int = 8):
    full = url + "?" + urllib.parse.urlencode(params, doseq=True)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            wait = 60 if e.code == 429 else 5 * (attempt + 1)
            print(f"  {e.code} from {url}, waiting {wait}s", flush=True)
            time.sleep(wait)
        except (urllib.error.URLError, http.client.HTTPException, OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"  {type(e).__name__} from {url}, retrying", flush=True)
            time.sleep(5 * (attempt + 1))
    raise SystemExit(f"!! {url} keeps failing, stopping. Run the same command again later to carry on.")


def load(name: str, default):
    p = WORK / name
    return json.loads(p.read_text()) if p.exists() else default


def save(name: str, data) -> None:
    tmp = WORK / (name + ".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(WORK / name)


def scan_akatsuki() -> dict:
    """Every frozen Akatsuki map with a ranked/approved/loved status, keyed by beatmap id."""
    state = load("akatsuki.json", {"next_page": 1, "done": False, "maps": {}})
    if state["done"]:
        print(f"Akatsuki list already downloaded ({len(state['maps'])} maps).")
        return state["maps"]
    print("Downloading Akatsuki's hand-ranked map list (50 per page)...", flush=True)
    page = state["next_page"]
    while True:
        data = get_json(AKATSUKI, {"ranked_status_frozen": 1, "l": 50, "p": page, "sort": "beatmap_id,asc"})
        maps = (data or {}).get("beatmaps") or []
        for m in maps:
            if m.get("ranked") in AKATSUKI_WANTED:
                state["maps"][str(m["beatmap_id"])] = {
                    "set_id": m["beatmapset_id"],
                    "md5": m["beatmap_md5"],
                    "name": m["song_name"],
                    "akatsuki": m["ranked"],
                }
        state["next_page"] = page + 1
        if len(maps) < 50:
            state["done"] = True
        save("akatsuki.json", state)
        if page % 20 == 0 or state["done"]:
            print(f"  page {page}: {len(state['maps'])} ranked/loved maps so far", flush=True)
        if state["done"]:
            return state["maps"]
        page += 1
        time.sleep(AKATSUKI_DELAY)


def official_ranked_sets() -> set[str]:
    """Set ids that official osu! ranked or approved, from the mirror's search (100 sets a page).
    Most of Akatsuki's hand-set maps are in here, so we don't have to check those one by one."""
    state = load("official.json", {"done": [], "ids": []})
    ids = set(state["ids"])
    for status, name in ((1, "ranked"), (2, "approved")):
        if status in state["done"]:
            continue
        print(f"Downloading official osu!'s {name} list (100 sets a page)...", flush=True)
        offset, empty_in_row = 0, 0
        while True:
            page = get_json(MIRROR_SEARCH, {"status": status, "amount": 100, "offset": offset})
            page = page if isinstance(page, list) else []
            new = 0
            for s in page:
                st = s.get("status")
                if st in ("ranked", "approved") or s.get("ranked") in (1, 2):
                    if str(s["id"]) not in ids:
                        ids.add(str(s["id"]))
                        new += 1
            empty_in_row = empty_in_row + 1 if new == 0 else 0
            offset += 100
            if (offset // 100) % 50 == 0:
                print(f"  {offset} looked at, {len(ids)} official ranked/approved sets so far", flush=True)
                state["ids"] = sorted(ids)
                save("official.json", state)
            if len(page) < 100 or empty_in_row >= 3:
                break
            time.sleep(MIRROR_DELAY)
        state["done"].append(status)
        state["ids"] = sorted(ids)
        save("official.json", state)
        print(f"  official ranked/approved sets: {len(ids)}", flush=True)
    return ids


def scan_mirror(ak_maps: dict) -> dict:
    """Official data for every set that has an Akatsuki map, keyed by set id (list of v1 diffs)."""
    sets = load("mirror.json", {})
    official = official_ranked_sets()
    todo = sorted({str(m["set_id"]) for m in ak_maps.values()} - set(sets) - official)
    print(f"Skipping {len({str(m['set_id']) for m in ak_maps.values()} & official)} sets that official osu! already ranks.", flush=True)
    print(f"Checking official status on osu.direct: {len(todo)} sets to go ({len(sets)} done).", flush=True)
    for i, set_id in enumerate(todo, 1):
        data = get_json(MIRROR, {"s": set_id})
        sets[set_id] = data if isinstance(data, list) else []
        if i % 50 == 0 or i == len(todo):
            save("mirror.json", sets)
            print(f"  {i}/{len(todo)} sets checked", flush=True)
        time.sleep(MIRROR_DELAY)
    save("mirror.json", sets)
    return sets


def oily_frozen_ids() -> set[int]:
    out = mysql("select id from maps where frozen = 1", capture=True)
    return {int(x) for x in out.split()}


def scan() -> None:
    WORK.mkdir(parents=True, exist_ok=True)
    ak_maps = scan_akatsuki()
    sets = scan_mirror(ak_maps)
    already_set_on_oily = oily_frozen_ids()

    plan, skipped = [], {"ranked on official osu!": 0, "loved on both": 0, "gone from the mirror": 0, "already set by o!ily staff": 0}
    official_sets = set(load("official.json", {"ids": []})["ids"])
    for bid, m in ak_maps.items():
        if str(m["set_id"]) in official_sets:
            skipped["ranked on official osu!"] += 1
            continue
        official = {str(d["beatmap_id"]): d for d in sets.get(str(m["set_id"]), [])}.get(bid)
        if official is None:
            skipped["gone from the mirror"] += 1
            continue
        off = int(official["approved"])
        if off in (1, 2):
            skipped["ranked on official osu!"] += 1
            continue
        if m["akatsuki"] == 5 and off == 4:
            skipped["loved on both"] += 1
            continue
        if int(bid) in already_set_on_oily:
            skipped["already set by o!ily staff"] += 1
            continue
        plan.append({
            "id": int(bid),
            "set_id": int(m["set_id"]),
            "akatsuki": AKATSUKI_WANTED[m["akatsuki"]],
            "official": {-2: "graveyard", -1: "wip", 0: "pending", 3: "qualified", 4: "loved"}.get(off, str(off)),
            "mode": int(official["mode"]),
            "name": f"{official['artist']} - {official['title']} [{official['version']}] ({official['creator']})",
            "stars": round(float(official.get("difficultyrating") or 0), 2),
        })
    plan.sort(key=lambda p: p["id"])
    save("plan.json", plan)

    modes = ["osu!", "taiko", "catch", "mania"]
    count = lambda key, val: sum(1 for p in plan if p[key] == val)
    lines = [
        f"Akatsuki hand-set maps (ranked/approved/loved): {len(ak_maps)}",
        f"Would be imported to o!ily: {len(plan)} difficulties in {len({p['set_id'] for p in plan})} sets",
        f"  Akatsuki ranked: {count('akatsuki', 'ranked')}, approved: {count('akatsuki', 'approved')}, loved: {count('akatsuki', 'loved')}",
        "  by mode: " + ", ".join(f"{name} {count('mode', i)}" for i, name in enumerate(modes)),
        "  official status now: " + ", ".join(f"{s} {count('official', s)}" for s in ("graveyard", "wip", "pending", "qualified", "loved")),
        "Skipped: " + ", ".join(f"{k} {v}" for k, v in skipped.items()),
        "",
        "Sample (every Nth map):",
    ]
    step = max(1, len(plan) // 25)
    for p in plan[::step][:25]:
        lines.append(f"  b/{p['id']}  {p['akatsuki']:<8} (official: {p['official']:<9}) {modes[p['mode']]:<6} {p['stars']:>5}*  {p['name']}")
    report = "\n".join(lines)
    (WORK / "report.txt").write_text(report + "\n")
    print("\n" + report + "\n\nSaved to /opt/oily-akatsuki/report.txt", flush=True)


# ---------- apply ----------
def mysql(sql: str, capture: bool = False) -> str:
    cmd = COMPOSE + ["exec", "-T", "mysql", "sh", "-c",
                     'MYSQL_PWD="$MYSQL_PASSWORD" mysql --default-character-set=utf8 -N -B -u"$MYSQL_USER" "$MYSQL_DATABASE"']
    r = subprocess.run(cmd, cwd=BANCHO, input=sql.encode("utf-8"), capture_output=True)
    if r.returncode != 0:
        raise SystemExit("!! database error: " + r.stderr.decode(errors="replace")[-2000:])
    return r.stdout.decode("utf-8", errors="replace")


def lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, (int, float)):
        return repr(v)
    s = "".join(ch for ch in str(v) if ord(ch) <= 0xFFFF)  # the maps table is 3-byte utf8: drop emoji
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


def map_row(d: dict, status: int, frozen: int) -> str:
    f = lambda k: float(d.get(k) or 0)
    filename = "{artist} - {title} ({creator}) [{version}].osu".format(**d).translate(IGNORED_FILENAME_CHARS)
    vals = [
        "'osu!'", int(d["beatmap_id"]), int(d["beatmapset_id"]), status, lit(d["file_md5"]),
        lit(d["artist"][:128]), lit(d["title"][:128]), lit(d["version"][:128]), lit(d["creator"][:19]),
        lit(filename[:256]), lit(d["last_update"]), int(d.get("total_length") or 0), int(d.get("max_combo") or 0),
        frozen, int(d.get("mode") or 0), round(f("bpm"), 2), round(f("diff_size"), 2), round(f("diff_approach"), 2),
        round(f("diff_overall"), 2), round(f("diff_drain"), 2), round(f("difficultyrating"), 3),
    ]
    return "(" + ", ".join(str(v) for v in vals) + ")"


COLS = "(server, id, set_id, status, md5, artist, title, version, creator, filename, last_update, total_length, max_combo, frozen, mode, bpm, cs, ar, od, hp, diff)"


def apply(loved_as: str) -> None:
    plan = load("plan.json", None)
    if plan is None:
        raise SystemExit("Run the scan step first.")
    sets = load("mirror.json", {})
    target = {"ranked": 2, "approved": 2, "loved": 5 if loved_as == "loved" else 2}
    wanted = {p["id"]: target[p["akatsuki"]] for p in plan}
    set_ids = sorted({p["set_id"] for p in plan})
    print(f"Writing {len(wanted)} maps ({len(set_ids)} sets) to o!ily; Akatsuki-loved maps become {loved_as}.", flush=True)

    for start in range(0, len(set_ids), 200):
        chunk = set_ids[start:start + 200]
        imported, others = [], []
        for sid in chunk:
            for d in sets.get(str(sid), []):
                bid = int(d["beatmap_id"])
                if bid in wanted:
                    imported.append(map_row(d, wanted[bid], 1))
                else:
                    others.append(map_row(d, OSUAPI_TO_BANCHO.get(int(d["approved"]), 0), 0))
        sql = ["SET NAMES utf8;", "START TRANSACTION;",
               "INSERT IGNORE INTO mapsets (server, id, last_osuapi_check) VALUES "
               + ", ".join(f"('osu!', {sid}, NOW())" for sid in chunk) + ";"]
        if others:  # the set's other difficulties, with their official status, only if o!ily doesn't have them yet
            sql.append(f"INSERT IGNORE INTO maps {COLS} VALUES " + ", ".join(others) + ";")
        if imported:  # never overwrite a map o!ily staff already set by hand (frozen = 1)
            sql.append(f"INSERT INTO maps {COLS} VALUES " + ", ".join(imported)
                       + " ON DUPLICATE KEY UPDATE status = IF(frozen = 1, status, VALUES(status)), frozen = 1;")
        sql.append("COMMIT;")
        mysql("\n".join(sql))
        print(f"  {min(start + 200, len(set_ids))}/{len(set_ids)} sets written", flush=True)

    ranked = mysql("select count(*) from maps where frozen = 1 and status = 2", capture=True).strip()
    loved = mysql("select count(*) from maps where frozen = 1 and status = 5", capture=True).strip()
    print(f"o!ily now has {ranked} o!ily Ranked and {loved} o!ily Loved difficulties.", flush=True)
    print("Restarting bancho so it forgets the old statuses (players reconnect by themselves)...", flush=True)
    subprocess.run(COMPOSE + ["restart", "bancho"], cwd=BANCHO, check=True, stdin=subprocess.DEVNULL)
    print("Done. The o!ily Ranked page refreshes within 5 minutes.", flush=True)


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("Run this as root on the VPS.")
    args = sys.argv[1:]
    if args[:1] == ["scan"]:
        scan()
    elif args[:1] == ["apply"]:
        loved_as = "loved"
        if "--loved-as" in args:
            loved_as = args[args.index("--loved-as") + 1]
        if loved_as not in ("loved", "ranked"):
            raise SystemExit("--loved-as must be loved or ranked")
        apply(loved_as)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
