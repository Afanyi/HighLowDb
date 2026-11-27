#!/usr/bin/env bash
# The same verification runs locally and on an ephemeral CI runner. No SSH or app .env.
set -euo pipefail

cd "$(dirname "$0")/.."
export PYTHONDONTWRITEBYTECODE=1
export DOCKER_HOST=unix:///var/run/docker.sock

bash -n tests/check_ci.sh
bash -n deploy/setup.sh
bash -n deploy/update.sh
python3 -m unittest tests.check_ci -q
docker version
docker compose version

# Fresh runners have no image cache. Download before tests disable networking/pulls.
for image in \
  python:3.13.15-slim-bookworm \
  docker:29-cli \
  alpine:3.22 \
  caddy:2.10.2-alpine \
  curlimages/curl:8.12.1
do
  docker pull "$image"
done
docker build --tag allstocksdatabase-app:latest .

# Exercise the built source with no host mounts, provider network or live database.
docker run --rm --pull=never --network none \
  --read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m \
  --memory 512m --memory-swap 512m --cpus 0.5 --pids-limit 128 \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --entrypoint python \
  -e T212_API_KEY=ci-dummy-stock -e T212_ENVIRONMENT=demo \
  -e COINGECKO_API_KEY=ci-dummy-crypto \
  -e DB_PATH=/tmp/stocks.db -e CRYPTO_DB_PATH=/tmp/crypto.db \
  allstocksdatabase-app:latest -m unittest -q

# These host-side checks are not included in default unittest discovery.
python3 -m unittest tests.check_deploy -q
python3 tests/check_deploy_docker.py
python3 tests/check_production.py

# Test the actual allowlisted archive without reading SSH/application configuration.
bash deploy/update.sh --dry-run
