"""Unit tests for tools/identity_service.py -- the qwen identity provider.

Two things are pinned here, and they are pinned for different reasons:

  1. `assemble_identity` -- the vendor's shapes (the umid hides in `lswusea`
     behind an "@@timestamp", the cookie jar has to be filtered to qwen.ai,
     `bx-ua` only counts when the page's own script produced it) and the rule
     that a partial identity is *refused* rather than handed out. A stale or
     half identity silently spends the little quota it has left, which is the
     failure this service exists to prevent.
  2. the pool and the HTTP contract -- rotation, retirement at the measured
     per-identity ceiling, and a 503 that *says why* when there is nothing to
     hand out, since the adapter turns that into a failed request.

No browser, no network, no Playwright: the minter is injected. The HTTP tests
talk over a real loopback socket with `http.client`, which is deliberate --
`urllib`/`requests` pick up the macOS system proxy and would send a request for
127.0.0.1 to an external proxy.
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import threading
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[2] / "tools" / "identity_service.py"


def _load():
    spec = importlib.util.spec_from_file_location("identity_service", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


svc = _load()

QWEN_COOKIES = [
    {"name": "aui", "value": "300f8a0e", "domain": ".chat.qwen.ai"},
    {"name": "ssxmod_itna", "value": "abc", "domain": "chat.qwen.ai"},
    {"name": "tracker", "value": "no", "domain": ".example.com"},
]
UMID_RAW = "T2gAc4oFxxxxxxxxxxxxxxxx-xxxxxxxx=@@1758096000"
BXUA_OK = {"ok": True, "token": "234!teOeK..."}


def _identity(n=0):
    return {"cookie": f"aui=id{n}", "bx_ua": f"234!id{n}", "bx_umidtoken": f"T2gA{n}"}


# ------------------------------------------------------- 组装（vendor 形状）

def test_assemble_splits_umid_and_filters_the_jar():
    got = svc.assemble_identity(UMID_RAW, BXUA_OK, QWEN_COOKIES)
    assert got["bx_umidtoken"] == "T2gAc4oFxxxxxxxxxxxxxxxx-xxxxxxxx="
    assert "aui=300f8a0e" in got["cookie"] and "ssxmod_itna=abc" in got["cookie"]
    assert "tracker" not in got["cookie"]          # 别的域名的 cookie 不进 jar
    assert got["bx_ua"] == "234!teOeK..."


def test_assemble_requires_all_three_fields():
    with pytest.raises(svc.MintError) as missing_umid:
        svc.assemble_identity("", BXUA_OK, QWEN_COOKIES)
    assert "bx_umidtoken" in str(missing_umid.value)

    with pytest.raises(svc.MintError) as missing_jar:
        svc.assemble_identity(UMID_RAW, BXUA_OK, [])
    assert "cookie" in str(missing_jar.value)


def test_assemble_reports_why_the_token_mint_failed():
    """账号/指纹齐全但 bx-ua 没算出来 —— 报错要带页面的原话，否则无从排查。"""
    with pytest.raises(svc.MintError) as err:
        svc.assemble_identity(UMID_RAW, {"ok": False, "err": "no AWSC"}, QWEN_COOKIES)
    assert "bx_ua" in str(err.value) and "no AWSC" in str(err.value)


# ------------------------------------------------------------------- 池策略

def test_take_rotates_instead_of_reusing_one_identity():
    """轮换的意义是连续请求不烧同一份身份的额度，不是每请求都用全新身份。"""
    pool = svc.IdentityPool(lambda: _identity(0), target=1, max_uses=5)
    for n in range(3):
        pool.stock(_identity(n))
    assert [pool.take()["bx_umidtoken"] for _ in range(4)] == [
        "T2gA0", "T2gA1", "T2gA2", "T2gA0"]


def test_identity_is_retired_at_the_measured_ceiling():
    pool = svc.IdentityPool(lambda: _identity(), target=1, max_uses=2)
    pool.stock(_identity())
    pool.take()
    pool.take()
    assert pool.depth() == 0
    assert pool.retired == 1
    with pytest.raises(svc.PoolExhausted) as empty:
        pool.take()
    assert "no usable identity" in str(empty.value)


def test_identity_is_retired_after_its_ttl():
    now = [1000.0]
    pool = svc.IdentityPool(lambda: _identity(), target=1, max_uses=99,
                            ttl=60.0, clock=lambda: now[0])
    pool.stock(_identity())
    now[0] += 61.0
    assert pool.depth() == 0
    with pytest.raises(svc.PoolExhausted):
        pool.take()


def test_empty_pool_says_why_it_is_empty():
    """503 的文案要带上最后一次铸造失败的原因 —— 适配器只会看到这一句话。"""
    pool = svc.IdentityPool(lambda: (_ for _ in ()).throw(
        svc.MintError("incomplete identity, missing bx_ua")), target=1)
    pool._refill_once()
    assert pool.depth() == 0
    assert "bx_ua" in (pool.last_error or "")
    with pytest.raises(svc.PoolExhausted) as empty:
        pool.take()
    assert "bx_ua" in str(empty.value)


def test_refill_stops_at_the_target():
    calls = []

    def mint():
        calls.append(1)
        return _identity(len(calls))

    pool = svc.IdentityPool(mint, target=3)
    pool._refill_once()
    assert pool.depth() == 3 and pool.minted == 3
    pool._refill_once()                      # 已经满了
    assert pool.minted == 3


def test_refill_survives_a_failed_mint_and_heals():
    """一次铸造失败不能让填充循环退出：下一个身份很可能成功，而原因留在 /health。"""
    attempts = []

    def mint():
        attempts.append(1)
        if len(attempts) == 1:
            raise svc.MintError("browser flaked")
        return _identity()

    errors = []
    pool = svc.IdentityPool(mint, target=1, on_error=errors.append)
    pool._refill_once()
    assert pool.depth() == 0 and "browser flaked" in (pool.last_error or "")
    assert len(errors) == 1
    pool._refill_once()
    assert pool.depth() == 1 and pool.last_error is None


def test_background_refill_fills_the_pool():
    pool = svc.IdentityPool(lambda: _identity(), target=2, refill_interval=0.01)
    pool.start()
    try:
        deadline = time.time() + 5
        while pool.depth() < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert pool.depth() == 2
    finally:
        pool.stop()


# --------------------------------------------------------------- HTTP 契约

@pytest.fixture
def service():
    """真实回环端口 + 假铸币厂；用 http.client（不读系统代理）。"""
    servers = []

    def _serve(pool, token=""):
        server = svc.build_server(pool, host="127.0.0.1", port=0, token=token)
        threading.Thread(target=server.serve_forever, daemon=True).start()

        def call(path):
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                              timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            body = json.loads(resp.read().decode("utf-8"))
            conn.close()
            return resp.status, body

        def headers_of(path):
            """响应头（小写键）。挂在 call 上，免得每个用例都改解包形式。"""
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port,
                                              timeout=5)
            conn.request("GET", path)
            resp = conn.getresponse()
            resp.read()
            found = {name.lower(): value for name, value in resp.getheaders()}
            conn.close()
            return found

        call.headers_of = headers_of
        servers.append(server)
        return call

    yield _serve
    for server in servers:
        server.shutdown()
        server.server_close()


def test_identity_endpoint_answers_the_scripts_contract(service):
    pool = svc.IdentityPool(lambda: _identity(), target=1)
    pool.stock(_identity(7))
    call = service(pool)
    status, body = call("/identity")
    assert status == 200
    # 正是脚本 _maybe_fresh_identity 合并的三个字段
    assert set(body) == {"cookie", "bx_ua", "bx_umidtoken"}
    assert body["bx_umidtoken"] == "T2gA7"


def test_empty_pool_is_503_not_a_stale_identity(service):
    pool = svc.IdentityPool(lambda: _identity(), target=1)
    call = service(pool)
    status, body = call("/identity")
    assert status == 503 and "no usable identity" in body["error"]


def test_503_says_when_to_come_back(service):
    """503 必须自带 Retry-After：一次铸造 ~7-10s，更短的建议只会换来第二次 503。"""
    pool = svc.IdentityPool(lambda: _identity(), target=1)
    call = service(pool)
    headers = call.headers_of("/identity")
    assert headers["retry-after"] == str(svc.RETRY_AFTER_SECONDS)
    assert svc.RETRY_AFTER_SECONDS >= 7      # 与实测的铸造成本对齐


# ------------------------------------------------------------ 就绪（预热）

def test_warm_up_returns_as_soon_as_the_pool_is_ready():
    pool = svc.IdentityPool(lambda: _identity(), target=1, refill_interval=0.01)
    pool.start()
    try:
        assert svc.warm_up(pool, want=1, timeout=5, interval=0.01) is True
    finally:
        pool.stop()


def test_warm_up_gives_up_so_the_service_still_starts():
    """预热失败不能让服务拒绝启动：一个说明原因的 503 比起不来好。"""
    pool = svc.IdentityPool(lambda: (_ for _ in ()).throw(svc.MintError("nope")),
                            target=1, refill_interval=0.01)
    pool.start()
    try:
        assert svc.warm_up(pool, want=1, timeout=0.2, interval=0.05) is False
    finally:
        pool.stop()


def test_main_waits_for_the_pool_before_serving(monkeypatch):
    """接线也要钉：预热的参数没接上，就等于这件事没做（而它不会自己报警）。"""
    seen: list[dict] = []

    class _Server:
        def serve_forever(self):
            raise KeyboardInterrupt          # 立刻退出，不进真循环

        def server_close(self):
            pass

    monkeypatch.setattr(svc, "build_server", lambda pool, **kw: _Server())
    monkeypatch.setattr(svc, "warm_up",
                        lambda pool, **kw: (seen.append(kw), True)[1])
    assert svc.main(["--pool", "3", "--warmup", "2",
                     "--warmup-timeout", "7"]) == 0
    assert seen and seen[0]["want"] == 2 and seen[0]["timeout"] == 7


def test_health_reports_the_pool(service):
    pool = svc.IdentityPool(lambda: _identity(), target=4, max_uses=4)
    pool.stock(_identity())
    call = service(pool)
    status, body = call("/health")
    assert status == 200
    assert body["ready"] == 1 and body["target"] == 4
    assert body["max_uses"] == 4 and "last_error" in body


def test_token_gate_accepts_query_or_header(service):
    pool = svc.IdentityPool(lambda: _identity(), target=1)
    pool.stock(_identity())
    call = service(pool, token="s3cret")
    assert call("/identity")[0] == 401
    assert call("/identity?token=nope")[0] == 401
    assert call("/identity?token=s3cret")[0] == 200   # 脚本只能给 URL
    assert call("/health?token=s3cret")[0] == 200


def test_unknown_path_is_404(service):
    call = service(svc.IdentityPool(lambda: _identity(), target=1))
    status, body = call("/nope")
    assert status == 404 and body["path"] == "/nope"


def test_playwright_absent_names_the_install_command():
    """缺 Playwright 时要报出怎么装，而不是一个空身份或 500。"""
    if importlib.util.find_spec("playwright") is not None:
        pytest.skip("playwright 已安装：这条只钉缺依赖时的文案")
    minter = svc.BrowserMinter()
    with pytest.raises(svc.MintError) as err:
        minter._ensure_browser()
    assert "playwright install" in str(err.value)
