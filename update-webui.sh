#!/bin/sh
# Run only inside the fixed-action Docker CLI helper created by gpu-control.
set -eu
cd /stack
image='ghcr.io/open-webui/open-webui:main'
container='local-ai-open-webui-1'
old=${OLD_IMAGE_ID:-}
job=${JOB_ID:-}
backup_host_root=${BACKUP_HOST_ROOT:-}
printf '%s\n' "$old" | grep -Eq '^sha256:[a-f0-9]{64}$' || { echo 'Invalid rollback image ID' >&2; exit 2; }
printf '%s\n' "$job" | grep -Eq '^[a-f0-9-]{36}$' || { echo 'Invalid update job ID' >&2; exit 2; }
case "$backup_host_root" in
  /*/gpu_control/results/webui-backups) ;;
  *) echo 'Invalid host backup path' >&2; exit 2 ;;
esac
backup="/backups/webui-backups/$job"
volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/app/backend/data"}}{{.Name}}{{end}}{{end}}' "$container")
printf '%s\n' "$volume" | grep -Eq '^local-ai_[A-Za-z0-9_-]+$' || { echo 'Open WebUI data volume was not found' >&2; exit 2; }

compose() { docker compose --env-file .env "$@"; }
wait_healthy() {
  count=0
  while [ "$count" -lt 180 ]; do
    state=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)
    [ "$state" = healthy ] && return 0
    [ "$state" = unhealthy ] && return 1
    [ "$state" = exited ] && return 1
    count=$((count + 1))
    sleep 5
  done
  return 1
}
reload_proxy() {
  docker exec local-ai-nginx-1 nginx -s reload
}
restore() {
  trap - EXIT
  echo 'Restoring previous Open WebUI image and data'
  compose stop open-webui || true
  docker tag "$old" "$image"
  docker run --rm --network none \
    --mount "type=volume,source=$volume,target=/data" \
    --mount "type=bind,source=$backup_host_root/$job,target=/restore,readonly" \
    --entrypoint sh docker:cli -ec 'find /data -mindepth 1 -maxdepth 1 -exec rm -rf -- {} \;; cp -a /restore/. /data/'
  compose up -d --no-deps --force-recreate --pull never open-webui
  wait_healthy
  reload_proxy
}

echo 'Stopping Open WebUI for a consistent data backup'
compose stop open-webui
mkdir -p "$backup"
chmod 700 /backups/webui-backups "$backup"
if ! docker cp "$container:/app/backend/data/." "$backup"; then
  echo 'Backup failed; restarting existing Open WebUI' >&2
  compose start open-webui || true
  exit 1
fi
echo "Backup saved at $backup_host_root/$job"
trap 'restore' EXIT

echo 'Pulling the current Open WebUI image'
docker pull "$image"
new=$(docker image inspect --format '{{.Id}}' "$image")
[ "$new" != "$old" ] || { echo 'Registry image is unchanged'; exit 1; }
echo 'Recreating Open WebUI with its existing data volume'
compose up -d --no-deps --force-recreate --pull never open-webui
wait_healthy || { echo 'Updated Open WebUI did not become healthy'; exit 1; }
reload_proxy
trap - EXIT
echo 'Open WebUI update complete and healthy'
