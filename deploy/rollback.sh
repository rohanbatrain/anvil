#!/usr/bin/env bash
# Roll back to the previous release. A symlink swap and a restart.
#
#   sudo -u deploy bash /opt/anvil/repo/deploy/rollback.sh          # previous
#   sudo -u deploy bash /opt/anvil/repo/deploy/rollback.sh <name>   # a specific one

set -Eeuo pipefail
RELEASES=/opt/anvil/releases
CURRENT=/opt/anvil/current

if [[ -n "${1:-}" ]]; then
  TARGET="$RELEASES/$1"
else
  # The most recent release that is not the live one. Names are
  # UTC-timestamp-first, so a reverse glob sort is chronological.
  LIVE="$(readlink -f "$CURRENT")"
  TARGET=""
  while IFS= read -r candidate; do
    [[ "$(readlink -f "$candidate")" == "$LIVE" ]] && continue
    TARGET="$candidate"
    break
  done < <(printf '%s\n' "$RELEASES"/*/ | sort -r)
fi

[[ -d "$TARGET" ]] || { echo "no such release: $TARGET" >&2; exit 1; }

echo "rolling back to $(basename "$TARGET")"
ln -sfn "$TARGET" "$CURRENT.new"
mv -Tf "$CURRENT.new" "$CURRENT"
sudo systemctl restart anvil

for _ in $(seq 1 20); do
  curl -fsS --max-time 3 http://127.0.0.1:8000/health >/dev/null 2>&1 && { echo "healthy"; exit 0; }
  sleep 2
done
echo "rollback did not become healthy; journalctl -u anvil -n 80" >&2
exit 1
