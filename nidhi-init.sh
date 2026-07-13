#!/bin/sh
set -e

if [ -z "$NIDHI_DEV_SERVER_URL" ] || [ -z "$NIDHI_APP_API_KEY" ] || [ -z "$PROJECT_SLUG" ]; then
    echo "❌ FATAL: Missing required Nidhi environment variables."
    echo "    NIDHI_DEV_SERVER_URL=${NIDHI_DEV_SERVER_URL:-<unset>}"
    echo "    NIDHI_APP_API_KEY=${NIDHI_APP_API_KEY:+<set>}${NIDHI_APP_API_KEY:-<unset>}"
    echo "    PROJECT_SLUG=${PROJECT_SLUG:-<unset>}"
    echo "Abhyas requires Nidhi-provisioned databases. Cannot start without Nidhi."
    exit 1
fi

ENVIRONMENT="${ENVIRONMENT:-development}"
echo "📡 Contacting Nidhi Control Plane to auto-provision database for '$PROJECT_SLUG' in '$ENVIRONMENT'..."

cat << 'PYEOF' > /tmp/nidhi_req.py
import urllib.request, json, os, sys

url = os.environ.get("NIDHI_DEV_SERVER_URL", "") + "/api/instances/auto-provision/"
data = json.dumps({"project_slug": os.environ.get("PROJECT_SLUG", ""), "environment": os.environ.get("ENVIRONMENT", "")}).encode("utf-8")
req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer " + os.environ.get("NIDHI_APP_API_KEY", "")})
try:
    with urllib.request.urlopen(req) as f:
        resp = json.loads(f.read().decode("utf-8"))
        print(resp.get("database_url", ""))
        print(resp.get("bucket_name", ""))
        print(resp.get("bucket_endpoint", ""))
        print(resp.get("bucket_access_key", ""))
        print(resp.get("bucket_secret_key", ""))
except Exception as e:
    print("Python error: " + str(e), file=sys.stderr)
    sys.exit(1)
PYEOF

RESPONSE=$(python /tmp/nidhi_req.py)

DATABASE_URL=$(echo "$RESPONSE" | sed -n "1p")
BUCKET_NAME=$(echo "$RESPONSE" | sed -n "2p")
BUCKET_ENDPOINT=$(echo "$RESPONSE" | sed -n "3p")
BUCKET_ACCESS_KEY=$(echo "$RESPONSE" | sed -n "4p")
BUCKET_SECRET_KEY=$(echo "$RESPONSE" | sed -n "5p")

if [ -z "$DATABASE_URL" ]; then
    echo "❌ FATAL: Failed to provision database from Nidhi."
    echo "    Nidhi response: $RESPONSE"
    echo "    Abhyas cannot start without a Nidhi-provisioned database."
    exit 1
fi

DATABASE_URL=$(echo "$DATABASE_URL" | sed "s|^postgres://|postgresql://|")
export DATABASE_URL="$DATABASE_URL"
echo "✅ Database provisioned: $DATABASE_URL"

if [ -n "$BUCKET_NAME" ]; then
    NIDHI_HOST=$(echo "$NIDHI_DEV_SERVER_URL" | sed "s|http://||" | sed "s|https://||" | sed "s|:.*||")
    export MEDIA_BUCKET_NAME="$BUCKET_NAME"
    export MINIO_ENDPOINT="${BUCKET_ENDPOINT:-$NIDHI_HOST:9000}"
    export MINIO_ACCESS_KEY="${BUCKET_ACCESS_KEY:-}"
    export MINIO_SECRET_KEY="${BUCKET_SECRET_KEY:-}"
    echo "🪣 Media bucket: $MEDIA_BUCKET_NAME at $MINIO_ENDPOINT"
else
    echo "⚠️ WARNING: No storage bucket provisioned. File uploads will not persist."
fi

echo "🚀 Running migrations..."
sleep 2
python manage.py migrate 2>&1 | tail -5
echo "✅ Migrations complete."

cat << "HEOF" > /tmp/nidhi_heartbeat.py
import urllib.request, json, os, sys, time
url = os.environ.get("NIDHI_DEV_SERVER_URL", "") + "/api/instances/heartbeat/"
api_key = os.environ.get("NIDHI_APP_API_KEY", "")
project_slug = os.environ.get("PROJECT_SLUG", "")
environment = os.environ.get("ENVIRONMENT", "production")
db_url = os.environ.get("DATABASE_URL", "")
if not (url and api_key and project_slug and db_url):
    sys.exit(0)
payload = json.dumps({"project_slug": project_slug, "environment": environment, "db_url": db_url}).encode("utf-8")
headers = {"Content-Type": "application/json", "Authorization": api_key}
while True:
    try:
        req = urllib.request.Request(url, data=payload, headers=headers)
        with urllib.request.urlopen(req, timeout=5) as f:
            pass
    except Exception:
        pass
    time.sleep(300)
HEOF

python /tmp/nidhi_heartbeat.py &
echo "💓 Heartbeat monitoring started."

exec gunicorn pyqproject.wsgi:application --bind 0.0.0.0:8000 --workers 2 --timeout 300
