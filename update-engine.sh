#!/bin/sh
# Run only inside the fixed-action Docker CLI helper created by gpu-control.
set -eu
cd /stack
image='ghcr.io/ggml-org/llama.cpp:server-cuda13'
services='huihui flint-iq4 flint-q3 deepseek-v4-distill qwen38-ablit gemma4-ablit'
mode=${1:-apply}

compose() {
  docker compose --env-file .env --profile huihui --profile flint \
    --profile deepseek --profile qwen38 --profile gemma4 "$@"
}

active=''
running=$(compose ps --status running --services)
for service in $services; do
  if printf '%s\n' "$running" | grep -qx "$service"; then
    active=$service
    break
  fi
done

old=${OLD_IMAGE_ID:-}
printf '%s\n' "$old" | grep -Eq '^sha256:[a-f0-9]{64}$' || { echo 'Invalid rollback image ID' >&2; exit 2; }
rollback_tag="local-ai/llama-cuda13:rollback-$(printf '%s' "$old" | cut -c8-19)"
docker tag "$old" "$rollback_tag"

restore() {
  echo 'Restoring previous llama.cpp image'
  docker tag "$old" "$image"
  failed=0
  for service in $services; do
    if [ "$service" != "$active" ]; then
      compose up --no-start --no-deps --force-recreate --pull never "$service" || failed=1
    fi
  done
  if [ -n "$active" ]; then
    compose up -d --no-deps --force-recreate --pull never "$active" || failed=1
  fi
  return "$failed"
}

if [ "$mode" = rollback ]; then
  restore
  exit 0
fi
[ "$mode" = apply ] || { echo 'Invalid update mode' >&2; exit 2; }
trap 'restore' EXIT
echo 'Pulling current llama.cpp CUDA 13 image'
docker pull "$image"
new=$(docker image inspect --format '{{.Id}}' "$image")
if [ "$new" = "$old" ]; then
  echo 'Engine image is already current'
  trap - EXIT
  exit 0
fi
echo "Staged image $new"
docker run --rm --entrypoint /app/llama-server "$image" --version
for service in $services; do
  if [ "$service" != "$active" ]; then
    echo "Recreating stopped service $service"
    compose up --no-start --no-deps --force-recreate --pull never "$service"
  fi
done
if [ -n "$active" ]; then
  echo "Recreating active service $active"
  compose up -d --no-deps --force-recreate --pull never "$active"
fi
trap - EXIT
echo 'Compose services updated; checking model readiness'
