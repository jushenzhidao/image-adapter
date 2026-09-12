# Image Adapter — 协议适配执行引擎

把任意厂商的图像/多模态 API 转成 OpenAI 标准端点：`/v1/images/generations`、`/v1/images/edits`、`/v1/chat/completions`、`/v1/responses`。

**以 `/v1/images/generations` 为唯一规范格式。** 文生图与图生图走同一个端点：带上 `image`（可选 `mask`）即图生图，`image` 支持 URL、data URI、裸 base64 三种形态，脚本侧用 `ctx.image_*` 统一转成上游要的那一种。

`image` 传 `null` 或 `[]` **等同于不传**（即文生图）：该键会被**删除**而不是报错——留着它会被脚本原样转发给上游，而厂商不接受该参数。同理，`n` 不可用时**归一化为 1**；`response_format` 不是 `url`/`b64_json` 时（含 `null`、空串、chat 形态的 `{"type": ...}`）**判为「未指定」并删除该键**，由渠道自己的输出形态作答。三个字段都是「客户端没想认真说」的情形，各有安全读法：无图 / 一张图 / 渠道的输出形态。这是"能修就修"，不是放宽校验——真正含糊的输入（`image` 空字符串、没有 prompt 又没有图、`mask` 没有 image）仍然报 400。

`/v1/images/edits` 只是它的 **multipart 前门**：OpenAI 把 edits 拆出去是载体差异（multipart 文件上传 vs JSON），不是语义差异，所以该路由不含任何适配逻辑，只做一次形态改写后汇入同一条管道——脚本永远只需实现一套契约。

`/v1/chat/completions` 与 `/v1/responses` 是同一契约的**另外两个前门**（`adapter/api/frontdoor.py`）：前者把
`messages` 折成 `prompt` + `image`，再把规范响应的 `data[]` 封装成 `choices[0].message.content`；后者折 `input`、
封 `output[]`。**判定依据是请求打到的 path** —— 不靠渠道开关，也不需要脚本自述接受哪种请求体。于是渠道按
OpenAI 类型配置、最终用户打过来的四个端点全部兼容，脚本侧一行不用改。

| multipart/form-data | 规范 JSON |
|---|---|
| `image=@a.png`（文件） | `image: "data:image/png;base64,..."` |
| `image[]=@a.png&image[]=@b.png` | `image: ["data:...", "data:..."]` |
| `mask=@m.png` | `mask: "data:image/png;base64,..."` |
| `image=https://cdn/a.png`（文本） | `image: "https://cdn/a.png"` |
| `image=`（空 part／空文件） | 该键被**删除**（即文生图） |
| `n=2` | `n: 2`（转 int） |
| `n=abc`（无法解析） | `n: 1`（归一化，不报错） |
| 厂商私有字段 | 原样透传为字符串 |

上传文件转成 data URI 而非裸 base64，mime 由 **magic number 嗅探**得出（SDK 常把 PNG 声明成 `application/octet-stream`），因此 `ctx.image_*` 后续无需再嗅探。改写的 body 仍走 generations 同一个 `validate_images_body()`，不做第二套校验。

只有一处按载体区分：**multipart 分不清「字段没传」和「字段传了空值」**——HTML 未选文件就是提交一个长度 0 的 part，SDK 读到空文件也一样，这是载体本身的歧义，所以本门把它按「没传」处理（等同于 JSON 门的 `image: []`／`null`，即文生图），而不是报 400。JSON 与 chat／responses 门能表达这个区别，`image: ""` 这种**调用方主动写的空值**在那里仍然是 400。

## 职责边界

本服务是纯粹的**数据面（执行引擎）**，自身不存储任何渠道、模型、计费知识。

| | 控制面（New API） | 数据面（本服务） |
|---|---|---|
| 职责 | 渠道 / 模型 / 计费 / 路由 | 沙箱执行、协议转换、异步轮询、图片存储、可观测 |
| 配置来源 | 渠道配置界面 | 无配置文件，全部由请求头驱动 |
| 状态 | 持久化 | 无状态（Redis 仅作缓存与会话链） |

推论：**一个渠道 = 一个上游端点**。同厂商的 chat 与 images 各建一个 New API 渠道、各填各自的 URL，天然复用 New API 的负载均衡与故障切换。

## 渠道契约（请求头）

New API 在渠道配置里声明适配策略，通过头透传：

| 头 | 必填 | 说明 |
|---|---|---|
| `X-Upstream-Url` | 是 | 上游完整端点，如 `https://api.vendor-x.com/v2/text2img` |
| `X-Script` | 三选一 | 内联脚本源码，换行写作字面量 `\n` |
| `X-Script-64` | 三选一 | 源码的 base64（避免 `\n` 转义混乱） |
| `X-Script-Ref` | 三选一 | 命名引用 `vendor_y/mj@v1.3`，或 https URL |
| `Authorization` | 否 | **上游厂商**凭证，原样透传，不用于本服务鉴权 |
| `X-Adapter-Key` | 是 | 本服务准入密钥 |
| `X-Upstream-Method` | 否 | 默认 `POST` |
| `X-Auth-Emit` | 否 | 凭证位置非标准时，如 `header:X-API-Key:Bearer` |
| `X-Async` | 否 | 异步 Job 型上游，如 `poll=2,timeout=300` |
| `X-Script-Sha256` | 否 | 完整性锁定 |
| `X-Channel-Options` | 否 | JSON 对象，脚本内通过 `ctx.options` 读取 |

这 11 个头在代码里由 `adapter/main.py::channel_contract` 用 `Header()` 声明：因此 `/docs`
可以直接填写试调，契约表不会再与实现漂移。全部声明为**可选**是有意为之——缺失的头仍由
`channel.py` 报 `channel_config_error`（400，OpenAI 错误体），而不是被 FastAPI 拦成 422。

最小形态示例：

```json
{
  "X-Adapter-Key": "<data-plane-key>",
  "X-Upstream-Url": "https://api.vendor-x.com/v2/text2img",
  "Authorization": "Bearer <vendor-key>",
  "X-Script": "async def transform(ctx, payload, phase):\n    if phase == 'request':\n        return {'desc': payload['prompt']}\n    return {'data': [{'url': payload['img']}]}"
}
```

请求链路：取 `X-Upstream-Url` + 脚本 + `Authorization` → `transform(phase='request')` 转请求体 → 带凭证调上游 → `transform(phase='response')` 转响应体 → 返回客户端。

## 脚本契约

单一入口函数，相位（phase）区分方向：

```python
async def transform(ctx, payload, phase):
    if phase == 'request':
        # Vision 场景：image_url 可能是远程 URL，也可能是 data URI / 裸 base64。
        # 交给 ctx 判断形态——只有远程 URL 才会真的发起下载。
        return {'desc': payload['prompt'], 'image_b64': await ctx.image_b64(payload['image_url'])}

    # response 方向：上游返回二进制，客户端要 URL → 传 MinIO
    url = await ctx.upload_temp_image(payload)   # payload 是 bytes
    return {'data': [{'url': url}]}
```

相位取值：

- `request` / `response` —— 同步链路，必需
- `poll_request` / `poll_response` —— 仅 `X-Async` 开启时需要，脚本须用模块级 `PHASES` 声明，否则报 `channel_config_error`

`poll_response` 返回 `{'done': bool, 'payload': ...}`；`done=True` 时 `payload` 交给 `response` 相位收尾。

### ctx API

| 成员 | 说明 |
|---|---|
| `ctx.options` | `X-Channel-Options` 解析后的 dict |
| `ctx.upstream_url` | 当前渠道 URL |
| `ctx.request_id` | 贯穿日志的请求 ID |
| `await ctx.download_image(url)` | 下载图片，Redis 缓存 |
| `await ctx.upload_temp_image(raw)` | 传 MinIO 返回预签名 URL；无 MinIO 时降级为 data URI |
| `ctx.encode_b64(raw)` / `ctx.decode_b64(s)` | base64 编解码 |
| `await ctx.image_bytes(ref)` | 三态入参（URL / data URI / 裸 base64）统一取原始字节 |
| `await ctx.image_b64(ref)` | 三态入参 → 裸 base64（校验+限长；已是 b64 则原样返回，不重编码） |
| `await ctx.image_data_uri(ref)` | 三态入参 → data URI，mime 由magic number 嗅探 |
| `await ctx.image_url(ref)` | 三态入参 → 可公网访问 URL（必要时经 MinIO 中转） |
| `await ctx.compress_image(ref, *, max_edge=, max_bytes=, fmt=, quality=)` | 三态入参 → 变小后的 bytes。`max_edge` 是长边硬上限；`max_bytes` 是**目标非保证**（先降 quality 到下限 40，再降尺寸，且只在缩小确实达标时才动尺寸）；格式解析不开的图会**抛错**，要不要退回原图由调用方决定（见 `volcengine_ark/images@v1` 的 `ref_*`） |
| `ctx.is_url(s)` / `ctx.is_data_uri(s)` | 形态判断，写多分支转换时用 |
| `ctx.emit(url=..., method=..., headers=..., query=..., body=..., form=..., files=..., raw=..., timeout=...)` | 覆盖本次上游调用的任意维度（轮询换端点、multipart 上传等） |
| `ctx.fail(msg, code=..., param=..., status=400)` | 以客户端可见的错误结束请求。上游返回 200 但业务失败时用（安全拦截、模型拒答、没出图）；脚本没有别的错误通道——抛其它异常会被包成 500，返回 `{"error": ...}` 会被当成 200 正常响应 |
| `ctx.key` | 上游凭证（`Authorization` 去掉 Bearer 后的值），签名计算时用 |
| `await ctx.sleep(s)` | 异步等待（脚本内禁用 `import asyncio`） |
| `ctx.logfire` | 追踪句柄，`with ctx.logfire.span('...')` |

请求体有**三种形态**：脚本返回 dict 就得到 JSON，另外两种用 `emit()` 显式声明。

| 形态 | 声明方式 | 文本字段来源 | 二进制部件 |
|---|---|---|---|
| JSON | 直接 `return dict` | 返回值 | — |
| form-urlencoded | `emit(form={...})` | `form` | — |
| multipart/form-data | `emit(files={...})` | 返回值（并与 `form` 合并） | `files` |

`files` 的每个字段接受**一个部件** `("a.png", raw_bytes, "image/png")`，或**部件列表**（同名字段重复时用列表——OpenAI 的多图编辑拼作 `image[]`）。boundary 与 Content-Type 由引擎生成，脚本只交字节：

```python
ctx.emit(
    url=f"{base}/edits",
    files={"image[]": [("a.png", raw_a, "image/png"), ("b.png", raw_b, "image/png")]},
)
return {"prompt": "blend", "n": "2"}   # 这些成为 multipart 文本字段
```

字节一般来自 `await ctx.image_bytes(ref)`——三态入参（URL / data URI / 裸 base64）统一取原始字节，mime 由 `ctx.sniff_mime(bytes)` 嗅探。

**关于 `ctx.download_image` 的错误分类（行为变更）**：它只接受远程 http(s) URL，**不**做三态分派（分派是 `image_bytes`/`image_b64`/`image_data_uri`/`image_url` 的职责）。传入 `data:` URI 或裸 base64 时，现在返回 **`invalid_request`（400，`param="image"`）**，消息里点名该用的方法；此前是 `check_url` 报的 **`channel_config_error`（`param="ctx.download_image(url)"`）**。改动理由是归因：内联图不是控制平面配错了头，而是脚本选错了方法。**控制平面若按 error code 分流（例如把 `channel_config_error` 当作渠道故障告警/停用），需同步这一条。**

同一守卫顺带收敛了非字符串入参：`None`/`str` 以外的值现在统一得到上述 400；此前非空非字符串（如 `42`）会在 `check_url` 内执行 `.strip()` 抛 `AttributeError`，被包成 500 `ScriptRuntimeError`（无键名、不可读）。

## 安全模型

从请求头注入 Python 源码本质上是**受控的远程代码执行**，因此有四道防线，全部默认开启：

1. **准入**：`X-Adapter-Key`（恒定时间比较）。`ADAPTER_KEY` 未配置时拒绝所有请求，除非显式 `ADAPTER_KEY_REQUIRED=false`。
2. **来源策略**：生产建议 `ALLOW_INLINE_SCRIPT=false` + `SCRIPT_SHA256_ALLOWLIST=<hash1,hash2>`，只放行审核过的脚本；远程 URL 引用默认关闭。
3. **AST 沙箱**：白名单 stdlib 导入；禁 `exec/eval/open/getattr/setattr` 等及一切 dunder 访问，堵死 `().__class__` 逃逸族。
4. **受限 builtins**：编译后在无文件/网络/import 能力的命名空间执行；基础设施只能通过 `ctx` 触达。

`X-Upstream-Url` 与 `ctx.download_image` 均过 SSRF 校验（scheme/host/私网段策略，`UPSTREAM_ALLOW_PRIVATE_NETWORK=false` 时拒绝内网目标）。

注意校验**只在 URL 分支生效**：客户端内联图（data URI / 裸 base64）由 `ctx` 本地解码，不经过 `UPSTREAM_HOST_SET` 白名单——这是正确行为（没有出站请求），但别把 host 白名单理解成「约束了所有图片入参」。

## 快速开始

```bash
# 安装
/Users/betterme/.workbuddy/binaries/python/versions/3.13.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 测试（475 项）
.venv/bin/python -m pytest tests/ -q

# 启动
ADAPTER_KEY=dev-key .venv/bin/python -m uvicorn adapter.main:app --port 8080
```

E2E 冒烟（火山方舟，脚本走内置 script_store 引用）：渠道头**按 `@stable` 写**（用户口径：
以 `@stable` 为准），它既是别名、又恰好是退役版本的降级目标；`@v1` / `@latest` 指向同一版。

```bash
curl -s -X POST localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "X-Adapter-Key: dev-key" \
  -H "X-Upstream-Url: https://ark.cn-beijing.volces.com/api/v3/images/generations" \
  -H "X-Script-Ref: volcengine_ark/images@stable" \
  -H "Authorization: Bearer $VOLCENGINE_ARK_API_KEY" \
  -d '{"prompt":"一只可爱的白色小猫","size":"2048x2048","response_format":"url"}'
```

同一个上游脚本，换用 OpenAI SDK 的 multipart 形态（`image` 传文件，头与脚本完全不变）：

```bash
curl -s -X POST localhost:8080/v1/images/edits \
  -H "X-Adapter-Key: dev-key" \
  -H "X-Upstream-Url: https://ark.cn-beijing.volces.com/api/v3/images/generations" \
  -H "X-Script-Ref: volcengine_ark/images@stable" \
  -H "Authorization: Bearer $VOLCENGINE_ARK_API_KEY" \
  -F image=@cat.png \
  -F prompt=把猫换成橘色 \
  -F response_format=url
```

## OpenAI 原生上游（两通端点）

OpenAI 把图片任务按**载体**拆成两个端点：`/v1/images/generations` 收 JSON，
`/v1/images/edits` 收 multipart/form-data。适配器的规范 body 已把这一差异收敛到
一个 `image` 字段，所以内置脚本 `openai/images@v1` 按它分流，渠道只声明**主端点**：

| 客户端请求 | 打到上游 |
|---|---|
| 无 `image` | `POST {X-Upstream-Url}`，JSON |
| 有 `image` | `POST {同服务的姊妹端点}`，multipart（`image[]` / `mask` 为文件部件） |

姊妹端点由渠道 URL 的末段替换推出，host 与 query 保留（因此 Azure 的
`api-version` 会一并带过去）；布局不匹配时用
`X-Channel-Options: {"edits_url": "..."}` 直接指定。响应也走同一个脚本：`usage` 原样
透传（计费依赖它），但**图片的输出形态按客户端的要求交付**，不把上游的偷懒转嫁给调用方。
兼容 OpenAI 的上游对 `response_format` 的遵守程度并不一致——有的回真链接，有的在
`data[].url` 里塞一个 `data:` URI，有的干脆忽略该字段只给 `b64_json`：

| 客户端要的 | 上游给的 | 脚本做的事 |
|---|---|---|
| `url` | `data:` URI 或 `b64_json` | 落对象存储换回真链接（**没配存储则原样返回**） |
| `b64_json` | 链接（`http` 或 `data:`） | 取回 / 解码后重新编码 |
| 不传 | 任意 | 不干预，原样透传 |
| 非法值（`null`／`""`／`{"type":...}`） | 任意 | 同「不传」：该键在准入校验阶段已被删除，不报 400 |

这正是 `docs/04_Spec.md` 的 AC-03 / AC-04（BR-007 / BR-008）。两点要清楚：
`response_format=b64_json` 且上游只给链接时会发生下载，那个 host 必须在渠道的
`upstream_host_set` 白名单里。`response_format=url` 要有配好的对象存储才能产出**真链接**——
没配就跳过这一步、把上游原本的形态返回（可能仍是 `b64_json`，或上游自己塞的 `data:` URI），
不为我们的配置缺口去 502；代价是要对外承诺链接的部署必须配好 MinIO。

（火山方舟**不在**此列：它的上游如实支持 `url` 与 `b64_json` 两种 `response_format`，
脚本转发即可，响应原样返回就对 —— 见 `docs/05` §2。**只有会撒谎的上游才需要归一化。**）

```bash
curl -s -X POST localhost:8080/v1/images/generations \
  -H "Content-Type: application/json" \
  -H "X-Adapter-Key: dev-key" \
  -H "X-Upstream-Url: https://api.openai.com/v1/images/generations" \
  -H "X-Script-Ref: openai/images@stable" \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -d '{"model":"gpt-image-1","prompt":"一只白猫","image":"https://cdn.test/cat.png"}'
```

## 项目结构

```
adapter/
  main.py            # ASGI 应用与生命周期
  settings.py        # 数据面策略（无渠道配置）
  channel.py         # 渠道头解析（ChannelSpec / AuthEmit / AsyncSpec）
  script_source.py   # 内联 / base64 / 引用 / 远程 四种脚本来源 + sha256 校验
  sandbox.py         # AST 扫描
  script_cache.py    # 按源码 sha256 缓存编译产物（替代热重载）
  executor.py        # 相位管线：auth -> request -> 上游 -> poll -> response
  context.py         # ctx 组装根：仅状态 + 基础设施句柄 + 生命周期
  ctxapi/            # ctx 脚本 API（按关注点分 mixin，新增能力只加文件）
    base.py          #   属性契约 + Needs* 跨 mixin 依赖声明
    codec.py         #   base64 / data URI / MIME
    image_ref.py     #   下载 + 三形态互转
    storage.py       #   bytes -> URL（MinIO，缺失则降级 data URI）
    budget.py        #   ctx.remaining / ctx.deadline
    plan.py          #   RequestPlan + ctx.emit()（JSON / form / multipart）
  script_source.py   # 来源策略：尺寸/哈希/白名单/SSRF（不含查找）
  scriptstore/       # 命名引用的可插拔后端
    ref.py           #   ref 语法（vendor_y/mj@v1.3）
    base.py          #   ScriptStore 协议 + ChainStore 优先级链
    dirstore.py      #   目录后端（一个实例对应一个根）
  urlguard.py        # SSRF 防线
  api/pipeline.py    # 各端点共用的请求路径
  api/images.py      # 规范格式：generations（校验逻辑的唯一来源）
  api/image_edits.py # multipart 前门：改写形态后汇入 images
  api/frontdoor.py   # chat / responses 前门：折叠进规范体 + 出口封装（纯函数）
  api/chat.py        # /v1/chat/completions（前门 + SSE 桥）
  api/responses.py   # /v1/responses（前门 + 状态链）
script_store/        # 命名脚本库（默认后端，随镜像打包）
  manifest.json      # 可选：别名（@stable/@latest）+ 每版本 sha256
  volcengine_ark/images@v1.py   # 每渠道只有一个版本（2026-09-13 起取消版本切分）
  openai/images@v1.py           # OpenAI 原生：generations(JSON) / edits(multipart) 分流
  google/images@v1.py
                            # 改行为＝原地改这一个文件 + 重算 manifest 摘要；没有旧版可退
tests/               # 单元（沙箱/工具/组装/存储）+ 集成（真实 HTTP mock 上游）
```

## 脚本存储：镜像内置 + 可选只读挂载

命名引用 `X-Script-Ref` 按**链式优先级**解析，两种部署形态同时支持：

| 顺序 | 后端 | 配置 | 用途 |
|---|---|---|---|
| 1 | overlay | `SCRIPT_OVERLAY_DIRS`（逗号分隔，可空） | 只读卷挂载，热修脚本免重建镜像 |
| 2 | image | `SCRIPT_REF_DIR`（默认 `/app/script_store`） | 随镜像打包，恒存在，无宿主依赖 |

**默认读镜像内置目录**：不配 `SCRIPT_OVERLAY_DIRS` 时链上只有 image 后端，
行为与之前完全一致。挂载目录后，同名 ref 命中 overlay，未命中则回落镜像；
目录不存在只是跳过，不算错误。编译缓存以源码 sha256 为键，改文件即换键，
所以挂载目录里改脚本**无需重启**即可生效。

**可选 `manifest.json`**（每个根一份，没有它也能正常用）：提供别名与摘要。

```json
{"scripts": {"volcengine_ark/images": {
  "latest": "v1", "aliases": {"stable": "v1"}, "digests": {"v1": "7bb5c4cb..."}}}}
```

于是 `X-Script-Ref: volcengine_ark/images@stable` 与 `@latest` 都可用，改一处
清单即可整体前移版本，不必改每个渠道头。`digests` **默认只声明不强制**，
要强校验就打开 `SCRIPT_PIN_MANIFEST_DIGESTS=true`。清单损坏（非法 JSON、
结构不对）只记 warning 并按「无清单」处理，不会连带其它 ref 一起失败。

> ⚠️ **本仓现状（2026-09-13 起）**：三个渠道各只保留一份 `images@v1.py`，`latest` 与
> `stable` 都指向它。别名机制仍在、也确实能整体前移，但**现在没有第二个版本可前移**。

⚠️ **改清单要重启，改脚本不用** —— 两者语义不同，别把脚本的热改经验套到清单上：

| 改什么 | 何时生效 | 原因 |
|---|---|---|
| 挂载目录里的**脚本 `.py`** | **立即**，无需重启 | 读取缓存按 `(mtime_ns, size)` 校验，改文件即换缓存键 |
| `manifest.json`（别名 / 摘要） | **必须重启**（或滚动发布） | 清单在 `DirStore.__init__` 里 `load_manifest` 读一次，而 store 是进程启动时建一次的 |

实测：同一进程内把 `stable` 从 `v1` 改指另一个版本，`@stable` 仍然解析到 `v1`；
新建 store 实例后才变。要**不重启**换版本，只有两条路 —— 改渠道头的
`X-Script-Ref` 指向具体版本，或把脚本放进 overlay 目录。

别名只影响**带版本号的 ref**：`@stable` / `@latest` 会被查表改写，而 `@v1` 这种
具体版本**原样透传**（`Manifest.resolve` 只在 `ref.version` 是别名时才改写）。
也**绝不要**把 `v1` 写成指向另一个版本的别名。

🔴 **「钉住旧版本」这条逃生阀没有了，但退役版本不会打死部署**（2026-09-13）：`@v2` 及以上
已删除、manifest 也没为它们留别名 —— 但链条会**降级到 `@stable`**（并打一条 `warning`），所以
渠道头指旧版本得到的不是失败，而是**stable 那一版的内容**。要回滚行为只能改脚本（旧版正文在
git 历史里，见提交 `chore(script_store): 归档 ark v3–v8 后再合并为单一 v1`）。

⇒ **`stable` 因此是必须项**：每个 manifest 条目都要定义它，否则降级无处落地、退役版本会退回成
硬失败。启动时会对缺少它的条目打 warning，`tests/unit/test_scriptstore.py` 也钉住了本仓
manifest 三渠道都定义了它。降级还会开一个 `script_ref_fallback` span（`requested` → `serving`），
所以它在 Logfire 里可查、可告警 —— 一次降级不该只在一条日志里被看见。降级**只发生在「具体版本找不到」时**：无版本的 ref 仍报
`script_not_found`，而某个 root 真有该版本时也仍按根优先级如实服务（不会被 `stable` 顶替）。

## 关键环境变量

> 下列取值是**示例值**（与 `.env.example` 模板一致），作用是说清键名与语义，**不代表本部署在跑的数**；
> 它也**不全等于**代码默认（`adapter/settings.py`：`SCRIPT_TIMEOUT=30`、`UPSTREAM_TIMEOUT=60`、
> `POLL_TIMEOUT_DEFAULT=300`、`IMAGE_DOWNLOAD_TIMEOUT=25`）。本部署 `.env` 的实际覆盖
> （`SCRIPT_TIMEOUT=240` / `UPSTREAM_TIMEOUT=300` / `IMAGE_DOWNLOAD_TIMEOUT=60` / `STORAGE_UPLOAD_TIMEOUT=60` /
> `POLL_TIMEOUT_DEFAULT=240` / `FANOUT_CONCURRENCY=5`）与代码默认的逐项对照见 `docs/03` §3.3.1；
> 两者不一致时**一律以 `.env` 为准**。

```bash
ADAPTER_KEY=                     # 数据面准入密钥（必填，除非显式关闭）
ALLOW_INLINE_SCRIPT=true         # 生产置 false
SCRIPT_SHA256_ALLOWLIST=         # 逗号分隔的脚本哈希白名单
SCRIPT_REF_DIR=./script_store    # 命名引用的默认目录（镜像内置）
SCRIPT_OVERLAY_DIRS=             # 逗号分隔的只读挂载目录，优先于上一项
SCRIPT_PIN_MANIFEST_DIGESTS=false # true = 按 manifest 的 sha256 强校验脚本
UPSTREAM_ALLOW_PRIVATE_NETWORK=true  # 生产置 false（SSRF）
UPSTREAM_HOST_ALLOWLIST=         # 逗号分隔的上游主机白名单
REDIS_URL=                       # 空 = 内存降级
STORAGE_BACKEND=minio            # 对象存储后端：minio（预签名，会过期）| fal（公网长期，需 fal extra）
MINIO_ENDPOINT=                  # 空 = data URI 降级（STORAGE_BACKEND=minio 时）
FAL_KEY=                         # 空 = data URI 降级（STORAGE_BACKEND=fal 时）
LOGFIRE_TOKEN=                   # 空 = 仅本地
SCRIPT_TIMEOUT=30            # 单个 transform() 调用的墙钟上限（不含等上游的时间）
UPSTREAM_TIMEOUT=180         # 单次上游 HTTP 调用；等待出图看的就是这一项
POLL_TIMEOUT_DEFAULT=120     # 异步 Job 轮询的总时长（不是单次轮询的间隔）
STAGE_BUDGET_DEFAULT=300     # 多级级联的总预算，上限 STAGE_BUDGET_MAX=600
IMAGE_DOWNLOAD_TIMEOUT=25    # 单次客户端图片下载；紧贴 SCRIPT_TIMEOUT 下方，只负责「报自己」不是「掐时间」
STORAGE_UPLOAD_TIMEOUT=25    # 单次对象存储上传；超时按既有契约降级成 data URI
FANOUT_CONCURRENCY=5         # 单请求内并发物化条数（ctx.fanout：N 张图下载 / 取回 / 上传）；实际并发 = min(本值, N)
```

## 响应头

每个成功响应携带 `X-Request-Id` 与 `X-Script-Sha256`（实际执行的脚本指纹，供控制面审计比对）。

## License

Proprietary. Developed for internal use.
