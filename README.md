# o!ily website

The website for **o!ily**, a relax-first osu! private server, at https://oily.ilystudios.net.

Plain HTML, CSS and JavaScript with no build step. It reads the server's
[bancho.py](https://github.com/osuAkatsuki/bancho.py) v2 API through `/api/v2` on the same domain.

## Pages

- Home with live server stats
- Leaderboards: Stable, Stable Relax, and Lazer, Lazer Relax, Lazer Mods (coming soon)
- Player profiles (`/u/<id or name>`) and beatmap pages (`/b/<id>`)
- Sign up, log in, how to connect, about and rules

## Deploying

On the server, the site is cloned to `/opt/oily-web` and served by Caddy
(`try_files {path} /index.html`), with `/api/*` proxied to bancho.py.
To update: `cd /opt/oily-web && git pull`. No restart needed.

## Credits

Runs on bancho.py (MIT). Not affiliated with ppy Pty Ltd. osu! is a trademark of ppy Pty Ltd.
