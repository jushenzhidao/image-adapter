# Spec - OpenAI Images 适配微服务 (image-adapter) v1.0

> 生成日期：2026-09-04
> 基于：01_解决方案.md v1.0 + 02_业务架构.md v1.0 + 03_技术架构.md v1.0
> 状态：已确认（用户已选定：三端点完整骨架 + Mock 上游 + Logfire 真实 Token）

---

## 1. 产品定义

- **一句话描述**：OpenAI 协议兼容网关，把任意异构图像/多模态上游统一转换为 OpenAI 标准三端点。
- **目标用户**：客户端开发者（用 OpenAI SDK 无感调用）、平台运营者（写脚本接上游）、上游接入方。
- **核心问题**：异构上游协议/认证/异步模式/输出格式五花八门，客户端集成成本高。

## 2. MVP 范围（锁定——不在此列表的功能一律不做）

| 优先级 | 功能 | 验收标准摘要 |
|--------|------|-------------|
| P0 | `POST /v1/images`：字段映射、批量 n>1 聚合、url/b64_json 双输出 | AC-01~04 |
| P0 | `POST /v1/chat/completions`：文本 + Vision 输入、非流式 + SSE 流式桥接 | AC-05~07 |
| P0 | `POST /v1/responses`：input 转换、image_generation 工具模拟编排、previous_response_id 状态链 | AC-08~10 |
| P0 | 脚本引擎：importlib 动态加载 + AST 安全扫描 + 模块缓存 + watchdog 热重载 | AC-11~13 |
| P0 | 异步状态机：Job 轮询（interval/timeout 可配）+ Redis 分布式锁 | AC-14 |
| P0 | 上游注册表：YAML 配置 model→upstream+endpoint 路由 | AC-15 |
| P0 | Mock 上游服务：同步文生图 / 异步 Job / AK-SK 签名 三种模式 | AC-16 |
| P0 | 示例脚本：upstream_a(同步)、upstream_b(异步) 全端点脚本 | 随 AC-01~10 验证 |
| P0 | Logfire 全链路埋点（token 从环境变量读取，未配置时本地降级） | AC-17 |
| P1 | `GET /health` 健康检查（含 Redis/对象存储依赖状态） | AC-18 |
| P1 | 中间件：鉴权（内部 Token）、日志、CORS；限流（Redis 滑动窗口） | AC-19 |
| P1 | Docker Compose 编排（adapter + redis + minio + mock-upstream） | AC-20 |
| P1 | 图片处理：URL 下载缓存 5min、对象存储临时图片（生命周期随 `STORAGE_BACKEND`：minio 预签名 1h / fal 公网长期）、Base64 编解码 | AC-03/04 |

### 2.1 v1.1 增量范围：级联编排（前处理 + 生成 + 后处理）

MVP（v1.0）保持锁定不变，以下为 v1.0 验收通过后启动的增量：

| 优先级 | 功能 | 验收标准摘要 |
|--------|------|-------------|
| P0 | 分层超时模型：转换钩子 30s / 单级 120s / 级联总预算 300s，共享 `ctx.deadline` | AC-22、AC-27 |
| P0 | `ctx.image` 图像门面（resize/crop/pad/convert/compress/info/compose_mask），Pillow 不进白名单 | AC-23 |
| P0 | 模块级 `STAGES` 声明 + `phase` 分发（`request:{stage}` / `response:{stage}`） | AC-24 |
| P0 | 向后兼容：未声明 `STAGES` 的脚本走原单级链路，行为零变化 | AC-25 |
| P0 | `ctx.stage` 级间产物传递 + `ctx.emit()` 覆写下一级入参 | AC-26 |
| P0 | 级降级：`STAGE_FALLBACK` 声明的级失败时返回上一级产物并标记 `degraded` | AC-28 |
| P1 | 级维度可观测：`stage` Span + `adapter_stage_*` Metrics | AC-29 |
| P1 | 批量放大控制：`concurrency` 限并发，限流按实际上游调用次数计费 | AC-30 |

## 3. 明确不做（Out-of-Scope — 锁定）

| 不做的功能 | 原因 | 何时考虑 |
|------------|------|----------|
| New API 网关本身 | 现有系统，Adapter 仅作其下游渠道 | 不做 |
| 同构上游透传逻辑 | 由 New API 承载（BR-001） | 不做 |
| K8s / Helm Chart | MVP 用 Docker Compose 足够 | v1.1+ |
| 管理后台 / Web UI | 纯 API 服务，脚本即配置 | 有运营诉求后 |
| 真实厂商上游脚本 | 用户暂无真实上游凭证，Mock 先行 | 拿到 API 文档后照抄示例脚本 |
| 客户端取消（CANCELLED 状态） | 扩展协议，MVP 不实现 | v0.3+ |
| orjson 极致性能优化 | 标准库 json 足够 | 压测后按需 |

## 4. 技术架构（锁定 — 含版本锚定）

> 版本锚定规则：requirements.txt 必须 `==` 精确锁定；下表"锁定版本"由后端在 venv 实际安装后回填确认，禁止凭记忆写版本。

| 层 | 技术 | 锁定版本 | 锁定原因 |
|----|------|----------|----------|
| 运行时 | Python | 3.13.12（managed: `/Users/betterme/.workbuddy/binaries/python/versions/3.13.12/bin/python3`） | 本机托管版本 |
| Web 框架 | FastAPI | pip 安装后回填 | 基于 Starlette；用 `Header()` 声明渠道契约，/docs 可试调 |
| ASGI 服务器 | uvicorn（生产 + gunicorn） | pip 安装后回填 | 多 Worker 绕 GIL |
| HTTP 客户端 | aiohttp | pip 安装后回填 | 异步连接池 + 流式下载 |
| 缓存/状态 | redis-py（`redis.asyncio`，**不用已废弃的 aioredis 包**） | pip 安装后回填 | 官方异步客户端 |
| 对象存储 | 端口 `adapter/storage`；后端 `minio`（默认）/ `fal` | minio 随镜像；fal 走 `fal` extra | 后端协议不同：minio 预签名会过期，fal 公网长期且 SDK 原生异步。新增 OSS/COS/TOS = 一个 builder |
| 图片处理 | Pillow | pip 安装后回填 | 格式转换 |
| 可观测 | logfire | pip 安装后回填 | OTel 标准；`logfire.instrument_fastapi(app)`，**capture_headers=False**（渠道头含凭据与脚本源码） |
| 配置 | pydantic-settings | pip 安装后回填 | 环境变量类型安全 |
| 热重载 | watchdog | pip 安装后回填 | 跨平台文件监听 |
| 测试 | pytest + pytest-asyncio + httpx2(ASGI TestClient) | pip 安装后回填 | 异步测试；starlette 1.6 起以 httpx2 取代 httpx |
| 部署 | Docker + Docker Compose | - | 卷挂载热更新 |
| 图标库 | 不适用（纯 API 服务，无 UI）；任何文档/日志输出禁止 emoji | - | P0-1 |

## 5. API 端点清单（锁定——开发以此为唯一依据）

| Method | Path | 功能 | 认证 | 请求体关键字段 | 响应体 |
|--------|------|------|------|----------------|--------|
| POST | `/v1/images` | 文生图/图生图 | Bearer 内部 Token | model, prompt, n, size, quality, response_format(url/b64_json) | `{created, data:[{url|b64_json}]}` |
| POST | `/v1/chat/completions` | 多模态对话 | Bearer 内部 Token | model, messages(含 image_url content), stream | ChatCompletion / SSE chunks |
| POST | `/v1/responses` | Agent 工具编排 | Bearer 内部 Token | model, input, tools[{type:image_generation}], previous_response_id | `{id, output:[message|image_generation_call], usage}` |
| GET | `/health` | 健康检查 | 无 | - | `{status, deps:{redis, <storage_backend>}}` |

错误格式统一（03_技术架构 §8.2）：`{"error":{"message","type","param","code"}}`；脚本异常不暴露堆栈（BR-009）。

## 6. 数据存储清单（锁定，无关系型数据库）

| 存储 | Key 模式 | 用途 | TTL |
|------|----------|------|-----|
| Redis | `img_cache:{md5(url)}` | 图片下载缓存 | 5min (BR-006) |
| Redis | `job_lock:{job_id}` | 异步轮询分布式锁 | poll_interval+1s |
| Redis | `resp_ctx:{response_id}` | responses 状态链上下文 | 1h |
| Redis | `ratelimit:{token}:{window}` | 滑动窗口限流 | 窗口期 |
| 对象存储 | `[<prefix>/]<yyyymmdd>/{request_id}/{uuid}.{ext}` | 临时图片（键由引擎构造，与后端无关；日期段恒定，前缀默认空 ⇒ 日期落在桶根） | minio：预签名 URL，TTL=`temp_image_ttl`（默认 1h，上限 7d）(BR-005)；fal：公网长期，仅取 basename |
| 对象存储 | `[<prefix>/]<yyyymmdd>/{request_id}/step_{name}.{ext}` | 管线中间产物（大图引用传递）——**约定，非当前 API 能力**：`upload_temp_image` 不接受命名键，现无脚本产生此形态 | 同上 (BR-016) |

**降级规则（锁定）**：`REDIS_URL`/所选后端必填项（`minio` 看 `MINIO_ENDPOINT`，`fal` 看 `FAL_KEY`）未配置时，dev 模式降级为进程内内存缓存 + data URI 输出并打 warning 日志；docker compose 生产编排必须配齐，不允许降级。

## 7. 页面清单

不适用（纯 API 服务，无前端页面）。

## 8. 设计 Token

不适用（无 UI）。文档/README/日志中禁止 emoji（P0-1）、禁止 AI 模板味文案（P0-3）。

## 9. 验收标准（锁定——QA 以此为唯一依据，EARS 格式）

| 编号 | 功能 | EARS 验收标准 | 优先级 |
|------|------|---------------|--------|
| AC-01 | images 同步 | When 客户端以 model=vendor-a-text2img 请求 /v1/images，系统必须经 upstream_a/images.py 映射调用 mock 上游并返回 200 + `data[0]` 含 b64_json 或 url | P0 |
| AC-02 | images 批量 | When n=2 且上游不支持批量，系统必须内部聚合调用 2 次并返回 `len(data)==2` | P0 |
| AC-03 | 输出统一 | If response_format=b64_json 且上游返回 URL，系统必须下载后编码为 b64_json（BR-007） | P0 |
| AC-04 | 输出统一 | If response_format=url 且上游返回二进制，系统必须上传到当前 `STORAGE_BACKEND` 指向的对象存储并返回其 URL（minio 为预签名、fal 为公网长期）（BR-008）。无对象存储时不得为此失败请求，也不得把 data URI 冒充 url：退回上游自身的形态（通常为 b64_json） | P0 |
| AC-05 | chat 文本 | When 纯文本 messages 请求，系统必须返回标准 ChatCompletion 结构（choices[0].message.content 非空） | P0 |
| AC-06 | chat Vision | When messages 含 image_url，系统必须下载图片并按上游要求编码后重组请求 | P0 |
| AC-07 | 流式桥接 | When stream=true 且上游非流式，系统必须拆分为合法 SSE chunk 流并以 `data: [DONE]` 结束 | P0 |
| AC-08 | responses 工具 | When tools 含 image_generation，系统必须编排文本模型优化 prompt → 文生图 → 返回 output 数组含 message + image_generation_call | P0 |
| AC-09 | responses 状态链 | When 携带 previous_response_id，系统必须从 Redis 取回历史上下文拼接；响应后写入新上下文 | P0 |
| AC-10 | responses 容错 | If 编排中上游文本模型失败，系统必须返回标准化错误且不暴露内部编排细节 | P0 |
| AC-11 | 沙箱 | If 脚本含 `import subprocess` / `exec(` / `eval(` / `__import__` / `open(`，加载必须抛 SecurityError 拒绝执行 | P0 |
| AC-12 | 沙箱白名单 | While 脚本仅 import 白名单标准库（json/re/base64/datetime/time/math/random/string/typing/collections/itertools/functools/hashlib/uuid/urllib.parse），加载必须成功；合法的 `await` / 下标访问不得误杀 | P0 |
| AC-13 | 热重载 | When scripts/ 下文件被修改，系统必须在 5s 内使缓存失效，下次请求加载新代码并重新 AST 扫描 | P1 |
| AC-14 | 异步 Job | When mock 上游返回 job_id，系统必须按 poll_interval 轮询至 completed 并返回结果；If 超过 poll_timeout，必须返回 500 超时错误（BR-004） | P0 |
| AC-15 | 路由 | If model 未在注册表登记，系统必须返回 404 + `code=model_not_found` | P0 |
| AC-16 | Mock 上游 | Mock 服务必须提供：同步生图、异步 Job（提交/查询）、AK-SK 签名校验 三组端点，返回 1x1 PNG 真实字节 | P0 |
| AC-17 | 可观测 | While LOGFIRE_TOKEN 已配置，每请求必须产生含 registry_resolve/script_load/script_transform_request/upstream_http_call/script_transform_response Span 的 Trace；未配置时本地运行不报错 | P1 |
| AC-18 | 健康检查 | When GET /health，系统必须返回 200 + Redis/对象存储连通状态。存储项的键名＝当前 `STORAGE_BACKEND`（默认 `minio`），使运维一眼看出探针实际打到了哪个后端 | P1 |
| AC-19 | 鉴权 | If Authorization 缺失或 Token 错误（且 ADAPTER_AUTH_ENABLED=true），系统必须返回 401 标准错误 | P1 |
| AC-20 | 编排 | docker compose config 必须校验通过；adapter 容器非 root、脚本只读挂载 | P1 |
| AC-21 | 超时 | If 脚本执行超过 SANDBOX_TIMEOUT(30s)，系统必须返回 504（BR-003） | P0 |

### 9.1 v1.1 级联编排验收标准

| 编号 | 功能 | EARS 验收标准 | 优先级 |
|------|------|---------------|--------|
| AC-22 | 分层超时 | While 脚本声明了 `STAGES`，系统必须以 `STAGE_BUDGET_DEFAULT`(默认 300s，上限 600s) 而非 30s 作为总预算；单次转换钩子仍受 30s 约束（BR-011） | P0 |
| AC-23 | 图像门面 | When 脚本调用 `ctx.image.resize()`，系统必须返回处理后 bytes 且不阻塞事件循环；If 图像超过 50 MP 或输出超 20 MB，必须拒绝并返回标准错误（BR-012） | P0 |
| AC-24 | 级分发 | When 脚本声明 `STAGES = ["generate","upscale"]`，系统必须按序以 `phase="request:{stage}"` / `"response:{stage}"` 调用 `transform`，每级各发起一次上游调用 | P0 |
| AC-25 | 向后兼容 | While 脚本未声明 `STAGES`，系统必须完全走原有单次调用链路，AC-01~10 全部保持通过 | P0 |
| AC-26 | 级间传递 | When 某级 `response` 钩子调用 `ctx.emit(payload)`，系统必须将该 payload 作为下一级 `request` 钩子入参；`ctx.stage[name]` 必须可读取任意已完成级的产物 | P0 |
| AC-27 | 单级超时 | If 某级超过其 timeout 或总预算耗尽，系统必须返回 504 且错误 `code` 为 `stage_timeout`/`pipeline_budget_exceeded` 并含级名 | P0 |
| AC-28 | 降级 | If `STAGE_FALLBACK` 内的级失败且 `ctx.stage` 已有可用产物，系统必须返回上一级产物、响应标记 `degraded`、HTTP 状态仍为 200（BR-013） | P0 |
| AC-29 | 级可观测 | While LOGFIRE_TOKEN 已配置，级联请求必须在 `adapt` Trace 下产生每级 `stage` Span，含 stage 名、remaining_s、耗时、degraded 属性 | P1 |
| AC-30 | 批量放大 | When n=2 且 `STAGES` 含 3 级，系统必须按 `concurrency`(默认 3) 限并发，且 `adapter_stage_upstream_calls_total` 如实记录实际上游调用次数（BR-015） | P1 |
| AC-31 | 输入跳过 | If 某级在 `request` 钩子中返回 `ctx.SKIP` 且入参缺失（如文生图无入图），系统必须跳过该级继续执行，不得报错 | P0 |

### 9.2 v1.2 上游请求载体与 OpenAI 原生端点

| 编号 | 功能 | EARS 验收标准 | 优先级 |
|------|------|---------------|--------|
| AC-32 | multipart 载体 | When 脚本在 `request` 相位调用 `ctx.emit(files={...})`，系统必须以 `multipart/form-data` 发起上游调用：返回值（与 `form`）作文本字段、`files` 作二进制部件（文件名/字节/类型），boundary 与 Content-Type 由引擎生成，且不得残留会缺失 boundary 的 Content-Type；`files` 的字段值接受单部件或部件列表，非 bytes 必须报错 | P0 |
| AC-33 | OpenAI 原生两端点 | When 渠道 `X-Upstream-Url` 指向 generations 且请求含 `image`，脚本 `openai/images@v1` 必须改打同服务的 edits 姊妹端点（末段替换、保留 host 与 query，或按 `X-Channel-Options.edits_url`）并以上传文件的 multipart 发送（多图拼 `image[]`、`mask` 独立部件）；无 `image` 时保持 JSON 打 generations；响应与 `usage` 原样透传 | P0 |

## 10. 边界与约束

- 单文件 ≤ 300 行；分层 routes → services(调度/引擎/级联) → infra(ctx/存储/缓存)，依赖只向下（code-organization 标准）
- 脚本只能通过 ctx 访问基础设施，禁止直接 import aiohttp/redis/minio/PIL
- 上游调用超时 60s；纯转换钩子 30s；异步轮询 120s；级联单级 120s、总预算 300s（上限 600s）（BR-003/004/011）
- 级联只在单一上游内串联多级；跨上游编排不做（上游 URL 由 channel 头单值提供，BR-014）
- 所有请求携带 request_id 全链路透传（BR-010）
- 性能目标：协议转换耗时 P99 < 50ms；脚本加载 < 100ms（缓存命中）；级联自身调度开销（不含上游与图像算子）P99 < 30ms
- 不支持客户端取消异步 Job
- `STAGES` 只支持线性串联，不支持 DAG 分叉汇合与条件跳转（除 `ctx.SKIP` 整级跳过）

## 11. 内嵌已知坑（本项目预判，pitfalls.jsonl 为空首建）

| 坑 | 技术栈指纹 | 根因 | 修法 |
|----|------------|------|------|
| aioredis 包已废弃且与新 Python 不兼容 | redis | aioredis 并入 redis-py | 用 `redis.asyncio`，文档中 aioredis 字样仅作概念 |
| logfire 埋点函数与框架不匹配（历史） | logfire+fastapi | 曾跑在裸 Starlette 上 | 迁移 FastAPI 后统一为 `logfire.instrument_fastapi(app)` |
| 03_技术架构 §3.4 黑名单含 Subscript/Await | ast | 误封禁会导致所有正常脚本（下标/await）无法加载 | 沙箱只禁危险 import/调用/dunder 属性访问，不禁 Subscript/Await |
| watchdog 回调在子线程，直接操作 asyncio 对象崩溃 | watchdog+asyncio | 线程边界 | 回调用 `loop.call_soon_threadsafe` 或仅做线程安全的 dict pop |
| gunicorn UvicornWorker 下模块缓存为进程级 | gunicorn | 各 Worker 独立缓存 | 热重载靠每 Worker 各自的 watchdog 监听，无需跨进程同步 |
| httpx2/TestClient 测 SSE 需逐行读取 | pytest | SSE 非 JSON | 测试中按行解析 `data: ` 前缀 |
| 级联脚本被 30s 沙箱超时腰斩 | asyncio+stages | 30s 是墙钟，级联绝大部分时间在等上游 | 分层预算：声明 `STAGES` 后按总预算计，单次转换钩子仍 30s（BR-011） |
| 各级超时相加溢出总预算 | stages | 每级独立 timeout 累加可远超总预算 | 每级实际超时取 `min(stage_timeout, ctx.remaining)` |
| Pillow 同步调用阻塞事件循环 | Pillow+asyncio | resize/convert 是 CPU 密集同步操作 | `ctx.image` 内部走 `asyncio.to_thread`，不在事件循环里直接调 PIL |
| 解压炸弹撑爆内存 | Pillow | 小文件可解出超大位图 | 设 `Image.MAX_IMAGE_PIXELS` + 解码前校验 50 MP 上限（BR-012） |
| 中间产物在内存反复拷贝导致 OOM | stages | 每级持有完整 bytes，n>1 时线性放大 | `ctx.stage` 大图存对象存储句柄，级间传引用而非 bytes |
| 级联把上游配额瞬间打穿 | ratelimit | 一次客户端请求 = n × stages 次上游调用 | 限流按实际上游调用次数计费，`concurrency` 限并发（BR-015） |
| 降级产物被误判为成功而无标记 | stages | 上游 200 但内容是上一级原图 | 降级必须写 `degraded=true` + `degraded_reason`，并计 `adapter_stage_degraded_total`（BR-013） |

## 12. 端到端验证步骤（锁定）

```bash
# 0. 安装（managed venv）
/Users/betterme/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 1. 单元 + 集成测试
.venv/bin/python -m pytest tests/ -q          # 断言：全部通过，0 failed

# 2. 启动 mock 上游 + adapter（本地，无需 docker）
.venv/bin/python -m mock_upstream.main &      # 端口 9100
.venv/bin/python -m uvicorn adapter.main:app --port 8080 &

# 3. 健康检查
curl -s localhost:8080/health                 # 断言：200 {"status":"ok",...}

# 4. 核心成功流：同步文生图
curl -s -X POST localhost:8080/v1/images -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-text2img","prompt":"an orange cat","n":1,"response_format":"b64_json"}'
# 断言：200，data[0].b64_json 可解码为 PNG

# 5. 异步 Job 上游
curl -s -X POST localhost:8080/v1/images -H "Content-Type: application/json" \
  -d '{"model":"vendor-b-text2img","prompt":"a dog","response_format":"b64_json"}'
# 断言：200（内部完成 Job 轮询）

# 6. chat 非流式 + 流式
curl -s -X POST localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-vision","messages":[{"role":"user","content":"hello"}]}'
# 断言：200，choices[0].message.content 非空
curl -sN -X POST localhost:8080/v1/chat/completions -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-vision","messages":[{"role":"user","content":"hi"}],"stream":true}'
# 断言：SSE 流，末尾 data: [DONE]

# 7. responses 工具编排
curl -s -X POST localhost:8080/v1/responses -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-agent","input":"画一只橘猫在沙发上","tools":[{"type":"image_generation"}]}'
# 断言：200，output 含 image_generation_call

# 8. 关键错误流
curl -s -X POST localhost:8080/v1/images -d '{"model":"unknown-model","prompt":"x"}' \
  -H "Content-Type: application/json"
# 断言：404，error.code == "model_not_found"

# 9. Docker 编排校验
docker compose config -q                      # 断言：退出码 0
```

### 12.1 v1.1 级联编排验证步骤

```bash
# 10. 级联链路：前处理 + 生图 + 超分（AC-24/AC-26）
curl -s -X POST localhost:8080/v1/images -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-hd","prompt":"an orange cat","response_format":"b64_json"}'
# 断言：200；data[0].b64_json 可解码为 PNG，且尺寸大于非级联版本（超分生效）

# 11. 向后兼容回归（AC-25）—— 单级链路必须零变化
.venv/bin/python -m pytest tests/ -q -k "not stage"
# 断言：AC-01~10 相关用例全部通过，0 failed

# 12. 降级路径（AC-28）—— 关停超分 mock 后重跑级联
curl -s -X POST localhost:8080/v1/images -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-hd","prompt":"a dog","response_format":"b64_json"}'
# 断言：仍返回 200；响应含 degraded 标记；data[0] 为未超分的生图结果

# 13. 级间产物传递（AC-26）—— generate 级 ctx.emit() 的 payload 进入 upscale 级
# 断言：Logfire 中 script_phase(phase=request:upscale) 入参含 generate 级产出的图片引用

# 14. 单级超时与总预算（AC-27）—— mock 超分注入 200s 延迟，级超时 120s
# 断言：504，error.code == "stage_timeout"，message 含级名 upscale

# 15. 整级跳过（AC-31）—— 文生图请求无入图，preprocess 级返回 ctx.SKIP
# 断言：200，Logfire 中 stage(name=preprocess) 属性 skipped=true，无图像算子耗时

# 16. 上游调用计数（AC-30）
curl -s -X POST localhost:8080/v1/images -H "Content-Type: application/json" \
  -d '{"model":"vendor-a-hd","prompt":"two cats","n":2,"response_format":"b64_json"}'
# 断言：len(data)==2；adapter_stage_upstream_calls_total 增量 == 4（2 次生图 + 2 次超分）

# 17. 图像门面防护（AC-23）—— 投喂解压炸弹（小文件解出 > 50 MP）
# 断言：400，错误 code 为 image_too_large，进程内存无尖峰、事件循环不阻塞
```

## 13. 变更记录

| 日期 | 变更内容 | 原因 | 影响范围 |
|------|----------|------|----------|
| 2026-09-04 | v1.0 初版，锁定三端点完整骨架 + Mock 上游方案 | 用户确认 | 全部 |
| 2026-09-04 | 修正 03_技术架构 §3.4 沙箱黑名单（移除 Subscript/Await） | 原设计会误杀全部合法脚本 | sandbox.py |
| 2026-09-04 | aioredis → redis.asyncio | 包已废弃 | context/中间件 |
| 2026-09-09 | 新增 v1.1 级联范围（§2.1）、AC-22~31（§9.1）、级联已知坑 7 条 | 需支持「前处理+生成+后处理」「生成+超分」类级联 | engine.py/context.py/scripts |
| 2026-09-09 | BR-003 缩小适用范围为纯转换钩子；新增 BR-011~016 | 单一 30s 墙钟超时会腰斩所有级联请求 | 02_业务架构 §6/§6.1 |
| 2026-09-09 | ctx 新增 `image`/`stage`/`emit()`/`deadline`/`remaining`/`SKIP` | 前处理需图像操作、级间需传产物、各级需共享预算 | 03_技术架构 §3.5.1 |
| 2026-09-09 | 级联收敛为「模块级 `STAGES` + phase 分发」，撤销 `orchestrate` 钩子、`ctx.upstream()`、YAML `pipelines` 三项设计 | 上游 URL 由 channel 头单值提供，跨上游编排无处落地；复用既有 `transform` 单一入口即可表达线性级联，不引入第二套执行路径 | 03_技术架构 §3.8/§7、04_Spec §2.1/§9.1/§10 |
| 2026-09-09 | **AC-22~29、AC-31 实现落地**（AC-30 并发/计费未做）；新增 `degraded` phase | 降级时手上的中间产物是级间交接形状，直接返回会给出残缺 body，需脚本重新塑形 | 新增 `adapter/{budget,stages,stage_spec,transport}.py`、`adapter/utils/imageops.py`；改 `executor/context/channel/script_cache/errors/settings/api.pipeline` |
| 2026-09-09 | `executor.py` 拆出 `transport.py`、`channel.py` 拆出 `stage_spec.py` | 两文件在加入级联后均超出 §10 的 300 行/文件约束 | executor 350→263、channel 356→246 |
| 2026-09-10 | **AC-32/33 实现落地**：`ctx.emit(files=)` multipart 载体（`ctxapi/plan.py` 归一化 + `transport.build_multipart`）；新增内置脚本 `openai/images@v1`（generations JSON / edits multipart 分流）；mock 上游补 OpenAI 原生两端点与自省端点 | OpenAI 按载体拆两端点，JSON body 无法表达文件上传；沿用「厂商差异留在脚本、引擎只补协议无关原语」的分工，不把 OpenAI 语义写进引擎 | `ctxapi/plan.py`、`transport.py`、`script_store/openai/images@v1.py`、`mock_upstream/main.py`；新增 `tests/unit/test_emit_files.py`(10)、`tests/integration/test_openai_images_script.py`(11) |
| 2026-09-10 | 新增依赖审计工作流 `deps-audit.yml`；`anyio` 4.15.0→4.15.1；`pyproject.toml` 给 starlette 加上界 `>=1.6.0,<2` | 原先 CI 只有测试、无依赖审计，公告只能靠人工发现；且 fastapi 对 starlette 只声明 `>=0.46.0` 无上界，走 pyproject 安装会静默拉入未来大版本 | 新增 `.github/workflows/deps-audit.yml`；改 `requirements.txt`、`pyproject.toml`；**release.yml 未改**（审计刻意不作发版门禁） |
| 2026-09-10 | 测试依赖 `httpx` → `httpx2`（2.12.0） | starlette 1.6.0 的 `TestClient` 已优先导入 `httpx2`、回落到 `httpx` 时发弃用警告（"install httpx2 instead"）；httpx 稳定版停在 0.28.1（2024-12-06）且 issues/discussions 已关闭，Pydantic 以 `httpx2` 接管延续 | `pyproject.toml` 测试 extra、`requirements.txt`（+`httpcore2`/`truststore`，−`httpx`/`httpcore`）；**生产端 HTTP 客户端仍是 aiohttp，不在本次范围** |

---

### 13.1 v1.1 落地顺序（依赖递进）

1. ~~**分层超时 + `ctx.deadline`/`remaining`**（AC-22/27）~~ ✅ 已实现 —— `adapter/budget.py`，
   预算检查点在 `_do_upstream` 入口，实际超时取 `min(plan.timeout or upstream_timeout, remaining)`
2. ~~**`ctx.image` 门面**（AC-23）~~ ✅ 已实现 —— `adapter/utils/imageops.py`，
   `info`/`resize`/`convert`/`to_data_url` 四个算子（`crop`/`pad`/`compose_mask` 按 YAGNI 暂缓）
3. ~~**`STAGES` 声明 + `phase` 分发**（AC-24/25）~~ ✅ 已实现 —— `adapter/stages.py::execute_staged`，
   无 `STAGES` 时走原 `execute()` 路径
4. ~~**`ctx.stage` + `ctx.SKIP`**（AC-26/31）~~ ✅ 已实现 —— 级间产物 dict + `_Skip` 单例哨兵
5. ~~**降级与可观测**（AC-28/29）~~ ✅ 已实现 —— `STAGE_FALLBACK` + `degraded` phase +
   `cascade`/`stage` Span（含 `remaining_s`/`skipped`/`degraded` 属性）
6. **并发与限流计费**（AC-30）—— ⏳ 未实现。`n > 1` 的级内并发与
   `adapter_stage_upstream_calls_total` 指标尚未落地；当前 `StageOutcome.calls` 已在
   Span 上暴露每请求的实际上游调用数，可作为该指标的数据源。上线前需补。

**已完成部分的验证状态**：全量 142 passed（原有 76 + 新增 66），AC-01~10 单级链路零回归
（AC-25 的实质要求）。新增测试文件：`tests/unit/test_budget.py`（13）、
`tests/unit/test_imageops.py`（20）、`tests/unit/test_stage_declarations.py`（25）、
`tests/integration/test_stages.py`（8）。

每级完成后回归 AC-01~10 确认单级链路零回归（AC-25 的实质要求）。
