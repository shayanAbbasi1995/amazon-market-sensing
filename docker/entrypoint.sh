#!/bin/sh
# Full pipeline, safe to restart at any point:
#   1. McAuley download (skipped if already on disk)
#   2. Keepa, reference marketplace (resumes from its visited ledger)
#   3. shared ASIN universe (skipped if it exists)
#   4. Keepa, every other marketplace, one after another
#   5. panels
# A non-zero exit lets `restart: unless-stopped` bring the container back; it
# then resumes where it stopped. After a clean finish it idles instead of
# restarting in a loop.
set -eu

REF=$(python -c "from amsense.config import load; print(load().reference_marketplace)")

python -m amsense mcauley
python -m amsense collect --marketplace "$REF"
[ -f "$(python -c "from amsense.config import load; print(load().universe_path)")" ] || python -m amsense universe
python -m amsense collect
python -m amsense panels

echo "[entrypoint] pipeline finished; idling. Stop with: docker compose down"
exec tail -f /dev/null
