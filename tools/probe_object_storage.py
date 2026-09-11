#!/usr/bin/env python3
"""Probe object-storage backends for the wire contract the adapter's storage
layer would need, so the abstraction is designed against measured facts.

It answers, per backend, the questions that decide whether ONE parameterised
S3 client can serve MinIO / OSS / COS / TOS -- or whether each vendor needs
its own SDK:

  1. addressing style -- is path-style or virtual-hosted style accepted?
     (minio-py defaults to virtual-hosted ONLY for AWS hosts and for hosts
     ending in ``aliyuncs.com``; see helpers.BaseURL.__init__. COS and TOS
     are therefore path-style unless switched explicitly.)
  2. region            -- does passing ``region=`` explicitly work, so that
     GetBucketLocation is never needed?
  3. presign           -- does ``presigned_get_object()`` produce a URL a
     plain HTTP client (i.e. an upstream model provider) can actually fetch?
     This is the step that catches a vendor's public-domain policy.
  4. cleanup           -- can the probe remove what it wrote?

fal's CDN is not S3 at all, and it has TWO upload paths that are easy to
confuse. The ``fal-client`` SDK's PRIMARY path is a CDN-token flow
(``POST rest.fal.ai/storage/auth/token?storage_type=fal-cdn-v3`` for a
short-lived token, then ``POST v3.fal.media/files/upload``). The
``POST /storage/upload/initiate`` REST endpoint -- the one the public docs
lead you to hand-roll -- is only the SDK's FALLBACK repository (``fal``),
reached when the v3 CDN fails.

So the probe exercises the SDK path first, because that is what we would
ship, and because the raw REST endpoint cannot express what the SDK can:
``initial_acl`` (``hide`` / ``forbid`` / ``allow``), the only way to make a
fal object non-public. It then checks the REST fallback, and finally whether
an uploaded object is readable with no credentials at all -- the fact behind
the storage port's ``visibility`` field.

Retention is deliberately NOT probed with an override. The decision is to
send no ``expires_in`` at all and inherit fal's account-level default (the
longest window available), so the upload steps below carry no lifecycle
field -- and there is consequently nothing about retention left to measure.

Usage
-----
Configure one or more backends; unconfigured ones are skipped.

  export PROBE_OSS_ENDPOINT=s3.oss-cn-hangzhou.aliyuncs.com
  export PROBE_OSS_REGION=cn-hangzhou
  export PROBE_OSS_BUCKET=my-bucket
  export PROBE_OSS_ACCESS_KEY=...
  export PROBE_OSS_SECRET_KEY=...
  export PROBE_FAL_KEY=...

  .venv/bin/python tools/probe_object_storage.py

Security note: the credentials above are read from the environment and are
never printed or written to disk. Only their presence is reported.

Safety
------
Writes exactly one small object under a ``probe/`` prefix and deletes it in a
``finally`` block. It never lists or deletes anything it did not create, and
it never touches bucket lifecycle rules. fal has no delete on this API
surface, so a fal probe leaves a 1x1 transparent PNG on a public CDN -- know
that before running it.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import timedelta

#: A 1x1 transparent PNG, 70 bytes. Small enough to be free, contentless
#: enough that leaving it behind on a public CDN is not a disclosure.
PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

#: Kept short on purpose. A probe that hangs teaches nothing, and GET
#: BucketLocation against a vendor that does not implement it is exactly the
#: hang this bounds.
CONNECT_TIMEOUT = float(os.environ.get("PROBE_CONNECT_TIMEOUT", "10"))
READ_TIMEOUT = float(os.environ.get("PROBE_READ_TIMEOUT", "20"))
PRESIGN_TTL = int(os.environ.get("PROBE_PRESIGN_TTL", "300"))

#: The SDK's own REST base is rest.fal.ai; the public OpenAPI spec for the
#: upload endpoints advertises rest.alpha.fal.ai. Which host actually serves
#: /storage/upload/initiate is unverified, so the probe asks both rather than
#: betting on one.
FAL_REST_HOSTS = ("https://rest.fal.ai", "https://rest.alpha.fal.ai")


def _env(prefix: str, key: str, default: str = "") -> str:
    return os.environ.get(f"PROBE_{prefix}_{key}", default).strip()


def _cfg(prefix: str) -> dict:
    """Read one backend's config. No defaults are guessed: an endpoint the
    probe invent would turn 'unconfigured' into 'silently probing the wrong
    host', which is the one failure mode a probe must not have."""
    return {
        "prefix": prefix,
        "endpoint": _env(prefix, "ENDPOINT"),
        "region": _env(prefix, "REGION") or None,
        "bucket": _env(prefix, "BUCKET"),
        "access_key": _env(prefix, "ACCESS_KEY"),
        "secret_key": _env(prefix, "SECRET_KEY"),
        "secure": _env(prefix, "SECURE", "true").lower()
        not in ("0", "false", "no"),
    }


def _short(exc: BaseException) -> str:
    """Flatten an SDK exception to something that fits in a table cell.

    minio's S3Error carries the vendor's XML error code, which is the single
    most useful thing a failed probe can report -- ``AccessDenied`` and
    ``SignatureDoesNotMatch`` mean very different next steps.
    """
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    if code:
        return f"{type(exc).__name__}({code}): {message[:150]}"
    return f"{type(exc).__name__}: {message[:150]}"


def _http_get(url: str, headers: dict | None = None) -> tuple[int | None, int, str]:
    """Plain HTTP GET with no SDK involved. Returns (status, bytes, error).

    Deliberately SDK-free: the question is whether an *upstream provider*
    can fetch this URL, and an upstream uses a plain HTTP client, not the
    same SDK that minted the signature.
    """
    request = urllib.request.Request(url, method="GET", headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as resp:
            body = resp.read()
            return resp.status, len(body), ""
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200]
        return exc.code, 0, f"HTTP {exc.code}: {detail!r}"
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        return None, 0, f"{type(exc).__name__}: {exc}"


def _s3_client(cfg: dict, style: str, region: str | None):
    """Build a minio client pinned to one addressing style.

    ``enable_virtual_style_endpoint`` / ``disable_virtual_style_endpoint`` are
    public API (minio/api.py) that flip the same flag the constructor derives
    from the hostname -- this is the switch that makes one implementation
    usable across vendors with opposite addressing requirements.
    """
    import certifi
    import urllib3
    from minio import Minio

    pool = urllib3.PoolManager(
        timeout=urllib3.Timeout(connect=CONNECT_TIMEOUT, read=READ_TIMEOUT),
        maxsize=4,
        cert_reqs="CERT_REQUIRED",
        ca_certs=os.environ.get("SSL_CERT_FILE") or certifi.where(),
        # retries=0 on purpose: a retried 403 or signature failure hides
        # which address style actually produced it.
        retries=urllib3.Retry(total=0),
    )
    client = Minio(
        cfg["endpoint"],
        access_key=cfg["access_key"],
        secret_key=cfg["secret_key"],
        secure=cfg["secure"],
        region=region,
        http_client=pool,
    )
    if style == "virtual":
        client.enable_virtual_style_endpoint()
    else:
        client.disable_virtual_style_endpoint()
    return client


def _s3_body(
    cfg: dict, key: str, style: str, region: str | None, steps: list, state: dict
) -> None:
    """Run the five steps, gating each on the previous one.

    Gating matters: PUT after a failed bucket_exists would report a second,
    derived failure that buries the first real cause.
    """
    client = _s3_client(cfg, style, region)
    state["client"] = client

    try:
        client.bucket_exists(cfg["bucket"])
    except Exception as exc:  # noqa: BLE001
        steps.append(("bucket_exists", False, _short(exc)))
        return
    steps.append(("bucket_exists", True, ""))

    try:
        client.put_object(
            cfg["bucket"],
            key,
            io.BytesIO(PNG_1X1),
            len(PNG_1X1),
            content_type="image/png",
        )
    except Exception as exc:  # noqa: BLE001
        steps.append(("put_object", False, _short(exc)))
        return
    state["wrote"] = True
    steps.append(("put_object", True, ""))

    try:
        url = client.presigned_get_object(
            cfg["bucket"], key, expires=timedelta(seconds=PRESIGN_TTL)
        )
    except Exception as exc:  # noqa: BLE001
        steps.append(("presigned_get_object", False, _short(exc)))
        return
    # The presigned host IS the addressing style, observed rather than assumed.
    state["host"] = urllib.parse.urlsplit(url).netloc
    steps.append(("presigned_get_object", True, f"host={state['host']}"))

    status, size, error = _http_get(url)
    ok = status == 200 and size == len(PNG_1X1)
    steps.append(
        ("fetch_presigned_no_sdk", ok, error or f"HTTP {status}, {size} bytes")
    )


def _s3_cleanup(cfg: dict, key: str, steps: list, state: dict) -> None:
    if not state.get("wrote") or state.get("client") is None:
        return
    try:
        state["client"].remove_object(cfg["bucket"], key)
        steps.append(("remove_object", True, ""))
    except Exception as exc:  # noqa: BLE001
        steps.append(("remove_object", False, _short(exc)))


def _s3_combo(cfg: dict, style: str, region: str | None) -> dict:
    steps: list[tuple[str, bool, str]] = []
    state: dict = {"wrote": False, "client": None, "host": ""}
    key = f"probe/{uuid.uuid4().hex}.png"
    try:
        _s3_body(cfg, key, style, region, steps, state)
    except Exception as exc:  # noqa: BLE001
        steps.append(("<unexpected>", False, _short(exc)))
    finally:
        # Cleanup happens before the verdict is computed, so remove_object
        # counts toward it -- a backend that cannot delete is a real finding.
        _s3_cleanup(cfg, key, steps, state)
    return {
        "steps": steps,
        "host": state["host"],
        "ok": bool(steps) and all(ok for _, ok, _ in steps),
    }


def probe_s3(name: str, cfg: dict) -> dict:
    """Run the addressing-style x region matrix for one backend."""
    if not (cfg["endpoint"] and cfg["bucket"] and cfg["access_key"]):
        return {"configured": False, "combos": {}, "region": cfg["region"]}

    # The region dimension exists because GetBucketLocation is the quiet
    # failure: OSS/COS/TOS do not all implement it, and a client that needs
    # it works only until the vendor changes something.
    regions: list[str | None] = [cfg["region"]]
    if cfg["region"] is not None:
        regions.append(None)

    combos: dict[str, dict] = {}
    for style in ("path", "virtual"):
        for region in regions:
            label = f"{style} / region={region or 'auto'}"
            combos[label] = _s3_combo(cfg, style, region)
    return {"configured": True, "combos": combos, "region": cfg["region"]}


async def _fal_sdk_steps(api_key: str, steps: list) -> None:
    """Exercise the SDK's primary path, plus the one capability the raw REST
    endpoint cannot express at all: ``initial_acl``."""
    try:
        from fal_client import AsyncClient, StorageACL, StorageSettings
        from fal_client import __version__ as fal_version
    except ImportError as exc:
        steps.append(
            ("import fal_client", False, f"{type(exc).__name__}: {exc} -- pip install fal-client")
        )
        return
    steps.append(("import fal_client", True, f"v{fal_version}"))

    # `key=` is a constructor field, so the credential is injected per
    # channel instead of read from a global FAL_KEY. That is what makes the
    # SDK usable in a multi-tenant adapter at all.
    client = AsyncClient(key=api_key)

    async def upload(label: str, **kwargs) -> str | None:
        try:
            url = await client.upload(PNG_1X1, "image/png", **kwargs)
        except Exception as exc:  # noqa: BLE001
            steps.append((label, False, _short(exc)))
            return None
        steps.append((label, True, f"host={urllib.parse.urlsplit(url).netloc}"))
        return url

    def observe(label: str, url: str) -> None:
        """Record what the server actually did, rather than asserting what
        the docs imply -- the ACL semantics in particular are documented only
        as three words ('hide', 'forbid', 'allow')."""
        status, size, error = _http_get(url)
        steps.append(
            (label, True, error or f"HTTP {status}, {size} bytes -> public={status == 200}")
        )

    # No `lifecycle=` here, and that IS the shipped behaviour: omitting
    # `expires_in` means fal's account-level retention applies, which is the
    # longest window on offer. Nothing to override, so nothing to measure
    # beyond the upload succeeding.
    plain = await upload("sdk upload (fal_v3, no lifecycle override)")
    if plain:
        observe("sdk fetch no-auth (default acl)", plain)

    hidden = await upload(
        "sdk upload (initial_acl=forbid)",
        lifecycle=StorageSettings(initial_acl=StorageACL(default="forbid")),
    )
    if hidden:
        observe("sdk fetch no-auth (acl forbid)", hidden)


def _fal_rest_steps(api_key: str, steps: list) -> None:
    """The SDK's FALLBACK repository: POST /storage/upload/initiate -> PUT.

    Worth probing separately because it is the path the public docs describe,
    so it is the one most likely to be hand-rolled -- and a hand-rolled
    version would be silently using the fallback while believing it is the
    primary. The probe asks both REST hosts, because the OpenAPI spec
    advertises rest.alpha.fal.ai while the SDK uses rest.fal.ai.

    Unlike the SDK steps above, this one DOES set a short lifecycle. Two
    reasons: the shipped code will never take this path, so fidelity is moot;
    and fal offers no delete, so an expiry is the only way to stop the probe
    from leaving a permanent object on a public CDN.
    """
    body = json.dumps(
        {"file_name": f"probe-{uuid.uuid4().hex}.png", "content_type": "image/png"}
    ).encode()
    for host in FAL_REST_HOSTS:
        request = urllib.request.Request(
            f"{host}/storage/upload/initiate",
            data=body,
            method="POST",
            headers={
                # fal uses "Key", not "Bearer" -- a 401 here is usually this.
                "Authorization": f"Key {api_key}",
                "Content-Type": "application/json",
                "X-Fal-Object-Lifecycle-Preference": "expiration_duration_seconds=600",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=READ_TIMEOUT) as resp:
                payload = json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            steps.append((f"rest initiate @ {host}", False, _short(exc)))
            continue

        steps.append(
            (
                f"rest initiate @ {host}",
                True,
                f"file_host={urllib.parse.urlsplit(payload['file_url']).netloc}",
            )
        )
        put = urllib.request.Request(
            payload["upload_url"],
            data=PNG_1X1,
            method="PUT",
            headers={"Content-Type": "image/png"},
        )
        try:
            with urllib.request.urlopen(put, timeout=READ_TIMEOUT) as resp:
                steps.append(("rest put_bytes", resp.status in (200, 201), f"HTTP {resp.status}"))
        except Exception as exc:  # noqa: BLE001
            steps.append(("rest put_bytes", False, _short(exc)))
            return

        status, _, error = _http_get(payload["file_url"])
        steps.append(("rest fetch no-auth", True, error or f"HTTP {status} -> public={status == 200}"))
        return


def probe_fal(api_key: str) -> dict:
    """SDK primary path, then the REST fallback, then observed visibility."""
    steps: list[tuple[str, bool, str]] = []
    if not api_key:
        return {"configured": False, "steps": steps}
    asyncio.run(_fal_sdk_steps(api_key, steps))
    _fal_rest_steps(api_key, steps)
    return {"configured": True, "steps": steps}


def _print_s3(name: str, result: dict) -> None:
    print(f"\n=== {name} ===")
    if not result["configured"]:
        print("  skipped (set PROBE_%s_ENDPOINT / _BUCKET / _ACCESS_KEY)" % name.upper())
        return
    print(f"  configured region: {result['region'] or '(none given)'}")
    for label, combo in result["combos"].items():
        verdict = "PASS" if combo["ok"] else "FAIL"
        print(f"  [{verdict}] {label}")
        for step, ok, detail in combo["steps"]:
            mark = "ok  " if ok else "FAIL"
            suffix = f"  {detail}" if detail else ""
            print(f"       {mark} {step}{suffix}")


def _print_fal(result: dict) -> None:
    print("\n=== fal ===")
    if not result["configured"]:
        print("  skipped (set PROBE_FAL_KEY)")
        return
    for step, ok, detail in result["steps"]:
        mark = "ok  " if ok else "FAIL"
        suffix = f"  {detail}" if detail else ""
        print(f"  {mark} {step}{suffix}")
    # fal exposes no delete on either path, so every successful upload is a
    # permanent artifact. Say so with a count rather than burying it.
    uploaded = sum(1 for step, ok, _ in result["steps"] if ok and "upload" in step)
    if uploaded:
        print(
            f"  note: {uploaded} probe object(s) left on fal's CDN "
            "(contentless 1x1 PNG; no delete on this API)"
        )


def main() -> int:
    print("object-storage probe")
    print("credentials are read from the environment and never printed")

    results: dict[str, dict] = {}
    for name in ("minio", "oss", "cos", "tos"):
        cfg = _cfg(name.upper())
        results[name] = probe_s3(name, cfg)
        _print_s3(name, results[name])

    fal = probe_fal(os.environ.get("PROBE_FAL_KEY", "").strip())
    _print_fal(fal)

    print("\n--- summary ---")
    any_pass = False
    for name, result in results.items():
        if not result["configured"]:
            continue
        passing = [label for label, combo in result["combos"].items() if combo["ok"]]
        any_pass = any_pass or bool(passing)
        print(f"  {name}: {', '.join(passing) if passing else 'no combination passed'}")
    if fal["configured"]:
        sdk_ok = any(
            ok and step.startswith("sdk upload (fal_v3") for step, ok, _ in fal["steps"]
        )
        any_pass = any_pass or sdk_ok
        print(f"  fal: {'sdk primary path ok' if sdk_ok else 'sdk primary path failed'}")

    return 0 if any_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
