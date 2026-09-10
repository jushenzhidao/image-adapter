"""Mock upstream service (AC-16): four modes.

1. Sync text2img/chat endpoints (vendor-a style)
2. Async Job submit + query endpoints (vendor-b style)
3. AK-SK signature validation
4. OpenAI-native images: /v1/images/generations (JSON) and /v1/images/edits
   (multipart), the two-endpoint shape openai/images@v1 targets

Returns real 1x1 PNG bytes for all image endpoints.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import time
import uuid
from collections.abc import Callable

from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

# 1x1 red PNG (real bytes, not placeholder)
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg=="
)

_jobs: dict[str, dict] = {}

#: What the last OpenAI-shaped call looked like, so a manual E2E can assert on
#: the wire shape (JSON vs multipart, which parts arrived) without a debugger.
_openai_last: dict = {}


def _verify_ak_sk(request: Request) -> bool:
    """Validates AK-SK signature for vendor-b endpoints."""
    ak = request.headers.get("X-Access-Key", "")
    timestamp = request.headers.get("X-Timestamp", "")
    signature = request.headers.get("X-Signature", "")
    
    if not all([ak, timestamp, signature]):
        return False
    
    sk = "test-sk" if ak == "test-ak" else ""
    method = request.method
    path = request.url.path
    
    string_to_sign = f"{method}\n{path}\n{timestamp}"
    expected_sig = hashlib.sha256(f"{string_to_sign}{sk}".encode()).hexdigest()
    
    return signature == expected_sig


# Vendor A: synchronous endpoints
async def vendor_a_chat(request: Request) -> Response:
    body = json.loads(await request.body())
    prompt = body.get("prompt", "")
    
    return JSONResponse({
        "reply": f"Mock response to: {prompt[:50]}",
        "usage": {"prompt_tokens": 10, "completion_tokens": 20},
    })


async def vendor_a_text2img(request: Request) -> Response:
    body = json.loads(await request.body())
    description = body.get("description", "")

    return Response(content=PNG_1X1, media_type="image/png")


async def vendor_a_img2img(request: Request) -> Response:
    """Image-to-image: echoes back what it was given, so tests can assert on
    the shape the script actually forwarded."""
    body = json.loads(await request.body())
    image = body.get("image")

    if not image:
        return JSONResponse(
            {"error": {"message": "image is required"}}, status_code=400
        )

    refs = image if isinstance(image, list) else [image]
    return JSONResponse(
        {
            "created": int(time.time()),
            "mode": "img2img",
            "received": {
                "count": len(refs),
                "kinds": [
                    "url" if str(r).startswith("http") else "b64" for r in refs
                ],
                "has_data_uri": any(str(r).startswith("data:") for r in refs),
                "prompt": body.get("prompt", ""),
            },
            "data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}],
        }
    )


async def vendor_a_asset(request: Request) -> Response:
    """Serves a real PNG so ctx.download_image has something to fetch."""
    return Response(content=PNG_1X1, media_type="image/png")


# Mode 4: OpenAI-native images. Two endpoints, split by transport -- JSON for
# generations, multipart for edits -- which is exactly the shape
# openai/images@v1 has to route between.
async def openai_generations(request: Request) -> Response:
    body = json.loads(await request.body() or b"{}")
    _openai_last.clear()
    _openai_last.update(
        {
            "endpoint": "generations",
            "content_type": request.headers.get("content-type", ""),
            "body": body,
        }
    )
    return JSONResponse(
        {
            "created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}],
            "usage": {"total_tokens": 100},
        }
    )


async def openai_edits(request: Request) -> Response:
    form = await request.form()
    fields: dict[str, str] = {}
    files: dict[str, list[dict]] = {}
    for key in form:
        for value in form.getlist(key):
            if isinstance(value, UploadFile):
                data = await value.read()
                files.setdefault(key, []).append(
                    {
                        "filename": value.filename,
                        "content_type": value.content_type,
                        "bytes": len(data),
                    }
                )
            else:
                fields[key] = value
    _openai_last.clear()
    _openai_last.update(
        {
            "endpoint": "edits",
            "content_type": request.headers.get("content-type", ""),
            "fields": fields,
            "files": files,
        }
    )
    return JSONResponse(
        {
            "created": int(time.time()),
            "data": [{"b64_json": base64.b64encode(PNG_1X1).decode("ascii")}],
        }
    )


async def mock_last_openai(request: Request) -> Response:
    """Inspection hook: what the last OpenAI-shaped call carried."""
    return JSONResponse(_openai_last)


# Vendor B: async Job-based endpoints
async def vendor_b_chat_submit(request: Request) -> Response:
    if not _verify_ak_sk(request):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    
    body = json.loads(await request.body())
    query = body.get("query", "")
    
    job_id = f"job-{uuid.uuid4().hex[:16]}"
    _jobs[job_id] = {
        "status": "running",
        "type": "chat",
        "query": query,
        "created_at": time.time(),
    }
    
    return JSONResponse({"job_id": job_id})


async def vendor_b_images_submit(request: Request) -> Response:
    if not _verify_ak_sk(request):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    
    body = json.loads(await request.body())
    prompt = body.get("prompt", "")
    
    job_id = f"job-{uuid.uuid4().hex[:16]}"
    _jobs[job_id] = {
        "status": "running",
        "type": "images",
        "prompt": prompt,
        "created_at": time.time(),
    }
    
    return JSONResponse({"job_id": job_id})


async def vendor_b_job_status(request: Request) -> Response:
    if not _verify_ak_sk(request):
        return JSONResponse({"error": "Invalid signature"}, status_code=401)
    
    job_id = request.path_params["job_id"]
    job = _jobs.get(job_id)
    
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    
    elapsed = time.time() - job["created_at"]
    
    # Simulate Job completing after 1 second
    if elapsed < 1.0:
        return JSONResponse({"status": "running", "job_id": job_id})
    
    if job["type"] == "chat":
        result = {
            "answer": f"Mock answer to: {job['query'][:50]}",
            "usage": {"tokens": 30},
        }
    else:
        result = {
            "image": base64.b64encode(PNG_1X1).decode("ascii"),
        }
    
    return JSONResponse({"status": "completed", "job_id": job_id, "result": result})


routes = [
    Route("/v2/chat", vendor_a_chat, methods=["POST"]),
    Route("/v2/text2img", vendor_a_text2img, methods=["POST"]),
    Route("/v2/img2img", vendor_a_img2img, methods=["POST"]),
    Route("/assets/sample.png", vendor_a_asset, methods=["GET"]),
    Route("/v1/images/generations", openai_generations, methods=["POST"]),
    Route("/v1/images/edits", openai_edits, methods=["POST"]),
    Route("/mock/last-openai", mock_last_openai, methods=["GET"]),
    Route("/v1/chat/submit", vendor_b_chat_submit, methods=["POST"]),
    Route("/v1/images/submit", vendor_b_images_submit, methods=["POST"]),
    Route("/v1/jobs/{job_id:str}", vendor_b_job_status, methods=["GET"]),
]

app = Starlette(routes=routes)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9100, log_level="info")
