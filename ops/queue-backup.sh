#!/usr/bin/env bash
# Запускается на хосте стенда. Секреты берёт Docker Compose из файла окружения.
set -euo pipefail
umask 077
cd "${DOMOFOMKA_ROOT:?Set DOMOFOMKA_ROOT}"
project="${COMPOSE_PROJECT_NAME:-domofomka-dev}"
env_file="${DOMOFOMKA_ENV_FILE:-.env.dev}"
backup_dir="$PWD/backups/queue"
mkdir -p "$backup_dir"
chmod 700 "$backup_dir"
exec 9>"$backup_dir/.lock"
flock -n 9 || exit 0
compose=(docker compose -p "$project" --env-file "$env_file" -f docker-compose.yml)
# Оставляем минимум две копии, даже если стенд долго не запускался.
mapfile -t old < <(find "$backup_dir" -maxdepth 1 -name 'queue-*.dump' -type f -printf '%T@ %p\n' | sort -nr | tail -n +3 | cut -d' ' -f2-)
for file in "${old[@]}"; do
    if [[ $(find "$file" -mtime +"${QUEUE_BACKUP_KEEP_DAYS:-3}" -print) ]]; then rm -- "$file"; fi
done
size=$("${compose[@]}" exec -T queue psql -U domofomka -d domofomka -Atc 'SELECT pg_database_size(current_database())')
free=$(df -PB1 "$backup_dir" | awk 'NR==2 {print $4}')
if (( free < size + ${QUEUE_BACKUP_RESERVE_BYTES:-268435456} )); then
    echo 'Not enough free space for a queue backup' >&2
    exit 1
fi
file="$backup_dir/queue-$(date -u +%Y%m%dT%H%M%SZ).dump"
check_db=""
cleanup() {
    rm -f -- "$file.part"
    if [[ "$check_db" == domofomka_restore_check_* ]]; then
        "${compose[@]}" exec -T queue dropdb --if-exists -U domofomka "$check_db"
    fi
}
trap cleanup EXIT
"${compose[@]}" exec -T queue pg_dump -U domofomka -d domofomka --format=custom > "$file.part"
"${compose[@]}" exec -T queue pg_restore --list < "$file.part" > /dev/null
if [[ "${1:-}" == --verify ]]; then
    check_db="domofomka_restore_check_$(date +%s)_$$"
    "${compose[@]}" exec -T queue createdb -U domofomka "$check_db"
    "${compose[@]}" exec -T queue pg_restore --exit-on-error --no-owner -U domofomka -d "$check_db" < "$file.part"
    "${compose[@]}" exec -T queue psql -v ON_ERROR_STOP=1 -U domofomka -d "$check_db" -Atc 'SELECT count(*) FROM bot_inbox' > /dev/null
    echo 'Queue backup restore verified'
fi
mv -- "$file.part" "$file"
echo "Queue backup saved: $(basename "$file")"
