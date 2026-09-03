#!/usr/bin/env bash
# Deploy a new release of the Anvil console.
#
# Run as the deploy user, on the server:
#   sudo -u deploy bash /opt/anvil/repo/deploy/deploy.sh [git-ref]
#
# Atomic by symlink: a new release is built in full and only then does
# /opt/anvil/current point at it. A failed build leaves the running version
# untouched, and rollback is a symlink swap.

set -Eeuo pipefail

APP_ROOT=/opt/anvil
REPO_DIR="$APP_ROOT/repo"
RELEASES="$APP_ROOT/releases"
CURRENT="$APP_ROOT/current"
VENV="$APP_ROOT/venv"
REF="${1:-origin/main}"
KEEP=5
HEALTH_URL="http://127.0.0.1:8000/health"

log()  { printf '\n\033[1;33m==>\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mFAILED:\033[0m %s\n' "$*" >&2; exit 1; }

log "Fetching $REF"
git -C "$REPO_DIR" fetch --prune --depth 50 origin
git -C "$REPO_DIR" rev-parse --verify "$REF" >/dev/null || die "no such ref: $REF"
SHA="$(git -C "$REPO_DIR" rev-parse --short "$REF")"
STAMP="$(date -u +%Y%m%d-%H%M%S)"
RELEASE="$RELEASES/${STAMP}-${SHA}"

log "Building release ${STAMP}-${SHA}"
rm -rf "$RELEASE"
mkdir -p "$RELEASE"
git -C "$REPO_DIR" archive "$REF" | tar -x -C "$RELEASE"

log "Dependencies"
# Installed into the shared venv rather than one per release: the dependency set
# changes rarely, and a venv per release would mean 300MB per deploy.
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet -e "$RELEASE"

log "Pre-flight against the new code"
# Refuse to promote a release whose own tests do not pass. Discovering that
# after the symlink has moved is discovering it in production.
( cd "$RELEASE" && "$VENV/bin/python" -m pytest tests/unit -q ) \
  || die "the unit suite does not pass on this release; nothing was promoted"

# The two commands a reviewer is most likely to run. If either is broken the
# release is not fit to be the thing people are shown.
( cd "$RELEASE" && "$VENV/bin/python" scripts/tour.py >/dev/null ) \
  || die "scripts/tour.py is broken on this release"

log "Promoting"
PREVIOUS="$(readlink -f "$CURRENT" 2>/dev/null || true)"
ln -sfn "$RELEASE" "$CURRENT.new"
mv -Tf "$CURRENT.new" "$CURRENT"

log "Restarting"
sudo systemctl restart anvil

log "Waiting for health"
for _ in $(seq 1 30); do
  if curl -fsS --max-time 3 "$HEALTH_URL" >/dev/null 2>&1; then
    body="$(curl -fsS "$HEALTH_URL")"
    echo "$body"
    # A public instance holding payment credentials would be the one mistake
    # this whole deployment posture exists to prevent. Check it every time.
    echo "$body" | grep -q '"mode":"offline"' \
      || die "the deployed instance is NOT in offline mode; rolling back"
    log "Healthy on ${STAMP}-${SHA}"
    # Keep the last few releases so a rollback needs no network.
    # Release names are UTC-timestamp-first, so lexicographic order is
    # chronological order and a glob sorts correctly without parsing ls.
    mapfile -t all < <(printf '%s\n' "$RELEASES"/*/ | sort -r)
    for old in "${all[@]:$KEEP}"; do rm -rf -- "$old"; done
    exit 0
  fi
  sleep 2
done

log "Health check never passed; rolling back"
if [[ -n "${PREVIOUS:-}" && -d "$PREVIOUS" ]]; then
  ln -sfn "$PREVIOUS" "$CURRENT.new"
  mv -Tf "$CURRENT.new" "$CURRENT"
  sudo systemctl restart anvil
  die "rolled back to $(basename "$PREVIOUS"). Check: journalctl -u anvil -n 80"
fi
die "no previous release to roll back to. Check: journalctl -u anvil -n 80"
