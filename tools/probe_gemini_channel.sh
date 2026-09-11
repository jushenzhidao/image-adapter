#!/usr/bin/env bash
# Probe a Gemini-compatible image endpoint (the "Nano Banana" family) for the
# exact wire contract that the adapter's google/images@v1 script assumes.
#
#   export GEMINI_KEY=sk-...
#   export UPSTREAM="https://<host>/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
#   export IMAGE_URL="https://s3ai.cn/cdn/20260901/53bbd0874ac247549beb8cb0226b5e66.jpg"
#   bash probe_gemini_channel.sh
#
# Optional: IMAGE_FILE=/path/to.jpg uses a local file for the inline case instead
#           of downloading IMAGE_URL.  Raw replies land in $OUT (default
#           /tmp/gemini-probe).  The key is never printed.

set -u

KEY=${GEMINI_KEY:?export GEMINI_KEY=...}
UPSTREAM=${UPSTREAM:?export UPSTREAM=https://host/v1beta/models/<model>:generateContent}
IMAGE_URL=${IMAGE_URL:-}
IMAGE_FILE=${IMAGE_FILE:-}
OUT=${OUT:-/tmp/gemini-probe}
PROMPT_T2I=${PROMPT_T2I:-"a red apple on a white table, studio light"}
PROMPT_I2I=${PROMPT_I2I:-"keep the subject, change the background to a soft blue studio"}

mkdir -p "$OUT"

summary() {
  python3 - "$1" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception as exc:                       # noqa: BLE001 - probe output
    print(f"unparsable ({exc})")
    raise SystemExit
if isinstance(d, dict) and d.get("error"):
    e = d["error"]
    print(f"ERROR code={e.get('code')} status={e.get('status')} "
          f"msg={str(e.get('message'))[:100]!r}")
    raise SystemExit
cands = d.get("candidates") or []
parts = [p for c in cands for p in (c.get("content") or {}).get("parts") or []]
imgs = [p.get("inlineData") or p.get("inline_data") for p in parts]
imgs = [i for i in imgs if i and i.get("data")]
texts = [p.get("text") for p in parts if p.get("text")]
finish = cands[0].get("finishReason") if cands else None
usage = d.get("usageMetadata") or {}
tokens = {k: v for k, v in usage.items() if k.endswith("TokenCount")}
bits = [f"imgs={len(imgs)}"]
if imgs:
    mime = imgs[0].get("mimeType") or imgs[0].get("mime_type")
    bits.append(f"mime={mime} b64len={len(imgs[0]['data'])}")
if texts:
    bits.append(f"text={texts[0][:40]!r}")
bits.append(f"finish={finish}")
bits.append(f"usage={tokens}")
block = (d.get("promptFeedback") or {}).get("blockReason")
if block:
    bits.append(f"block={block}")
print(" ".join(bits))
PY
}

post() {
  # The body goes through a file: an inline base64 payload is far past ARG_MAX.
  local name=$1 body=$2 code
  printf '%s' "$body" > "$OUT/$name.req.json"
  code=$(curl -sS --max-time 180 -o "$OUT/$name.json" -w '%{http_code}' \
    -X POST "$UPSTREAM" \
    -H "Authorization: Bearer $KEY" \
    -H 'Content-Type: application/json' \
    --data-binary "@$OUT/$name.req.json" 2>/dev/null)
  printf '%-24s http=%-4s %s\n' "$name" "$code" "$(summary "$OUT/$name.json")"
}

echo "upstream: $UPSTREAM"
echo "output:   $OUT"
echo

# --- text-to-image, both field generations, all size tiers ------------------
post t2i-imageConfig-1K "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_T2I\"}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"],\"imageConfig\":{\"aspectRatio\":\"1:1\",\"imageSize\":\"1K\"}}}"
post t2i-responseFormat-1K "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_T2I\"}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"],\"responseFormat\":{\"image\":{\"aspectRatio\":\"1:1\",\"imageSize\":\"1K\"}}}}"
post t2i-imageConfig-2K "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_T2I\"}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"],\"imageConfig\":{\"aspectRatio\":\"16:9\",\"imageSize\":\"2K\"}}}"
post t2i-imageConfig-4K "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_T2I\"}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"],\"imageConfig\":{\"aspectRatio\":\"1:1\",\"imageSize\":\"4K\"}}}"
post t2i-textmodality "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_T2I\"}]}],\"generationConfig\":{\"responseModalities\":[\"TEXT\",\"IMAGE\"],\"imageConfig\":{\"aspectRatio\":\"1:1\",\"imageSize\":\"1K\"}}}"

# --- image-to-image: forwarded URL, inlined base64, two references ----------
if [ -n "$IMAGE_URL" ]; then
  post i2i-fileData-url "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_I2I\"},{\"file_data\":{\"mime_type\":\"image/jpeg\",\"file_uri\":\"$IMAGE_URL\"}}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"]}}"
  post i2i-fileData-camel "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_I2I\"},{\"fileData\":{\"mimeType\":\"image/jpeg\",\"fileUri\":\"$IMAGE_URL\"}}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"]}}"
  post i2i-two-refs "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_I2I\"},{\"file_data\":{\"mime_type\":\"image/jpeg\",\"file_uri\":\"$IMAGE_URL\"}},{\"file_data\":{\"mime_type\":\"image/jpeg\",\"file_uri\":\"$IMAGE_URL\"}}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"]}}"
else
  echo "i2i-url probes        skipped (IMAGE_URL not set)"
fi

if [ -n "$IMAGE_FILE" ]; then
  B64=$(base64 -i "$IMAGE_FILE" | tr -d '\n')
elif [ -n "$IMAGE_URL" ]; then
  B64=$(curl -sS --max-time 60 "$IMAGE_URL" | base64 | tr -d '\n')
else
  B64=""
fi

if [ -n "$B64" ]; then
  echo "inline payload:       $(( ${#B64} / 1024 / 1024 )) MB encoded"
  post i2i-inline-b64 "{\"contents\":[{\"role\":\"user\",\"parts\":[{\"text\":\"$PROMPT_I2I\"},{\"inline_data\":{\"mime_type\":\"image/jpeg\",\"data\":\"$B64\"}}]}],\"generationConfig\":{\"responseModalities\":[\"IMAGE\"]}}"
else
  echo "i2i-inline probe      skipped (no IMAGE_FILE / IMAGE_URL)"
fi

# --- what the model list actually accepts ----------------------------------
post model-listing '{"contents":[{"role":"user","parts":[{"text":"ping"}]}],"generationConfig":{"responseModalities":["TEXT"]}}'

echo
echo "raw replies: $OUT/*.json"
echo "read usage from any successful reply: python3 -m json.tool $OUT/t2i-imageConfig-1K.json | head -40"
