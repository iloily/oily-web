#!/usr/bin/env bash
# o!ily server update: website, extras (banners/colours/o!ily Ranked), in-game branding, nightly backups.
# Run on the VPS as root. Safe to run again any time to update everything.
set -Eeuo pipefail
trap 'echo "!! FAILED: $BASH_COMMAND" >&2' ERR
main() {
D="oily.ilystudios.net"

echo "== 1/6 Website =="
if [ -d /opt/oily-web/.git ]; then git -C /opt/oily-web pull -q; else git clone -q --depth 1 https://github.com/iloily/oily-web.git /opt/oily-web; fi
git -C /opt/oily-web log --oneline -1

cd /opt/bancho
set_env() { if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }

echo "== 2/6 In-game branding =="
set_env MENU_ICON_URL "https://$D/assets/menu-icon.png"
set_env MENU_ONCLICK_URL "https://$D"
set_env SEASONAL_BGS "https://$D/assets/bg-1.jpg,https://$D/assets/bg-2.jpg"
python3 - "$D" <<'PY'
import sys
from pathlib import Path
d = sys.argv[1]
p = Path("/opt/bancho/app/api/domains/cho.py")
s = p.read_text()
swaps = [
    ('f"Welcome to {BASE_DOMAIN}.",', '"Welcome to o!ily, the relax-first osu! server!",'),
    ('f"We have a public (Discord)[{app.settings.DISCORD_INVITE}]!",', f'"Your profile, leaderboards and o!ily Ranked maps: (website)[https://{d}]",'),
    ('f"Welcome back to {BASE_DOMAIN}!\\nRunning bancho.py v{app.settings.VERSION}.",', 'f"Welcome back to o!ily!\\nRunning bancho.py v{app.settings.VERSION}.",'),
]
for old, new in swaps:
    if old in s:
        s = s.replace(old, new)
p.write_text(s)
print("welcome messages:", "o!ily" if "Welcome to o!ily" in s else "NOT CHANGED (check cho.py)")
PY

echo "== 3/6 Web server config =="
{
  printf 'c.%s, ce.%s, c4.%s, c5.%s, c6.%s, b.%s, api.%s {\n' "$D" "$D" "$D" "$D" "$D" "$D" "$D"
  printf '\treverse_proxy bancho:10000 {\n\t\theader_up X-Real-IP {remote_host}\n\t}\n}\n\n'
  # osu.: the game's "View profile" / beatmap links open here, send those to the website
  printf 'osu.%s {\n\t@profile path_regexp prof ^/(u|users)/([^/]+)/?$\n\tredir @profile https://%s/u/{re.prof.2} 302\n\t@map path_regexp map ^/(b|beatmaps)/([0-9]+)/?$\n\tredir @map https://%s/b/{re.map.2} 302\n\t@home path /\n\tredir @home https://%s/ 302\n\treverse_proxy bancho:10000 {\n\t\theader_up X-Real-IP {remote_host}\n\t}\n}\n\n' "$D" "$D" "$D" "$D"
  # assets.: medal pictures shown after a score (falls back to osu!'s own copy if missing)
  printf 'assets.%s {\n\troot * /srv/bancho-data/assets\n\t@missing not file\n\tredir @missing https://assets.ppy.sh{uri} 302\n\tfile_server\n}\n\n' "$D"
  printf 'a.%s {\n\troot * /srv/bancho-data/avatars\n\ttry_files {path} {path}.png {path}.jpg {path}.jpeg {path}.gif /default.jpg\n\theader Cache-Control "no-cache"\n\tfile_server\n}\n\n' "$D"
  printf '%s {\n\tencode gzip\n\thandle_path /api/* {\n\t\treverse_proxy bancho:10000 {\n\t\t\theader_up Host api.%s\n\t\t\theader_up X-Real-IP {remote_host}\n\t\t}\n\t}\n\thandle_path /extras/* {\n\t\treverse_proxy extras:8000 {\n\t\t\theader_up X-Real-IP {remote_host}\n\t\t}\n\t}\n\thandle {\n\t\troot * /srv/oily-web\n\t\ttry_files {path} /index.html\n\t\tfile_server\n\t}\n}\n' "$D" "$D"
} > Caddyfile

printf 'services:\n  bancho:\n    ports: !reset []\n    restart: unless-stopped\n  extras:\n    build: /opt/oily-web/extras\n    image: oily-extras:latest\n    restart: unless-stopped\n    environment:\n      API_HOST: api.%s\n    volumes:\n      - extras_data:/data\n    depends_on:\n      - bancho\n  caddy:\n    image: caddy:2-alpine\n    restart: unless-stopped\n    ports:\n      - "80:80"\n      - "443:443"\n    volumes:\n      - ./Caddyfile:/etc/caddy/Caddyfile:ro\n      - caddy_data:/data\n      - caddy_config:/config\n      - /opt/oily-web:/srv/oily-web:ro\n      - data:/srv/bancho-data:ro\n    depends_on:\n      - bancho\n      - extras\nvolumes:\n  caddy_data:\n  caddy_config:\n  extras_data:\n' "$D" > oily.override.yml

C="docker compose --env-file .env -f docker-compose.yml -f oily.override.yml"

echo "== 4/6 Starting everything (extras builds the first time, about 1 minute) =="
$C build -q extras
# only touch bancho, extras and caddy: recreating redis would wipe the rank lists
$C up -d --no-deps --force-recreate bancho
$C up -d --no-deps extras
$C up -d --no-deps --force-recreate caddy
$C cp /opt/oily-web/assets/default-avatar.jpg bancho:/srv/root/.data/avatars/default.jpg

echo "== 5/6 Nightly backups =="
mkdir -p /opt/backups && chmod 700 /opt/backups
printf '%s\n' '#!/usr/bin/env bash' \
 'set -euo pipefail' \
 'trap '"'"'echo "!! backup FAILED: $BASH_COMMAND" >&2'"'"' ERR' \
 'cd /opt/bancho' \
 'C="docker compose --env-file .env -f docker-compose.yml -f oily.override.yml"' \
 'T=$(date +%F-%H%M)' \
 '$C exec -T mysql sh -c '"'"'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump --no-tablespaces -u"$MYSQL_USER" "$MYSQL_DATABASE"'"'"' < /dev/null | gzip > /opt/backups/db-$T.sql.gz' \
 '$C exec -T bancho tar czf - -C /srv/root/.data avatars osr < /dev/null > /opt/backups/files-$T.tar.gz' \
 '$C exec -T extras tar czf - -C /data . < /dev/null > /opt/backups/extras-$T.tar.gz' \
 'find /opt/backups -name "db-*.sql.gz" -mtime +14 -delete' \
 'find /opt/backups \( -name "files-*.tar.gz" -o -name "extras-*.tar.gz" \) -mtime +7 -delete' \
 'echo "backup $T done: $(du -sh /opt/backups | cut -f1) total"' > /usr/local/bin/oily-backup
chmod 700 /usr/local/bin/oily-backup
echo '0 4 * * * root /usr/local/bin/oily-backup >> /var/log/oily-backup.log 2>&1' > /etc/cron.d/oily-backup
sleep 10
/usr/local/bin/oily-backup < /dev/null || echo "!! first backup failed (see above); the rest of the update continues"
ls -lh /opt/backups | tail -4

echo "== 6/6 Checks =="
bash /opt/oily-web/server/rebuild-ranks.sh
$C ps --format 'table {{.Service}}\t{{.Status}}'
curl -s -o /dev/null -w "website: HTTP %{http_code}\n" "https://$D/" || true
curl -s -o /dev/null -w "extras: HTTP %{http_code}\n" "https://$D/extras/ranked" || true
curl -s -o /dev/null -w "menu icon: HTTP %{http_code}\n" "https://$D/assets/menu-icon.png" || true
echo "== Done. Restart osu! to see the new menu icon and backgrounds. =="
}
main "$@" < /dev/null
