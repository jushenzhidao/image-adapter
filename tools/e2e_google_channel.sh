#!/usr/bin/env bash
# End-to-end check of the google/images@v1 channel against a real upstream.
#
#   export GEMINI_KEY=sk-...
#   export UPSTREAM="https://api.chatfire.cn/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
#   export IMAGE_URL="https://s3ai.cn/cdn/20260901/53bbd0874ac247549beb8cb0226b5e66.jpg"
#   bash e2e_google_channel.sh
#
# Optional:
#   OVERLAY=/path/to/script-root   -> served before the image store (SCRIPT_OVERLAY_DIRS)
#   MODEL=...                      -> model written into the request body
# Adapter state is forced to in-process fallbacks (empty REDIS_URL / MINIO_*) so a
# probe never writes to the production Redis or bucket.

set -u

ROOT=${ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}
KEY=${GEMINI_KEY:?export GEMINI_KEY=...}
UPSTREAM=${UPSTREAM:?export UPSTREAM=https://host/v1beta/models/<model>:generateContent}
IMAGE_URL=${IMAGE_URL:?export IMAGE_URL=https://...}
MODEL=${MODEL:-gemini-3.1-flash-image-preview}
OUT=${OUT:-/tmp/gemini-e2e}
OVERLAY=${OVERLAY:-}

mkdir -p "$OUT"
export NO_PROXY=localhost,127.0.0.1 no_proxy=localhost,127.0.0.1

PORT=$(python3 -c "import socket
s = socket.socket(); s.bind(('127.0.0.1', 0)); print(s.getsockname()[1]); s.close()")
BASE="http://127.0.0.1:$PORT"

echo "upstream: $UPSTREAM"
echo "adapter:  $BASE   (log: $OUT/adapter.log)"
[ -n "$OVERLAY" ] && echo "overlay:  $OVERLAY"
echo

curl -sS --max-time 60 -o "$OUT/input.jpg" "$IMAGE_URL"
echo "input image: $(wc -c < "$OUT/input.jpg") bytes"

MODEL="$MODEL" IMAGE_URL="$IMAGE_URL" "$ROOT/.venv/bin/python" - "$OUT" <<'PY'
import base64, io, json, os, pathlib, sys

out = pathlib.Path(sys.argv[1])
raw = (out / "input.jpg").read_bytes()
model = os.environ["MODEL"]

from PIL import Image

im = Image.open(io.BytesIO(raw))
im.thumbnail((768, 768))
buf = io.BytesIO()
im.convert("RGB").save(buf, "JPEG", quality=80)
small = buf.getvalue()

def dump(name, image):
    body = {
        "model": model,
        "prompt": "keep the subject, change the background to a soft blue studio",
        "response_format": "b64_json",
    }
    if image is not None:
        body["image"] = base64.b64encode(image).decode("ascii")
    (out / f"{name}.json").write_text(json.dumps(body, ensure_ascii=False),
                                      encoding="utf-8")
    return len(body.get("image", ""))

(out / "t2i.json").write_text(json.dumps({
    "model": model,
    "prompt": "a red apple on a white table, studio light",
    "size": "1024x1024",
    "response_format": "b64_json",
}, ensure_ascii=False), encoding="utf-8")

print(f"small image: {len(small)} bytes -> {dump('i2i_b64_small', small) // 1024} KB b64")
print(f"big image:   {len(raw)} bytes -> {dump('i2i_b64_big', raw) // 1024} KB b64")

(out / "i2i_url.json").write_text(json.dumps({
    "model": model,
    "prompt": "keep the subject, change the background to a soft blue studio",
    "image": os.environ["IMAGE_URL"],
    "response_format": "b64_json",
}, ensure_ascii=False), encoding="utf-8")
PY

cd "$ROOT"
env LOGFIRE_TOKEN="" ADAPTER_KEY=probe-key ADAPTER_KEY_REQUIRED=true \
    REDIS_URL= MINIO_ENDPOINT= MINIO_ACCESS_KEY= MINIO_SECRET_KEY= \
    UPSTREAM_TIMEOUT=180 WEB_CONCURRENCY=1 SCRIPT_OVERLAY_DIRS="$OVERLAY" \
    .venv/bin/uvicorn adapter.main:app --host 127.0.0.1 --port "$PORT" \
    --log-level warning > "$OUT/adapter.log" 2>&1 &
APP=$!
trap 'kill '"$APP"' 2>/dev/null' EXIT

for _ in $(seq 1 60); do
  curl -sf -o /dev/null "$BASE/health" && break
  sleep 0.5
done

probe() {
  # name, optional X-Channel-Options JSON, optional label suffix
  local name=$1 opts=${2:-} tag=${3:-} label="${1}${3:+_$3}" code extra=()
  [ -n "$opts" ] && extra=(-H "X-Channel-Options: $opts")
  code=$(curl -sS --max-time 300 -o "$OUT/$label.resp.json" -w '%{http_code}' \
    -X POST "$BASE/v1/images/generations" \
    -H 'Content-Type: application/json' \
    -H 'X-Adapter-Key: probe-key' \
    -H "X-Upstream-Url: $UPSTREAM" \
    -H 'X-Script-Ref: google/images@v1' \
    -H 'X-Auth-Emit: header:x-goog-api-key' \
    -H "Authorization: Bearer $KEY" \
    ${extra[@]+"${extra[@]}"} \
    --data-binary "@$OUT/$name.json")
  printf '%-22s http=%-4s %s\n' "$label" "$code" "$(python3 - "$OUT/$label.resp.json" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:
    print(f"unparsable ({exc})"); raise SystemExit
if d.get("error"):
    e = d["error"]
    print(f"ERROR code={e.get('code')} param={e.get('param')} msg={str(e.get('message'))[:110]!r}")
    raise SystemExit
data = d.get("data") or []
blob = (data[0].get("b64_json") or data[0].get("url") or "") if data else ""
print(f"images={len(data)} payload={len(blob)} usage={d.get('usage')}")
PY
)"
}

probe t2i
probe i2i_url
probe i2i_b64_small '{"image_ref_mode": "inline"}'
probe i2i_b64_big '' auto
probe i2i_b64_big '{"image_ref_mode": "inline"}' inline

echo
echo "raw replies: $OUT/*.resp.json"
echo "adapter log: $OUT/adapter.log"
