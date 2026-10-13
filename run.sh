#!/bin/sh
# Start von DaLiegt’s als Home-Assistant-App. Daten liegen in /data (werden von HA-Backups gesichert).
set -e
export LAGERWERK_DATA_DIR=/data LAGERWERK_HA_INGRESS=1
BASE_URL=$(python3 -c "import json; print(json.load(open('/data/options.json')).get('base_url') or '')" 2>/dev/null || true)
if [ -n "$BASE_URL" ]; then export LAGERWERK_BASE_URL="$BASE_URL"; fi
cd /app
python3 -m app.cli migrate
# ohne --proxy-headers: die Absenderadresse des Supervisors (172.30.32.2) muss erhalten bleiben
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
