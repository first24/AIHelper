#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v docker >/dev/null 2>&1; then
  echo 'Install Docker Engine and the Compose plugin from https://docs.docker.com/engine/install/ first.'
  exit 1
fi
docker compose version >/dev/null
if [ ! -f .env ]; then
  cp .env.example .env
  chmod 600 .env
  echo 'Created .env. Fill in your credentials locally on the server, then run sh deploy.sh again.'
  exit 2
fi
chmod 600 .env
docker compose config --quiet
docker compose build --pull
docker compose run --rm assistant python -m assistant doctor
docker compose up -d --no-build
docker compose ps
