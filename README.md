# o!ily website

The website for **o!ily**, a relax-first osu! private server, at https://oily.ilystudios.net.

Plain HTML, CSS and JavaScript with no build step. It reads the server's
[bancho.py](https://github.com/osuAkatsuki/bancho.py) v2 API through `/api/v2` on the same domain.

## Pages

- Home with live server stats
- Leaderboards: Regular (stable and lazer together, lazer fun mods count as normal mods), Stable Relax, Lazer Relax
- Player profiles (`/u/<id or name>`) and beatmap pages (`/b/<id>`)
- Sign up, log in, how to connect, about and rules

## Server

`server/oily-update.sh` sets up or updates everything on the VPS (run as root):

    git -C /opt/oily-web pull -q && bash /opt/oily-web/server/oily-update.sh

(First time: `git clone https://github.com/iloily/oily-web /opt/oily-web`.)

It pulls this repo to `/opt/oily-web`, writes the Caddy config (site, `/api/*` to bancho.py,
`/extras/*` to the extras service, avatars on `a.`), applies the o!ily in-game branding,
starts `extras/`, and installs nightly backups to `/opt/backups`.

## extras/

A small FastAPI service next to bancho.py for profile banners and colours,
o!ily Ranked tracking and map requests. bancho.py itself stays unmodified.

## Credits

Runs on bancho.py (MIT). Not affiliated with ppy Pty Ltd. osu! is a trademark of ppy Pty Ltd.
