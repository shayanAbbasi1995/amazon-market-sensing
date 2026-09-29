#!/bin/sh
# Full pipeline, safe to restart at any point:
#   1. McAuley download, only when asins.source is mcauley (skipped if on disk)
#   2. Keepa, reference marketplace (builds the seed list if missing, then
#      resumes from its visited ledger)
#   3. shared ASIN universe (skipped if it exists)
#   4. Keepa, every other marketplace, one after another
#   5. panels
# A non-zero exit lets `restart: unless-stopped` bring the container back; it
# then resumes where it stopped. After a clean finish it idles instead of
# restarting in a loop.
set -eu

cfg() { python -c "from amsense.config import load; s = load(); print($1)"; }

if [ "$(cfg 's.asin_source["source"]')" = "mcauley" ]; then
  python -m amsense mcauley
fi
python -m amsense collect --marketplace "$(cfg 's.reference_marketplace')"
[ -f "$(cfg 's.universe_path')" ] || python -m amsense universe
python -m amsense collect
python -m amsense panels

echo "[entrypoint] pipeline finished; idling. Stop with: docker compose down"
exec tail -f /dev/null
