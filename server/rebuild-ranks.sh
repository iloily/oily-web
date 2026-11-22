#!/usr/bin/env bash
# Rebuild bancho.py's rank lists (kept in redis) from the database.
# Needed if redis was restarted before it saved, which leaves players missing from the ranks.
set -euo pipefail
cd /opt/bancho
C="docker compose --env-file .env -f docker-compose.yml -f oily.override.yml"
$C exec -T mysql sh -c 'mysql -N -B -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$MYSQL_DATABASE" -e "select s.mode, s.id, s.pp, u.country from stats s join users u on u.id = s.id where s.pp > 0 and (u.priv & 1) = 1"' 2>/dev/null > /tmp/oily-ranks.tsv
$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASS" --no-auth-warning --scan --pattern "bancho:leaderboard:*" | xargs -r redis-cli -a "$REDIS_PASS" --no-auth-warning del' > /dev/null
awk -F'\t' '{ print "ZADD bancho:leaderboard:" $1 " " $3 " " $2; print "ZADD bancho:leaderboard:" $1 ":" $4 " " $3 " " $2 }' /tmp/oily-ranks.tsv \
  | $C exec -T redis sh -c 'redis-cli -a "$REDIS_PASS" --no-auth-warning' > /dev/null
$C exec -T redis sh -c 'redis-cli -a "$REDIS_PASS" --no-auth-warning save' > /dev/null
echo "ranks rebuilt for $(wc -l < /tmp/oily-ranks.tsv) player/mode entries"
rm -f /tmp/oily-ranks.tsv
