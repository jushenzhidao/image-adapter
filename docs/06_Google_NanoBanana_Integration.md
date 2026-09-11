# Google Nano Banana（Gemini Image）接入设计方案

> 状态：**设计稿**（未落库、未实测）。
> 上游契约来源：Google 官方文档镜像、Firebase AI Logic 文档、Google 官方博客（2026-09-11 检索）。
> **本机沙箱无法直连 Google 域**（`ai.google.dev` / `generativelanguage.googleapis.com` 均返回
> `curl: (56) Received HTTP code 502 from proxy after CONNECT`），因此第 9 节列出的每一项
> 都必须用真实 key 实测后才能写进 `script_store/`。文中数值凡带 ⚠️ 的均为待验证。
>
> **2026-09-11 复核修正（重要）**：**输入侧支持外部 https URL 直通**——`parts[].fileData.fileUri`
> 可指向任意公开可读或 presigned 的 URL（官方「File input methods」+ 一份真实请求样例双证，见附录 B）。
> 因此 4.4 节由「inline 为默认」改为「**URL 直通优先、按引用形态与体积自动分流**」。
> 输出侧结论不变：**仍然只有 base64（`inlineData`）**，"只有 base64" 这句原先只针对输出，
> 措辞不够干净，此处更正。
>
> **实施进度（2026-09-11）**：C1~C4 已实施 —— 引擎 `ctx.fail()`（`adapter/ctxapi/fault.py`）、
> 脚本 `script_store/google/images@v1.py`（sha256 `65d33c7acd03e3a2…`，已登记 manifest）、
> 18 项端到端测试（`tests/integration/test_google_images_script.py`）均已落库；
> 全量 340 项测试通过、ruff 干净。**C5（route 层直转）未实施**。
> 真实 key 的 P0 实测（第 9 节）仍未做 —— 契约来自官方文档与一份生产样例，脚本头部
> CONTRACT STATUS 已明确标注这一点。

---

## 1. 结论速览

**新增一个上游 = 新增一个脚本 + 一条渠道配置。** 适配器架构不用动，但有两处必须处理：

| # | 与 ARK 的根本差异 | 转换动作 |
|---|---|---|
| 1 | **模型 ID 在 URL 路径里**（`/v1beta/models/{model}:generateContent`），不是 body 字段 | request 相位用 `ctx.emit(url=...)` 改写路径；注意这会**重跑 SSRF 白名单检查** |
| 2 | **输出侧只有 base64**（`inlineData`），没有 URL 形态；**输入侧相反，支持外部 URL 直通** | 输出：`response_format: "url"` 需在 response 相位 `ctx.upload_temp_image()` 落对象存储（后端由 `STORAGE_BACKEND` 决定）。输入：`file_data.file_uri` 直通，见 4.4 |
| 3 | **客户端请求体在 response 相位不可见**（response 相位 payload = 上游响应） | 需要模块级 `_STATE[request_id]` 传递 `response_format`；这是 ARK 脚本没有的新机制 |
| 4 | **鉴权头是裸 key**（`x-goog-api-key`，无 Bearer 前缀） | `X-Auth-Emit: header:x-goog-api-key`（`apply_auth` 已支持空前缀） |
| 5 | **"HTTP 200 但没有图"是常态**（安全拦截、模型拒绝、`NO_IMAGE`） | response 相位必须校验 `finishReason` 与 parts；**这是本方案最大的静默失败风险点** |

### 需要的一处引擎改动

脚本无法 import `adapter.errors`（`sandbox.py::ALLOWED_STDLIB` 不含），所以当前**没有**把
"上游 200 但业务失败"转成正确 HTTP 错误码的通道：

- `raise ValueError(...)` → `_call_phase` 包成 `ScriptRuntimeError` → **500**（语义错误）
- 返回 `{"error": {...}}` → `images_handler` 直接 `json_ok()` → **200**（更糟）

`executor._call_phase` 已有 `except AdapterError: raise`（原样透传），因此**只需给 ctx 加一个
`fail()` 方法**即可闭环（实现见 5.5 节，约 15 行）。

---

## 2. 上游契约画像

### 2.1 端点与鉴权

两套 API 表面，同一份请求/响应结构：

| | Gemini Developer API（**推荐**） | Vertex AI |
|---|---|---|
| 端点 | `POST https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent` | `POST https://{location}-aiplatform.googleapis.com/v1/projects/{project}/locations/{location}/publishers/google/models/{model}:generateContent` |
| 鉴权 | `x-goog-api-key: <API_KEY>`（也支持 `?key=`） | `Authorization: Bearer <OAuth2 token>` |
| 计费/配额 | 按 API key 的 project | 按 GCP 项目 + IAM |

`v1beta` 是图像模型的现行版本；`v1` 亦有对应路径（⚠️ 需实测哪个可用，两者不要混用）。

### 2.2 模型家族与能力矩阵（⚠️ 全部需实测确认）

| 别名 | 模型 ID | 分辨率档 | 支持的 imageSize | 图片输出 token |
|---|---|---|---|---|
| Nano Banana 2 Lite | `gemini-3.1-flash-lite-image` | 512 / 1K | `512`,`1K` | 747 / 1120 |
| Nano Banana 2 | `gemini-3.1-flash-image` | 512 / 1K / 2K / 4K | `512`,`1K`,`2K`,`4K` | 747 / 1120 / 1680 / 2520 |
| Nano Banana Pro | `gemini-3-pro-image` | 1K / 2K / 4K | `1K`,`2K`,`4K` | 1120 / 1120 / 2000 |
| Nano Banana | `gemini-2.5-flash-image` | 固定 1K | （不可设） | 1290 |

- 旧 ID `gemini-3-pro-image-preview` 已废弃（⚠️ 传闻 2026-06-25 停机），**渠道一律用稳定 ID**。
- 比例（全家族一致，14 个）：`1:1 1:4 1:8 2:3 3:2 3:4 4:1 4:3 4:5 5:4 8:1 9:16 16:9 21:9`。
  其中极端比例（`1:4 / 4:1 / 1:8 / 8:1`）⚠️ 在 2.5 Flash 上应实测是否可用。
- **`imageSize` 必须大写 K**（`1K`/`2K`/`4K`），`512` 无后缀；小写 `1k` 会被拒。
- 计费口径（官方 token 表，与像素无关，只与档位有关）：
  图片输出按上表 tokens 计 + `thoughtsTokenCount`（3.x Pro 的思考 token，按 output 价计）
  + prompt 输入 token。**失败也会计 thinking token**。

### 2.3 请求体：注意有两代字段形态

```jsonc
{
  "contents": [{
    "role": "user",
    "parts": [
      { "text": "把背景换成星空" },
      { "inline_data": { "mime_type": "image/png", "data": "<base64>" } }
    ]
  }],
  "generationConfig": {
    "responseModalities": ["TEXT", "IMAGE"],

    // 形态 A（2.5 / 3.0 时期，现有大量三方网关仍用这个）
    "imageConfig": { "aspectRatio": "16:9", "imageSize": "2K" }

    // 形态 B（新版，官方把 imageConfig 标记为 legacy）
    // "responseFormat": { "image": { "aspectRatio": "16:9", "imageSize": "2K" } }
  }
}
```

⚠️ **A/B 两形态必须实测确认哪个被当前模型接受**。设计上由渠道选项兜住：
`X-Channel-Options.image_config_style = "imageConfig" | "responseFormat" | "auto"`（默认 `auto` 按模型代际选，未知模型用 `imageConfig`）。

其它要点：

- `responseModalities`：纯出图 `["IMAGE"]`；需要图文混排（infographic、故事配图）或开
  `tools:[{"google_search":{}}]` 时必须含 `"TEXT"`，否则 400。
- JSON 里字段名可用 snake_case（`inline_data`）或 camelCase（`inlineData`），官方 REST 示例用 snake_case。
- 没有 `n`、没有 `size`、没有 `watermark`、没有 `response_format` —— 这些 OpenAI 字段**没有对应上游字段**。

### 2.4 响应体

```jsonc
{
  "candidates": [{
    "content": { "role": "model", "parts": [
      { "text": "..." },                                        // 可选，混排时才有
      { "inlineData": { "mimeType": "image/png", "data": "<base64>" } }
    ]},
    "finishReason": "STOP",
    "safetyRatings": [ { "category": "...", "probability": "...", "blocked": false } ]
  }],
  "promptFeedback": { "blockReason": "..." },                   // 仅 prompt 被拦时存在，且此时无 candidates
  "usageMetadata": {
    "promptTokenCount": 25,
    "candidatesTokenCount": 1120,
    "thoughtsTokenCount": 0,
    "totalTokenCount": 1145,
    "promptTokensDetails": [ { "modality": "TEXT", "tokenCount": 25 } ],
    "candidatesTokensDetails": [ { "modality": "IMAGE", "tokenCount": 1120 } ]
  }
}
```

- **一个响应可以有多个 `inlineData` part**（图文混排、多场景配图）→ `data[]` 是数组，长度与
  `n` 不保证相等。适配器不需要改：`n` 只是请求侧语义。
- 输出固定 **PNG**（`mimeType: image/png`）；没有 jpeg / 透明通道开关。
- 有水印（SynthID，隐式、不可关）→ 客户端传 `watermark` 一律忽略（文档需写明）。

### 2.5 错误与「200 但无图」

**HTTP 非 2xx**（引擎统一处理，脚本看不到）：`{"error": {"code": 400, "message": "...", "status": "INVALID_ARGUMENT"}}`。
→ 被 `transport.raise_for_status` 转成 `UpstreamError(code="upstream_http_error", status=502 if >=500 else 400)`，
message 里带 Google 原文（截断 200 字符）。**这条路径已经正确，脚本不用管。**

**HTTP 200 但拿不到图**（脚本必须处理，否则静默无图）：

| 信号 | 含义 | 建议出口 |
|---|---|---|
| `promptFeedback.blockReason` 存在（**无 candidates 数组**） | prompt 被安全策略拦 | 400 `content_filter` |
| `candidates[0].finishReason == "IMAGE_SAFETY"` | 生成图被安全策略拦 | 400 `content_filter` |
| `finishReason == "IMAGE_PROHIBITED_CONTENT"` | 触碰不可调策略 | 400 `content_filter` |
| `finishReason == "PROHIBITED_CONTENT"` | 同上（文本侧枚举） | 400 `content_filter` |
| `finishReason == "NO_IMAGE"` | 模型没出图 | 400 `no_image_generated` |
| `finishReason == "STOP"` 但 parts 里没有 `inlineData` | 模型用文本拒绝了 | 400 `no_image_generated` + 附文本前 200 字 |
| `finishReason == "RECITATION"` / `"IMAGE_RECITATION"` | 复述版权内容 | 400 `content_filter` |
| `finishReason == "MAX_TOKENS"` | 输出被截断 | 502 `upstream_error` |
| `url_retrieval_status == "URL_RETRIEVAL_STATUS_UNSAFE"` | 上游对客户端给的 URL 做内容审核未通过 | 400 `url_retrieval_failed` |
| URL 拉不到（非公开可读 / 内网地址 / 签名过期） | 上游取不到图 | ⚠️ 形状待实测（可能 400 错误体，也可能 200 + 无图）→ P0-10 |

安全过滤的错误码（`raiFilteredReason` 里的 support code）⚠️ 只在 Vertex AI 形态观察到，
Developer API 是否返回需实测；不要把它写进脚本逻辑，只作为日志线索。

### 2.6 输入图的送达方式与体积上限

**三种送法，URL 直通是官方一等公民**（不是 workaround）：

| 方式 | 字段（snake_case / camelCase 双写均可） | 上限 | 适用 |
|---|---|---|---|
| **外部 URL 直通** | `file_data.{mime_type, file_uri}` | 单文件 **100MB**；每请求 **≤10 张图**；URL 必须公开可读或 presigned | 客户端给公网 URL：**零下载、零 base64 膨胀** |
| inline base64 | `inline_data.{mime_type, data}` | ⚠️ **三个口径互相冲突**：Vertex 参考写「图片 7MB」、Firebase 写「整请求 20MB」、Google 博客写「已从 20MB 提升到 100MB」 | 小图 / 客户端本来就给 base64 |
| Files API | 先 `POST /v1beta/files` 拿 handle，再 `file_data.file_uri` | 单文件 2GB，文件保留 48h | 大图、同一张图多次复用 |

官方对 URL 直通的原话（「File input methods」，2026-09-11 检索）：

> You can pass publicly accessible HTTPS URLs or pre-signed URLs (**compatible with S3 Presigned
> URLs** and Azure SAS) directly in your generation request. The Gemini API will fetch the content
> securely during processing. This is ideal for **files up to 100MB** that you don't want to re-upload.

五条必须知道的约束：

1. **给了 `file_uri` 就必须同时给 `mime_type`**（Vertex 参考明确要求）。这意味着直通模式下
   *不能*靠下载图片来 `sniff_mime` —— mime 得从 URL 扩展名推断或由调用方指定（见 4.4）。
2. **URL 必须公开可达**：登录墙 / 付费墙 / 内网地址一律拉不到。私有对象必须换成带有效期与
   权限的 presigned URL（官方支持 S3 presigned，**minio 后端产出的 presigned URL 属于这一类**；
   2026-09-11 起后端可选：`fal` 产出**公开可读**的长期 URL，落在「公开 CDN URL」那一类，因此
   URL 直通模式的成立性对后端不敏感——但两者对**隐私**的含义完全不同，见 5.2）。
3. **上游会对 URL 内容做安全审核**，不合格返回 `url_retrieval_status = URL_RETRIEVAL_STATUS_UNSAFE`
   （意味着第三方 CDN 上的图有被上游拒绝的可能，且这不是我们这边的错，错误信息必须可读）。
4. **启用 VPC Service Controls 时不支持 `file_uri`**（Vertex 场景的限制）。
5. 输入图上限被缩放/填充到 3072×3072（保比例）——参考图细节会被压，和输出档位无关。

inline 上限三个口径互相矛盾，所以脚本策略取**最保守**（≤6MB 走 inline，超过就走 URL），
真实阈值进第 9 节 P0-2 实测。

---

## 3. 差异清单：OpenAI images ↔ Gemini image

| OpenAI 契约 | Gemini | 转换方式 |
|---|---|---|
| `model`（body） | URL 路径段 | 路径改写（4.1） |
| `prompt` | `contents[0].parts[].text` | 直转，位置可调（4.2） |
| `size`（像素） | `aspectRatio` + `imageSize` | 比例化 + 档位化，**语义降级**（4.3） |
| `n` | 无 | `n>1` 明确报错（4.5） |
| `response_format`（url/b64_json） | 只有 b64 | b64 直通 / url 走对象存储（5.2） |
| `image`（URL/dataURI/b64） | `file_data.file_uri` 或 `inline_data` | 按引用形态与体积分流（4.4） |
| `mask` | 无（不支持的语义） | 有 mask 时报 400（不要静默丢弃） |
| `quality` / `style` / `user` | 无 | 忽略（`quality` 可选映射到档位） |
| `usage`（OpenAI 口径） | `usageMetadata`（token 口径） | 双向映射，见 5.3 |
| 错误信封 | `error.{code,message,status}` | 引擎已处理（2.5） |
| 无对应 | 安全拒图（200） | 脚本判定 + `ctx.fail`（5.4/5.5） |
| 无对应 | 图文混排、多图输出 | `data[]` 多元素透出 |

---

## 4. 请求方向转换设计

### 4.1 模型 → URL 路径（最关键的一处）

渠道声明的 `X-Upstream-Url` 是「一个渠道 = 一个端点」，而 Gemini 把模型编进了路径：

```
https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent
                                                    ^^^^^^^^^^^^^^^^^^ 客户端选的模型
```

脚本必须把客户端 `model`（或 `X-Channel-Options.model`）写回路径最后一段：

```python
def _model_url(url, model):
    parts = urlsplit(url)
    head, _, tail = parts.path.rpartition("/models/")     # 保留版本前缀
    if not head:
        return url                                        # 不是 Gemini 形态，交给引擎
    return urlunsplit(parts._replace(path=f"{head}/models/{model}:generateContent"))
```

**必须知道的两件事：**

1. `ctx.emit(url=...)` 会对新 URL **重跑 `check_url`** → 若配置了 `UPSTREAM_HOST_ALLOWLIST`，
   `generativelanguage.googleapis.com` 必须在白名单里。同 host 改写一般无事，但换版本
   （`v1beta` → `v1`）不受影响，换 host（如走自家代理）会被拦。
2. 保留 `:generateContent` 方法后缀与查询串；`?key=` 形态（把 key 放 query）请改用
   `X-Auth-Emit: query:key`，不要手工拼。

别名映射放在脚本内（控制面传什么名字都能兜住）：

```python
ALIASES = {
    "nano-banana": "gemini-2.5-flash-image",
    "nano-banana-2": "gemini-3.1-flash-image",
    "nano-banana-2-lite": "gemini-3.1-flash-lite-image",
    "nano-banana-pro": "gemini-3-pro-image",
}
```

未识别且不含 `gemini-` 的 model → 400（别把一个 OpenAI 模型名当 Gemini 模型发出去）。

### 4.2 字段映射

```python
body = {
    "contents": [{"role": "user", "parts": parts}],          # parts = [text?, inline_data...]
    "generationConfig": {
        "responseModalities": ["TEXT", "IMAGE"] if wants_text else ["IMAGE"],
        "imageConfig": {"aspectRatio": ratio, "imageSize": size}     # 或 responseFormat.image
    },
}
```

- `parts` 顺序：**text 在前、图在后**（与官方示例一致）；提供
  `X-Channel-Options.image_text_order = "text_first" | "text_last"` 兜住"先图后指令"更好的场景
  （多图时官方建议对图编号后再给指令）。
- 透传白名单（调用方给才发）：暂列 `seed`（⚠️ Gemini Image 是否支持需实测）、
  `tools`（含 `google_search`）、`safetySettings`、`systemInstruction`。
  注意 `responseModalities` 一旦带 `tools.google_search` 就必须含 `TEXT`。

### 4.3 `size` → `aspectRatio` + `imageSize`

OpenAI 的 `size` 是**目标像素**，Gemini 是**比例 + 档位**，两者不等价，映射必然是降级：

```
1024x1024 → 1:1  + 1K  → 1024x1024        （精确）
1024x1792 → 9:16 + 1K  → 768x1376         （比例对，像素不精确）
1536x1024 → 3:2  + 2K  → 2528x1696        （升档）
```

算法：

```python
RATIOS = ((1,1),(1,4),(1,8),(2,3),(3,2),(3,4),(4,1),(4,3),(4,5),(5,4),(8,1),(9,16),(16,9),(21,9))

def _ratio(w, h):                      # 取比值的最近邻；平手取更接近方形
    target = w / h
    return min(RATIOS, key=lambda r: (abs(r[0]/r[1] - target), abs(r[0]/r[1] - 1)))

def _tier(long_edge):
    if long_edge <= 768:  return "512"
    if long_edge <= 1024: return "1K"
    if long_edge <= 2048: return "2K"
    return "4K"
```

**两级裁剪**（否则必 400）：

1. 比例裁剪：`gemini-2.5-flash-image` 是否支持极端比例（`1:4/4:1/1:8/8:1`）⚠️ 需实测；
   不支持就回退到 4:3 / 3:4 / 16:9 / 9:16。
2. 档位裁剪：按 2.2 的能力表取**不超过请求档位的最高可用档**
   （`2.5-flash-image` 恒 `1K` 且不发 `imageSize`；`lite` 最高 `1K`）。
   另外 `512` 只在 Lite / 3.1 Flash 上可用，且不带 K。

`quality: high` → 可选映射到 `2K`（渠道选项 `quality_to_size: true` 才生效，默认忽略）。

### 4.4 图片引用三态 → parts（URL 直通优先，按体积分流）

上游支持 `file_data.file_uri`，所以**客户端给的公网 URL 不需要我们下载**，直接透传即可——
这条比 ARK 的"re-host 到公网 URL"更省（零下载、零 base64 膨胀、不受 inline 上限约束）。

| 客户端给的形态 | 默认动作 | 出站字段 |
|---|---|---|
| `http(s)://…` | **直通**，我们不发任何请求 | `file_data.{mime_type, file_uri}` |
| `data:` URI / 裸 base64，解码后 ≤ `inline_max_bytes`（默认 6MB） | 本地转 inline（零网络） | `inline_data.{mime_type, data}` |
| `data:` URI / 裸 base64，解码后 > `inline_max_bytes` | `ctx.upload_temp_image()` → URL 当 file_uri（minio 预签名 / fal 公网，均属上游可拉取的形态） | `file_data.{mime_type, file_uri}` |
| 上面第三条且对象存储不可用 | **413 `image_too_large` 报错**——请求根本组不出来，没有可退回的形态（与输出侧的规则不同，见 5.2） | — |

`X-Channel-Options.image_ref_mode`：

| 值 | 行为 | 何时用 |
|---|---|---|
| `auto`（**默认**） | 按上表分流 | 一般场合 |
| `url` | 一律走 `file_data`（base64 也先上传对象存储） | 想彻底绕开 inline 上限 |
| `inline` | 一律走 `inline_data`（URL 会被 `ctx.download_image` 下载后内联） | 上游网关**不接受**外部 URL 时（若遇到，必须实测确认） |

#### mime 从哪来（直通模式的唯一难点）

给了 `file_uri` 就必须同时给 `mime_type`（2.6 约束 1），而直通模式下我们**故意不下载图片**，
所以不能用 `ctx.sniff_mime()`。三级推断，成本递增：

```python
_EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".webp": "image/webp"}

def _mime_from_url(ref, ctx):
    path = urlsplit(ref).path.lower()
    for ext, mime in _EXT_MIME.items():
        if path.endswith(ext):
            return mime                       # 1) 扩展名，零成本
    return None                               # 2) 交给调用方 / 3) 回落 default_mime_type
```

1. URL 扩展名（`cdn.example.com/a/b.jpg`）——覆盖绝大多数 CDN 链接；
2. 渠道选项 `default_mime_type`（默认 `image/png`）——上游对**不匹配的 mime 通常宽容**，
   ⚠️ 但需实测（P0-9）；
3. 真推不出且不能容忍失败时，才 `await ctx.image_bytes(ref)` 下载 sniff——顺手把该引用
   降级成 inline，代价是回到 2.6 的体积约束。

只认 `http`/`https` 两种 scheme，其它（`ftp://`、`gs://`）直接 400：`gs://` 只有 Vertex
形态能用，且要走 GCS 注册（2.6 的 Files API 行）。

#### 安全语义变更（写进 README，这是显式决策）

- 直通模式下**我们不再发起任何请求**，所以**不存在我方 SSRF** —— 反而比 `inline` 模式更安全。
- 但客户端 URL 会被 **Google 拉取**（数据出境；本项目上游本就在境外，属既定事实），
  且上游会对 URL 做内容审核，不合格回 `URL_RETRIEVAL_STATUS_UNSAFE`。
- 留一个开关：`X-Channel-Options.client_url_passthrough = true|false`（默认 `true`）。
  关掉即一律走 `inline`，此时客户端 URL 会经 `ctx.download_image` → `check_url`，
  受 `upstream_allow_private_network` 与 `UPSTREAM_HOST_ALLOWLIST` 约束
  （历史行为，原文档已注明；**白名单只在这条分支生效**）。
- 两种模式的失败信息要能区分："上游拉不到你的 URL"（直通）vs "我方拒绝下载该 URL"（inline），
  否则排障时会指向错误的层。

多图：URL 方式**每请求 ≤10 张**（Vertex 参考，官方口径），inline 方式只受体积约束
（Firebase 另写「每请求最多 3000 张图」，第三方又传「Pro 支持 14 张参考图」—— 三个数字
来自不同模型/入口，**按最保守的 10 张预检**）。脚本按 `max_input_images`（默认 10）
预检并 400，别让上游去拒。

#### b64 通路的阈值与护栏（**C1~C3 已实施，C4/C5 待确认**）

入口**无法拒绝 base64**：OpenAI 契约允许 `image` 是三态之一，而 `/v1/images/edits` 的上传
还会被 route 层主动转成 data URI（`adapter/api/image_edits.py:86`）。所以唯一可控的决策点是
**b64 在上游请求体里停留多久**。先看这笔账：

| 成本项 | 原样转发 b64（inline） | 直通 URL / re-host |
|---|---|---|
| 我方 CPU | 1 次解码 + 1 次编码；**edits 路径共 3 次编解码**（route `b64encode` → 脚本 `decode_b64` → inline `encode_b64`） | URL 引用 0 次；re-host 1 次解码 + 上传 |
| 内存峰值 | JSON 直传 ≈ **4.1X**（入站 body 1.37X + 解析串 1.37X + 解码 bytes X + 出站序列化 1.37X）；edits ≈ 3.7X。20MB 图 ≈ **80MB/请求** | ≈ 0；re-host 2~3X，且出站 body 只有几百字节 |
| 出站带宽 | 1.37X 走**我方上行**（跨境） | 几百字节 |
| 硬上限 | 入站 64MB → 单图 20MB → **上游 inline 最紧**（保守 7MB/图 ≈ 原图 5.25MB；多图或按 20MB/请求算） | 100MB，且不吃我们的 body |
| 失败模式 | 413 / 400，往往打到上游才知道 | 上游拉不到 URL，错误可读 |
| 留存 | 不落盘 | 写进我方对象存储（`[<prefix>/]<yyyymmdd>/<request_id>/<uuid>.<ext>`，日期段恒定、前缀默认空）。**留存期取决于后端**：minio 预签名 URL（TTL 可配，默认 1h，上限 7d）；fal 公网长期、不过期 |

两条决定性事实：`binascii` **不释放 GIL**（编解码串行占 event loop，≈0.5ms/MB/次，5MB 图走
edits+inline ≈ 7~8ms 纯 CPU）；以及**上游 inline 上限是三层限制里最紧的** —— 所以"inline 大图"
不是成本选择题，而是**过线必 400**。反向也有两条好处：b64 不落盘（敏感图）、不依赖对象存储。

| # | 变更 | 现状 → 建议 | 依据 |
|---|---|---|---|
| C1 | 单图 inline 阈值 | `inline_max_bytes` 6MB → **4MB**（✅ 已实施） | 上游最紧口径 7MB/图 ÷ 4/3 ≈ 5.25MB，留余量。P0-2 实测后若确认是 20MB/100MB，可放宽到 12MB / 28MB |
| C2 | 每请求累计护栏 | 新增 `inline_total_max_bytes`（默认 **6MB**），超出的图**逐张 re-host**（`parts` 允许 `file_data` 与 `inline_data` 混用）（✅ 已实施） | 上游可能按请求总量限（Firebase 口径 20MB/请求）；原草案只判单张，多张"都不超"仍会整体超线 |
| C3 | **原草案缺陷**：`over` 判定未限定模式 | `image_ref_mode=inline` 时必须真的恒 inline（✅ 已实施） | 显式模式的价值就是可预测；混用行为会让"我明明设了 inline"无法排障 |
| C4 | 引擎侧 `ctx.fail()`（独立于本渠道） | ✅ 已实施（`adapter/ctxapi/fault.py` + `SCRIPT_API` 门禁 + 2 项行为测试） | 5.5 节：脚本原本无错误通道，安全拦截只能 500 或 200 空 data |
| C5 | edits 路径的 data URI 中间形态（**仅记录，暂不实施**） | `image_edits.py` 上传 → `data:` URI → 脚本解码；若要 inline 再编码一次 = 3 次编解码。备选：route 层直传 bytes 或直转 MinIO | C5 会改到所有上游的行为（每个 edits 请求多一次对象存储往返），不该默认开 |

附带的一处**性能实现**（已含在 C1~C3 里）：判定顺序改成"**先按字符串长度估算、再决定要不要解码**"，
并且 data URI 命中快路径时用 `ctx.image_b64()` 直接拿结果（前缀自带 mime，省掉重编码）；
裸 base64 无 mime 可用，才必须解码一次。实测见附录 C 的性能表。

若最终**必须** inline 大图，省 CPU 的写法是优先用 `ctx.image_b64()` 快路径（data URI 直接剥
前缀返回，省掉重编码），并用**字符串长度**估算体积（b64 长度 × 3/4），不必先解码；只有裸
base64 缺 mime 时才需要解码一次去 sniff。

> **实施状态**：C1~C4 已实施 —— C1~C3 在脚本里（`script_store/google/images@v1.py`），
> C4 在引擎里（`adapter/ctxapi/fault.py`），并有逐路径基准（附录 C.2）与 18 项端到端测试兜底。
> **C5（route 层直转）未实施**：它会影响所有上游的行为，单独评估。
> 附录 A 保留设计原文（含注释里的取舍说明），**可执行版本以仓库文件为准**。

### 4.5 `n` / `mask` / `watermark` 的处理决策

- **`n > 1` → 400 `unsupported_parameter`**（"Gemini returns one image per request; send
  separate requests"）。不做串行 repeat：那会把单请求压到 `UPSTREAM_TIMEOUT`（180s）以上、
  计费 ×n、且失败语义不清（部分成功怎么回？）。要做也应在上层控制面 fan-out。
- **`mask` → 400**（Gemini 没有 mask 语义，静默丢弃等于骗调用方）。
- **`watermark` → 忽略**（SynthID 不可关）；文档注明，不要假装支持。

### 4.6 多轮 / chat（v2，本方案不实现）

`/v1/chat/completions` 也能打 Gemini（把 messages 折成 `contents[]` 多轮，image parts 折成
`inline_data`），从而支持"多轮改图"。注意 `payload["messages"][i]["content"]` 既可能是
字符串也可能是 parts 数组；Gemini 的多轮编辑还需要把**上一轮返回的图**带回下一轮
（客户端得持有并回传），这部分需求先不承诺。

> **2026-09-11 更新**：`/v1/chat/completions` 已作为前门实现**单轮**折叠（见 `docs/09` §3），
> 历史截断到最后一条 user 轮，前序轮次不出站。本节描述的**真多轮**
> （`contents[]` 多轮 + 上一轮图回传）**仍不实现**，本节其余内容继续有效。

---

## 5. 响应方向转换设计

### 5.1 parts → `data[]`

```python
def _collect(payload):
    out = []
    for cand in payload.get("candidates", []) or []:
        for part in (cand.get("content") or {}).get("parts", []) or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                out.append((inline.get("mimeType") or inline.get("mime_type") or "image/png",
                            inline["data"]))
    return out
```

注意 `inlineData` / `inline_data` **两种拼写都要认**（不同网关回不同形态，2.4 节）。

### 5.2 `response_format` 的出口

| 客户端要 | 做法 | 备注 |
|---|---|---|
| `b64_json`（推荐默认） | 原样透传 base64 | 零额外成本，最贴近上游 |
| `url` | response 相位 `await ctx.upload_temp_image(raw_bytes, ext)` → URL | 需要对象存储；**返回的 URL 生命周期取决于 `STORAGE_BACKEND`**：`minio` 给预签名 URL，TTL = `TEMP_IMAGE_TTL`（默认 3600s），比 OpenAI/ARK 的 24h 短；`fal` 给**公网长期** URL，不过期（2026-09-11 定调：不控制失效时间）。文档必须写明当前部署是哪一种 |
| 什么都不传 | 由渠道选项 `default_response_format` 决定，默认 `b64_json` | 理由：Gemini 场景下 url 会多一次对象存储往返，且 minio 后端只有 1h 有效期，默认值应偏向无损 |

**无对象存储且客户端要 `url` 时**（2026-09-11 定调，与 `openai/images@v1` 统一）：不报错、
也**不把 data URI 塞进 `data[].url`**，而是退回上游自身的形态——Gemini 只有 base64，所以该项
出 `b64_json`。调用方要的是链接却拿到 base64，这是可接受的失望；为我们的配置缺口去失败请求
才是不可接受的（`docs/02` BR-008 原本就写「不报错」，先前脚本里的 502 是偏离）。
「无对象存储」指当前后端缺必填项：`minio` 看 `MINIO_ENDPOINT`，`fal` 看 `FAL_KEY`。
对外承诺链接的部署必须把所选后端配齐。

### 5.3 `usage` 映射（计费口径）

控制面按 token 计费，所以不能只透传原始字段。建议同时给两份：

```jsonc
{
  "usage": {
    "input_tokens": 25,
    "output_tokens": 1120,                 // 含 thoughtsTokenCount
    "total_tokens": 1145,
    "input_tokens_details":  { "text_tokens": 25, "image_tokens": 0 },
    "output_tokens_details": { "image_tokens": 1120, "text_tokens": 0 }
  },
  "gemini_usage": { /* usageMetadata 原样，便于对账与排障 */ }
}
```

`*_tokens_details` 从 `promptTokensDetails` / `candidatesTokensDetails[].modality` 拆；
若 modality 明细缺失，退化为 **`output_tokens = candidatesTokenCount + thoughtsTokenCount`、
`image_tokens = output_tokens`**（图像模型下这个近似是对的）。
`total_tokens` 缺失时用 `prompt + candidates + thoughts` 兜底 —— **别让 usage 变成 null，
控制面会算成 0 计费**。

### 5.4 安全拦截 / 无图的出口

映射表见 2.5。message 里带上模型给出的拒绝文本（前 200 字），并把
`finishReason` 放进 `code`：

```json
{"error": {"message": "Gemini refused to generate this image (IMAGE_SAFETY): <模型原文…>",
           "type": "invalid_request_error", "param": "prompt", "code": "content_filter"}}
```

这类错误**不可重试**（同 prompt 必再被拦），控制面应据此停止重试 —— 这正是要用 400 而不是 500 的原因。

### 5.5 引擎侧最小改动：`ctx.fail()`

现状（已核实）：脚本能用的 API 只有 `encode_b64 / decode_b64 / data_uri / is_url /
is_data_uri / sniff_mime / download_image / upload_temp_image / image_bytes / image_b64 /
image_data_uri / image_url / emit / sleep / image / remaining / deadline / SKIP`，
**没有错误通道**；而 `executor._call_phase` 对 `AdapterError` 是原样 `raise` 的。

因此加一个 mixin 即可（`adapter/ctxapi/fault.py`）：

```python
class FaultMixin(CtxMixin):
    """Gives a script a typed way to fail a request (upstream 200 = failure)."""

    def fail(self, message: str, *, code: str | None = None, param: str | None = None,
             status: int = 400, err_type: str = "invalid_request_error") -> None:
        raise AdapterError(status, message, err_type, param, code)
```

配套改动（各一行）：

1. `adapter/ctxapi/__init__.py`：`CTX_MIXINS` 加 `FaultMixin`；`AdapterContext` 基类列表加它。
2. `tests/unit/test_ctx_composition.py::SCRIPT_API` 加 `"fail"`（这是钉住脚本可见面的门禁）。
3. README 的 ctx API 小节 + 本文档。

不引入它的话，Google 渠道的安全拦截只能表现为 500，或者更糟——200 + 空 `data`。

---

## 6. 渠道配置

### 6.1 控制面 headers

| Header | 值 |
|---|---|
| `X-Upstream-Url` | `https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent` |
| `X-Script-Ref` | `google/images@v1` |
| `Authorization` | `Bearer <GEMINI_API_KEY>`（凭据仍走标准头，值会被 `upstream_key` 取走） |
| `X-Auth-Emit` | `header:x-goog-api-key`（**裸 key，无前缀**） |
| `X-Channel-Options` | `{"default_response_format":"b64_json","image_ref_mode":"inline"}` |
| `X-Async` | 不需要（同步返回） |
| `X-Stages` | 不需要 |

### 6.2 端到端调用示例

```bash
curl -X POST http://localhost:8080/v1/images/generations \
  -H 'Content-Type: application/json' \
  -H 'X-Upstream-Url: https://generativelanguage.googleapis.com/v1beta/models/gemini-3-pro-image:generateContent' \
  -H 'X-Script-Ref: google/images@v1' \
  -H 'X-Auth-Emit: header:x-goog-api-key' \
  -H 'Authorization: Bearer '"$GEMINI_API_KEY" \
  -H 'X-Channel-Options: {"default_response_format":"b64_json"}' \
  -d '{"model":"nano-banana-pro","prompt":"一只戴墨镜的橘猫，摄影棚布光","size":"1024x1024","response_format":"b64_json"}'
```

改图（同端点，`image` 触发编辑语义）：

```bash
curl -X POST http://localhost:8080/v1/images/edits \
  -H 'X-Upstream-Url: .../models/gemini-3-pro-image:generateContent' \
  -H 'X-Script-Ref: google/images@v1' \
  -H 'X-Auth-Emit: header:x-goog-api-key' \
  -H 'Authorization: Bearer '"$GEMINI_API_KEY" \
  -F 'model=nano-banana-pro' \
  -F 'prompt=把背景换成星空，保留人物姿势' \
  -F 'image=@./photo.png'
```

---

## 7. 与适配器既有约束的交界

| 既有约束 | 现状 | 对 Google 渠道的影响 / 处置 |
|---|---|---|
| `UPSTREAM_TIMEOUT` | 默认 60s，`.env` 已设 180s | 3.x Pro 带 thinking，同步出图可能 30~90s；**180s 是硬下限**，4K 建议实测后调 |
| `MAX_UPSTREAM_BYTES` | 64MB | 4K PNG 的 base64 约 15~35MB，够用；但**响应相位若还要在此基础上转 URL 上传，峰值内存 ×2** |
| `MAX_REQUEST_BYTES` | 64MB（入站） | inline 上限有 7/20/100MB 三口径（P0-2）；**URL 直通模式下这条约束直接消失**（图片不进我们的 body），这正是 `auto` 模式要按体积分流的原因 |
| `TEMP_IMAGE_TTL` | 3600s | **仅 minio 后端**：只控制预签名有效期，不控制对象何时被删除（那属于桶生命周期规则，代码不管）。OpenAI/ARK 客户端习惯 24h，故 url 形态的有效期差异要写进文档；fal 后端无此概念 |
| `UPSTREAM_HOST_ALLOWLIST` | 默认空 | 若启用，必须放 `generativelanguage.googleapis.com`；同时注意它**也会**限制 `ctx.download_image`（4.4） |
| 连接池 `HTTP_POOL_LIMIT_PER_HOST` | 150/100（默认 per-host 值） | Gemini 与本项目其它上游**共用** `app.state.http`，同 host 独占一个 per-host 池；单渠道压测前先看 `per-host` 值 |
| 重试策略 | 不自动重试 POST | 保持不动：Gemini 图片计费按 token，重试 = 重复计费（与 ARK 同一理由） |
| 幂等 | 无 | 客户端超时重发会**重复生成、重复计费**；上游无 idempotency key 可用，只能靠控制面去重 |

---

## 8. 落地步骤

| 阶段 | 内容 | 产出 | 状态 |
|---|---|---|---|
| 1 | 引擎改动：`ctx.fail` mixin + 门禁 | `adapter/ctxapi/fault.py` + 单测 | ✅ 完成 |
| 2 | 写 `script_store/google/images@v1.py`（附录 A 为设计原文） | 脚本 + `manifest.json` 登记 | ✅ 完成 |
| 3 | 单测：比例/档位/别名/URL 改写等纯函数直测 | `tests/unit/test_google_images_script.py`（38 项） | ✅ 完成 —— **写下的当场就抓到 `_tier()` 的 4K 误判**（见附录 C.3 第 4 条） |
| 4 | **真实 key 实测**（第 9 节清单），按结果校准数值表 | 实测记录 | ✅ **第一轮已完成**（`api.chatfire.cn`，见附录 D/D.5）：文生 + 图生（b64/url）、1K~4K、两代字段形态、`fileData` 拼写、mime 容忍度、URL 拉取失败形状；并据此修掉 camelCase 缺陷。剩 P0-5（该网关无此模型）与 P0-6（未构造拦截请求） |
| 5 | 集成测试：假 Gemini 钉住出站形状 | `tests/integration/test_google_images_script.py`（19 项）+ 可复用探针 `tools/` | ✅ 完成（19 项含 camelCase 门禁） |
| 6 | 文档：README 的 ctx API 与渠道示例、本文件状态改「已实测」 | 文档 | ⏳ README 的 `ctx.fail` 已同步（API 必改项）；渠道示例见本文档第 6 章；「已实测」待阶段 4 |

---

## 9. 待实测清单（P0 = 不确认就没法写脚本）

> **2026-09-11 实测进展**（网关 `api.chatfire.cn`，详见附录 D）：P0-1 / P0-3 / P0-8 / P0-11 首轮确认；
> P0-9 / P0-10 第二轮补齐（D.5）；**P0-2 上探到 ≥ 21.6MB**；P0-6 用 `safetySettings` 触不到（该网关忽略
> 该字段），改用噪声图触发 `IMAGE_RECITATION` 并真机走通 400 `content_filter`（D.6）。**只剩 P0-5 测不了**
> （该网关没配 2.5-flash 渠道）。
> **已修并复验的两处缺陷**：① `file_data` → camelCase（`i2i_url` 502 → 200）；② **`_tier()` 的 4K 误判**
> （补齐纯函数单测的当场被抓到，见附录 C.3 第 4 条）。

| # | 项 | 怎么测 | 判据 |
|---|---|---|---|
| P0-1 | 字段形态：`imageConfig` vs `responseFormat.image` | 同一模型两种 body 各打一次 | 哪个 200；另一个报什么。**已有实证样例跑通 `imageConfig`**（附录 B），仍要确认 3.1 代模型 |
| P0-2 | inline 体积上限真实值（**7MB / 20MB / 100MB 三口径并存**） | 依次 5/10/15/21MB base64 单图 | ✅ **实测 ≥ 21.6MB 仍 200**（10 / 15.3 / 21.6MB 三档通过）→ 本网关不卡内联体积；`inline_max_bytes` 仍按最弱上游保守取 4MB |
| P0-3 | 各模型可用 `imageSize` / 极端比例 | 交叉矩阵（4 模型 × 档位 + 4 极端比例） | 400 清单，据此收紧 4.3 的裁剪表 |
| P0-4 | `v1beta` vs `v1` | 同一请求两版本 | 均可 / 仅其一 |
| P0-5 | 2.5 Flash 是否接受 `imageSize` | 显式发 `1K` / 不发 | 400 则脚本对它一律不发该字段 |
| P0-6 | 安全拦截的真实响应形状 | ~~打一个必被拦的 prompt~~（不构造违规内容）→ **`safetySettings` 最低阈值也拦不住**（该网关忽略此字段）；改用**噪声图**触发 `IMAGE_RECITATION` | ✅ 走 `finishReason` 分支（不是 `promptFeedback`），脚本转 400 `content_filter` —— 已真机验证，见附录 D.6 |
| P0-7 | `usageMetadata` 字段与 token 数 | 1K / 2K / 4K 各一次 | 是否等于 2.2 表；`thoughtsTokenCount` 是否出现 |
| P0-8 | **URL 直通的两条来源**（已确认支持，验证我们这两种 URL 能用） | ① 公开 CDN 图 URL；② 我方 MinIO presigned URL（s3ai.cn 域名） | 均 200 才算 `auto` 模式成立；②失败则大图只能走 inline/Files API |
| P0-9 | `mime_type` 与实际格式不匹配时的容忍度 | 同一张 PNG 分别标 `image/png` / `image/jpeg` / 乱写 | 报错还是照收 → 决定 4.4 的 mime 推断能放到多宽 |
| P0-10 | URL 拉取失败的响应形状 | 传一个内网地址 / 已过期 presigned URL | HTTP 码 + 错误体字段（是否有 `url_retrieval_status`） |
| P0-11 | 图像模型是否都接受 `file_data` | 4 个模型 × URL 输入各一次 | 有没有只支持 inline 的模型（尤其 2.5-flash-image） |
| P1-1 | 4K 出图耗时分布（n≥5） | 计时 | 决定 `UPSTREAM_TIMEOUT` 是否要 >180s |
| P1-2 | 一次响应是否可能返回多张 | 出图文混排 prompt | `data[]` 长度 >1 的实例 |
| P1-3 | `responseModalities` 最小值 | 只发 `["IMAGE"]` | 是否报 "must include TEXT" |
| P1-4 | Google 能否拉到我们的 MinIO presigned URL（s3ai.cn） | 用 P0-8 的 URL | 若被拒则 `url` 模式只用 inline |

> 沙箱限制：本机 curl 到 `*.googleapis.com` 会 502（代理），P0 全部需要在能直连 Google 的
> 环境（或控制面所在机器）上跑；`mock_upstream` 只能钉形状，不能替代实测。

---

## 10. 验收标准

**单测（无需网络）**
- 比例/档位映射：`1024x1024 → 1:1/1K`；`1536x1024 → 3:2/2K`；`2.5-flash-image` 请求 4K 时被裁到 `1K` 且不发 `imageSize`。
- **引用分流**：`http(s)` 引用 → `file_data.file_uri`，且**断言没有发生任何下载**（假 ctx 上 `image_bytes` 未被调用）；base64 ≤ 阈值 → `inline_data`；base64 超阈值 → 走 `upload_temp_image`，对象存储缺失时报 400 而非降级成 data URI。
- **mime 推断**：`.jpeg → image/jpeg`；无扩展名 → `default_mime_type`；`ftp://` / `gs://` → 400；`client_url_passthrough=false` 时 URL 引用改走下载分支。
- 别名：`nano-banana-pro → gemini-3-pro-image`；未知 OpenAI 模型名 → 400。
- URL 改写：保留 scheme/host/`:generateContent`/query，只换模型段。
- `n>1`、带 `mask` → 400，code 明确。
- 响应收集：只认 `inlineData`/`inline_data` 两种拼写；无图 → `no_image_generated`；`IMAGE_SAFETY` → `content_filter`。
- usage：`usageMetadata` → OpenAI 口径，`output_tokens` 含 thoughts；缺字段时不产生 null。

**集成（mock upstream）**
- 出站 body 断言：无 `n`/`size`/`response_format`；`parts[0]` 是 text；URL 引用出现在 `file_data.file_uri` 且与入参**逐字符一致**（不重写 host，不做 re-host）。
- `response_format=url` 时走对象存储（用 `tests/integration/conftest.py` 的假 storage 断言**发生了一次上传**、且返回 `https://cdn.test/...` 链接）；无对象存储时**不报错**，该项退回 `b64_json` 且**不出现 `url` 键**。
- 上游 200 但无图 → 客户端收到 400 而非 200。

**性能（可机械断言，不需要压测）**
- URL 引用路径：`image_bytes` / `decode_b64` / `encode_b64` 调用次数**均为 0**，且 `download_image` 未被触发。
- data URI 小图 inline：`encode_b64` 调用次数**为 0**（快路径生效，禁止退化成 decode+encode）。
- 裸 base64：`decode_b64` **恰好 1 次**（mime 只能靠它拿），不得出现第 2 次。
- 累计超 `inline_total_max_bytes` 时：超限的每一张走 `upload_temp_image`，upload 次数 = 超限张数。
- 纯转换 CPU（不含编解码）：URL 直通量级 **< 100 µs**（实测 7 µs，见附录 C.2）。

**端到端（真实 key，P0 全部通过后）**
- 文生图、单图编辑、多图合成各一次，`data[0].b64_json` 可解码为 PNG。
- 安全拦截的 prompt 拿到 400 `content_filter`，而不是 200 空 data 或 500。

---

## 附录 A：脚本设计（可执行版本已落库）

> **已落库**：`script_store/google/images@v1.py`
> sha256 `65d33c7acd03e3a2e981a9fbcb80bf0d4eca46a53b13f59316e23e12b52a9301`（与 `manifest.json` 一致；
> 该版本含实测后的 camelCase 修正与 `_tier()` 边界修复），
> 测试 `tests/integration/test_google_images_script.py`（18 项）。
> 下面保留设计原文（注释里写着每处取舍的理由）；**改动请改仓库文件，并同步重算 manifest 摘要**。


```python
"""google/images@v1: Gemini Image ("Nano Banana") through generateContent.

Channel setup (New API side):
  X-Upstream-Url: https://generativelanguage.googleapis.com/v1beta/models/
                  gemini-3-pro-image:generateContent
  X-Script-Ref:   google/images@v1
  Authorization:  Bearer <GEMINI_API_KEY>
  X-Auth-Emit:    header:x-goog-api-key          (bare key, no prefix)
  X-Channel-Options: {"default_response_format": "b64_json",
                      "image_ref_mode": "auto",            auto|url|inline
                      "inline_max_bytes": 4194304,         per image
                      "inline_total_max_bytes": 6291456,   per request
                      "default_mime_type": "image/png",
                      "client_url_passthrough": true,
                      "image_config_style": "auto"}        imageConfig|responseFormat|auto

Input accepts three shapes and the upstream can fetch URLs itself: a client URL is
forwarded as file_data.file_uri (no download, no base64 inflation), inline data is
sent as inline_data until it outgrows inline_max_bytes and is then re-hosted as a URL.
Output is base64 only (inlineData), so response_format=url means uploading to the configured object store.
"""

from urllib.parse import urlsplit, urlunsplit

ALIASES = {
    "nano-banana": "gemini-2.5-flash-image",
    "nano-banana-2": "gemini-3.1-flash-image",
    "nano-banana-2-lite": "gemini-3.1-flash-lite-image",
    "nano-banana-pro": "gemini-3-pro-image",
}

# Per-model capabilities. "sizes" is ordered low -> high; empty means the model
# fixes its own resolution and must not receive imageSize at all.
MODELS = {
    "gemini-2.5-flash-image": {"sizes": (), "wide": False},
    "gemini-3.1-flash-lite-image": {"sizes": ("512", "1K"), "wide": True},
    "gemini-3.1-flash-image": {"sizes": ("512", "1K", "2K", "4K"), "wide": True},
    "gemini-3-pro-image": {"sizes": ("1K", "2K", "4K"), "wide": True},
}
DEFAULT_MODEL = "gemini-3-pro-image"

RATIOS = ((1, 1), (1, 4), (1, 8), (2, 3), (3, 2), (3, 4), (4, 1), (4, 3),
          (4, 5), (5, 4), (8, 1), (9, 16), (16, 9), (21, 9))
NARROW = frozenset({(1, 4), (1, 8), (4, 1), (8, 1)})

CONTENT_FILTER = frozenset({"IMAGE_SAFETY", "IMAGE_PROHIBITED_CONTENT",
                            "PROHIBITED_CONTENT", "IMAGE_RECITATION", "RECITATION"})

_STATE = {}          # request_id -> {"response_format": str}; see _remember()


def _model(name):
    name = (name or "").strip()
    return ALIASES.get(name, name) or DEFAULT_MODEL


def _caps(model):
    return MODELS.get(model, MODELS[DEFAULT_MODEL])


def _model_url(url, model):
    parts = urlsplit(url)
    head, sep, _ = parts.path.rpartition("/models/")
    if not sep:
        return url
    return urlunsplit(parts._replace(path=f"{head}/models/{model}:generateContent"))


def _size_pair(size):
    try:
        w, h = (int(v) for v in str(size).lower().split("x", 1))
    except (TypeError, ValueError):
        return 1024, 1024
    return (w, h) if w > 0 and h > 0 else (1024, 1024)


def _ratio(w, h, caps):
    target = w / h
    best = min(RATIOS, key=lambda r: (abs(r[0] / r[1] - target), abs(r[0] / r[1] - 1)))
    if best in NARROW and not caps["wide"]:
        best = min((r for r in RATIOS if r not in NARROW),
                   key=lambda r: abs(r[0] / r[1] - target))
    return f"{best[0]}:{best[1]}"


ORDER = {"512": 0, "1K": 1, "2K": 2, "4K": 3}   # 显式档位序；不要靠字符串比较


def _tier(w, h, caps):
    if not caps["sizes"]:
        return None                       # fixed-resolution model: omit imageSize
    edge = max(w, h)
    want = "512" if edge <= 768 else "1K" if edge <= 1024 else "2K" if edge <= 2048 else "4K"
    allowed = [s for s in caps["sizes"] if ORDER[s] <= ORDER[want]]
    return max(allowed or caps["sizes"], key=lambda s: ORDER[s])


def _image_config(style, ratio, size):
    inner = {"aspectRatio": ratio}
    if size is not None:
        inner["imageSize"] = size        # 1K/2K/4K uppercase K -- "1k" is rejected
    if style == "responseFormat":
        return {"responseFormat": {"image": inner}}
    return {"imageConfig": inner}


_MIME_BY_EXT = ((".png", "image/png"), (".jpg", "image/jpeg"),
                (".jpeg", "image/jpeg"), (".webp", "image/webp"))


def _declared_mime(ctx, ref):
    """A file_uri needs a mime_type, and we deliberately avoid downloading to sniff one."""
    path = urlsplit(ref).path.lower()
    for ext, mime in _MIME_BY_EXT:
        if path.endswith(ext):
            return mime
    return str(ctx.options.get("default_mime_type", "image/png"))


def _uri_part(ref, mime):
    return {"file_data": {"mime_type": mime, "file_uri": ref.strip()}}


def _passthrough(ctx):
    return ctx.options.get("client_url_passthrough", True) is not False


async def _hosted_part(ctx, ref, mime, raw):
    """Re-host bytes and hand the upstream a URL -- the way past the inline cap."""
    url = await ctx.upload_temp_image(raw, ext=mime.split("/", 1)[-1])
    if url.startswith("data:"):
        # upload_temp_image degrades to a data URI when storage is off; for images
        # that big it is not a usable answer, so fail loudly instead of silently.
        ctx.fail("Image is too large for inline parts and object storage is unavailable",
                 param="image", code="image_too_large", status=413)
    return _uri_part(url, mime)


def _b64_size(ref):
    """Decoded size of an encoded reference, from its length -- without decoding.

    Padding makes this a slight over-estimate (4 chars -> exactly 3 bytes), which
    errs towards re-hosting: the safe direction whenever a cap is in play.
    """
    if ref.startswith("data:"):
        _, _, ref = ref.partition(",")
    return len(ref) * 3 // 4


def _data_uri_mime(ref):
    """The mime a data URI already carries -- cheaper than decoding to sniff one."""
    return ref[5:].split(";", 1)[0] or "image/png"


async def _bytes_part(ctx, ref, data, mime, mode, allowance):
    """Bytes in hand: hand the upstream a URL, or inline them if they fit."""
    per_image = int(ctx.options.get("inline_max_bytes", 4 * 1024 * 1024))
    over = mode == "url" or (
        mode == "auto" and (len(data) > per_image or len(data) > allowance[0])
    )
    if over:
        return await _hosted_part(ctx, ref, mime, data)
    if mode == "auto":
        # Only auto mode meters the request-wide budget; "inline" is unconditional
        # by definition, so mixing shapes there would just make it unpredictable.
        allowance[0] -= len(data)
    return {"inline_data": {"mime_type": mime, "data": ctx.encode_b64(data)}}


async def _ref_part(ctx, ref, mode, allowance):
    """Three shapes, four paths. Never fetch or encode what can be avoided.

    `allowance` is a one-element cell holding what is left of the request's inline
    budget: the upstream cap that matters is per request, so several individually
    small images can still overflow it.
    """
    ref = ref.strip()
    per_image = int(ctx.options.get("inline_max_bytes", 4 * 1024 * 1024))

    if ctx.is_url(ref):
        if mode != "inline" and _passthrough(ctx):
            return _uri_part(ref, _declared_mime(ctx, ref))       # zero download
        data = await ctx.image_bytes(ref)                         # we must fetch it
        return await _bytes_part(ctx, ref, data, ctx.sniff_mime(data), mode, allowance)

    if mode == "auto" and _b64_size(ref) <= min(per_image, allowance[0]):
        if ctx.is_data_uri(ref):
            # Cheapest possible inline: the prefix already carries the mime and
            # image_b64 validates-and-returns the string without re-encoding it.
            blob = await ctx.image_b64(ref)
            allowance[0] -= _b64_size(blob)
            return {"inline_data": {"mime_type": _data_uri_mime(ref), "data": blob}}

    # Bare base64 only: no mime anywhere, so one decode is unavoidable -- and with
    # the bytes in hand, encoding them is the cheap half.
    data = await ctx.image_bytes(ref)
    return await _bytes_part(ctx, ref, data, ctx.sniff_mime(data), mode, allowance)


def _remember(ctx, fmt):
    if len(_STATE) > 512:                                # 防止异常路径把状态表撑爆
        for key in list(_STATE)[:256]:
            _STATE.pop(key, None)
    _STATE[ctx.request_id] = {"response_format": fmt}


def _collect(payload):
    out = []
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            inline = part.get("inlineData") or part.get("inline_data")
            if isinstance(inline, dict) and inline.get("data"):
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                out.append((mime, inline["data"]))
    return out


def _refusal_text(payload):
    for cand in payload.get("candidates") or []:
        for part in (cand.get("content") or {}).get("parts") or []:
            if part.get("text"):
                return str(part["text"])[:200]
    return ""


def _usage(payload):
    meta = payload.get("usageMetadata") or {}
    prompt = int(meta.get("promptTokenCount") or 0)
    candidates = int(meta.get("candidatesTokenCount") or 0)
    thoughts = int(meta.get("thoughtsTokenCount") or 0)
    output = candidates + thoughts
    images = 0
    for detail in meta.get("candidatesTokensDetails") or []:
        if str(detail.get("modality", "")).upper() == "IMAGE":
            images += int(detail.get("tokenCount") or 0)
    return {
        "input_tokens": prompt,
        "output_tokens": output,
        "total_tokens": int(meta.get("totalTokenCount") or (prompt + output)),
        "input_tokens_details": {"text_tokens": prompt, "image_tokens": 0},
        "output_tokens_details": {"image_tokens": images or output,
                                  "text_tokens": output - (images or output)},
    }


async def _response(ctx, payload):
    reason = ""
    for cand in payload.get("candidates") or []:
        reason = cand.get("finishReason") or ""
        break

    images = _collect(payload)
    if not images:
        block = (payload.get("promptFeedback") or {}).get("blockReason")
        if block or reason in CONTENT_FILTER:
            ctx.fail(f"Gemini blocked this request ({block or reason})"
                     + (f": {_refusal_text(payload)}" if _refusal_text(payload) else ""),
                     code="content_filter", param="prompt")
        if reason == "MAX_TOKENS":
            ctx.fail("Gemini ran out of output tokens before producing an image",
                     code="upstream_error", status=502)
        ctx.fail("Gemini returned no image"
                 + (f" ({reason})" if reason else "")
                 + (f": {_refusal_text(payload)}" if _refusal_text(payload) else ""),
                 code="no_image_generated", param="prompt")

    fmt = (_STATE.pop(ctx.request_id, {}) or {}).get("response_format")
    fmt = fmt or ctx.options.get("default_response_format", "b64_json")

    data = []
    for mime, blob in images:
        if fmt == "url":
            raw = ctx.decode_b64(blob)
            ext = mime.split("/", 1)[-1] or "png"
            data.append({"url": await ctx.upload_temp_image(raw, ext=ext)})
        else:
            data.append({"b64_json": blob})
    return {"created": payload.get("created", 0), "data": data,
            "usage": _usage(payload), "gemini_usage": payload.get("usageMetadata") or {}}


async def transform(ctx, payload, phase):
    if phase != "request":
        return await _response(ctx, payload)

    model = _model(payload.get("model") or ctx.options.get("model"))
    if not model.startswith("gemini-"):
        ctx.fail(f"Unknown model for this channel: {model!r}", param="model")
    caps = _caps(model)
    ctx.emit(url=_model_url(ctx.upstream_url, model))

    n = payload.get("n", 1)
    if isinstance(n, int) and n > 1:
        ctx.fail("This upstream returns one image per request; send 'n' separate requests",
                 param="n", code="unsupported_parameter")
    if payload.get("mask"):
        ctx.fail("This upstream has no mask/inpainting parameter",
                 param="mask", code="unsupported_parameter")

    response_format = payload.get("response_format")
    if response_format not in (None, "url", "b64_json"):
        response_format = None
    _remember(ctx, response_format)

    w, h = _size_pair(payload.get("size"))
    ratio = _ratio(w, h, caps)
    size = _tier(w, h, caps)
    style = ctx.options.get("image_config_style", "auto")
    if style == "auto":
        style = "responseFormat" if model.startswith("gemini-3.1") else "imageConfig"

    wants_text = bool(ctx.options.get("response_modalities_text")) or bool(payload.get("tools"))
    parts = []
    image = payload.get("image")
    refs = image if isinstance(image, list) else ([image] if image else [])
    mode = ctx.options.get("image_ref_mode", "auto")
    # file_uri inputs cap at 10 per request upstream; inline count is bounded by size
    # instead. 10 is the safe default for both paths.
    limit = int(ctx.options.get("max_input_images", 10))
    if len(refs) > limit:
        ctx.fail(f"At most {limit} input images are supported here", param="image")

    allowance = [int(ctx.options.get("inline_total_max_bytes", 6 * 1024 * 1024))]
    ref_parts = [await _ref_part(ctx, ref, mode, allowance) for ref in refs]
    text_part = {"text": payload.get("prompt", "")}
    if refs and ctx.options.get("image_text_order") == "text_last":
        parts = ref_parts + [text_part]
    else:
        parts = [text_part] + ref_parts

    body = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "responseModalities": ["TEXT", "IMAGE"] if wants_text else ["IMAGE"],
        },
    }
    body["generationConfig"].update(_image_config(style, ratio, size))
    for key in ("tools", "safetySettings", "systemInstruction"):
        if payload.get(key) is not None:
            body[key] = payload[key]
    return body
```

---

## 附录 B：参考来源

| 内容 | 来源 | 检索日期 |
|---|---|---|
| 模型家族 / imageSize 支持矩阵 / 比例清单 / 大写 K 要求 | Firebase AI Logic「Generate & edit images using Gemini」（官方文档镜像） | 2026-09-11 |
| token 计费表（747/1120/1680/2000/2520）与 legacy `imageConfig` → `responseFormat.image` 迁移 | Google AI for Developers「Gemini Generate Content API」文档镜像 | 2026-09-11 |
| inline 上限 20MB→100MB、外部 URL / GCS 注册 | Google 官方博客「Increased file size limits and expanded inputs support」+ Firebase「Supported input files」 | 2026-09-11 |
| `finishReason` 全枚举（含 `NO_IMAGE`）、`promptFeedback.blockReason` 语义 | Google 官方 generateContent API 参考 + 第三方整理 | 2026-09-11 |
| 模型 ID 与废弃时间（`gemini-3-pro-image-preview`） | 第三方网关文档（可信度中，需实测） | 2026-09-11 |
| **输入侧 URL 直通**：`file_data.file_uri` 收公开 HTTPS / presigned（兼容 S3 presigned）URL、上限 100MB、`mime_type` 必需、每请求 ≤10 张图、上游对 URL 做安全审核并回 `URL_RETRIEVAL_STATUS_UNSAFE`、VPC-SC 下不可用；inline 图片上限写作 **7MB** | Google AI for Developers「File input methods」+ Vertex AI「Generate content with the Gemini API」参考 | 2026-09-11 |
| **真实生产请求样例**（`fileData.fileUri` 指第三方 CDN、`imageConfig` 形态、`responseModalities:["IMAGE"]`、text 在前图在后、2 张参考图、9:16 + 2K） | 用户提供的线上请求（hailuoai CDN） | 2026-09-11 |

> 沙箱到 `*.googleapis.com` / `ai.google.dev` 全站 502（代理），以上全部为检索所得，
> 未经本机直连原文校验 —— 因此第 9 节的实测清单是落库的前置条件。

---

## 附录 C：用线上样例反推的往返校验（2026-09-11 实跑）

把附录 B 那份生产样例**反推成 OpenAI 侧请求**，喂给附录 A 的草案，验证产出与线上形态一致。
不依赖网络，也不依赖真实 key —— 它证明的是**形状**（映射逻辑），不是上游可用性（那要靠第 9 节）。

反推的入参：

```python
payload = {
    "model": "nano-banana-pro",                  # 别名 -> gemini-3-pro-image
    "prompt": "复古胶片记忆墙风格，竖版画面，9:16比例",
    "image": ["https://cdn.hailuoai.video/…1778…jpeg",   # 客户端直给的两个 CDN 图
              "https://cdn.hailuoai.video/…3051…jpeg"],
    "size": "1080x1920",                         # OpenAI 像素 -> 9:16 + 2K
    "response_format": "b64_json",
}
```

实跑输出（`python3 /tmp/roundtrip_doc06.py`，脚本从本文档抽附录 A 源码 + 假 ctx）：

| 断言 | 结果 |
|---|---|
| 产出 body `==` 生产样例形态（`contents[].role`、text 在 `parts[0]`、两个 `file_data` 直通、`imageConfig`、`responseModalities:["IMAGE"]`） | ✅ True（逐字段相等） |
| URL 引用**零下载**（假 ctx 的 `image_bytes` 一旦被调用即断言失败） | ✅ `network calls: []` |
| 模型改写进 URL 路径 | ✅ `…/models/gemini-3-pro-image:generateContent` |
| `mime_type` 从 `.jpeg` 推断 | ✅ `image/jpeg` |
| `1080x1920` → 比例与档位 | ✅ `9:16` + `2K`（与线上样例完全一致） |
| `1024x1024` | ✅ `1:1` + `1K` |
| `2.5-flash-image` 请求 `3840x2160` | ✅ `16:9`，且**不发 `imageSize`**（该模型固定 1K） |
| `n=2` | ✅ `ctx.fail` → 400 `unsupported_parameter`（不串行重发） |

复现方式（脚本骨架；这段是**本地校验工具**，不经过 channel 沙箱，所以用 `exec` 把附录 A
加载成模块是正当的）：

```python
import asyncio, importlib.util, pathlib, re

md = pathlib.Path("docs/06_Google_NanoBanana_Integration.md").read_text(encoding="utf-8")
blocks = re.findall(r"^```python\n(.*?)^```", md, flags=re.M | re.S)
src = max((b for b in blocks if "async def transform" in b), key=len)   # 附录 A
draft = importlib.util.module_from_spec(
    importlib.util.spec_from_loader("draft", loader=None))
exec(compile(src, "draft.py", "exec"), draft.__dict__)


class FakeCtx:
    """Audits the network: any download attempt fails the check."""

    def __init__(self, **options):
        self.options = options
        self.request_id = "req-roundtrip"
        self.upstream_url = ("https://generativelanguage.googleapis.com/v1beta/"
                             "models/gemini-2.5-flash-image:generateContent")
        self.emitted_url, self.network = None, []

    def is_url(self, ref):
        return isinstance(ref, str) and ref.startswith(("http://", "https://"))

    def emit(self, *, url=None, **kw):
        if url is not None:
            self.emitted_url = url

    def fail(self, message, **kw):
        raise AssertionError(f"ctx.fail: {message} {kw}")

    async def image_bytes(self, ref):
        self.network.append(("image_bytes", ref))
        raise AssertionError("draft downloaded an image it should have forwarded")


asyncio.run(draft.transform(FakeCtx(), payload, "request"))
```

> 注意：跑这份校验只用到草案的纯逻辑，**`ctx.fail` 与 `upload_temp_image` 仍是引擎侧待补能力**
> （5.5 节），所以它是设计验证、不是可上线测试。

### C.2 逐路径 CPU 基准（同日实跑）

脚本 `/tmp/bench_doc06.py`（同一套"抽附录 A + 假 ctx"手法；假 ctx 记账
`decode/encode/upload` 次数，`n=25` 取中位数）。测的是**我方 event-loop CPU，不含网络**。

| 场景 | 中位耗时 | 解码 | 编码 | 上传 | 出站形状 |
|---|---|---|---|---|---|
| URL 直通（2 张） | **7 µs** | 0 | 0 | 0 | `file_data` ×2 |
| data URI inline（200KB） | **349 µs** | 1 | **0** | 0 | `inline_data` |
| 裸 base64 inline（200KB） | 490 µs | 1 | 1 | 0 | `inline_data` |
| data URI re-host（5MB） | 9.6 ms | 1 | 0 | 1 | `file_data` |
| `image_ref_mode=inline` + 5MB | 15.1 ms | 1 | 1 | **0** | `inline_data`（C3 生效） |
| 3×3MB（累计 9MB > 6MB 护栏） | 18.9 ms | 3 | 0 | **2** | `inline_data`, `file_data`, `file_data`（C2 生效） |
| [对照] 旧写法：无条件 decode+encode 同一 ref | 492 µs | 1 | 1 | 0 | — |

四条结论：

1. **URL 直通比我方任何 b64 路径快 50~2000 倍**（7 µs vs 349 µs ~ 15 ms），且零编解码、零下载 ——
   这是 `auto` 默认直通的量化依据。
2. **data URI 快路径省掉一次重编码**：349 µs vs 对照 492 µs（**−29%**）；差值 143 µs 恰好等于
   一次 200KB 编码的成本（"裸 base64 490 µs − data URI 349 µs" 同值）。这条正命中
   `/v1/images/edits` —— route 生成的就是 data URI。
3. **成本随体积线性**：≈ **1.9 ms/MB 解码 + 1.1 ms/MB 编码**（合成数据，只作量级参考）。
   换算：一张 5MB 图 inline ≈ 15 ms **纯 CPU 且压在 event loop 上**（`binascii` 不释放 GIL）——
   与"b64 的账"一致，也说明阈值不能放松。
4. C2/C3 行为符合设计：累计超护栏时**逐张** re-host 且允许混用形状；`inline` 模式即使 5MB 也不上传。

> 这组数字是我方侧的成本上限（假 ctx 无网络、无 MinIO 往返），真实链路还要叠加上传与上游耗时；
> 它保证的是"我们这层不做多余的事"，不是端到端延迟。

### C.3 实现期发现（写进代码才暴露的两件事）

1. **输出侧的存储守卫必须与输入侧同等严格**。§5.2 写明"无 MinIO 时不要静默降级"，但脚本第一版
   只给输入侧的 re-host 加了守卫，`response_format=url` 分支仍把 `upload_temp_image` 的降级结果
   （`data:image/png;base64,…`）直接写进 `data[].url` —— 一个几 MB 的字符串冒充图片链接。
   两条集成测试当场抓出来。
   **当时的处置是输出侧报 502 `storage_unavailable`、输入侧报 413 `image_too_large`。**
   **2026-09-11 修正**：输出侧的 502 属于把「我们的配置缺口」变成「调用方的失败」，与
   `docs/02` BR-008「MinIO 未配置时…不报错」相抵触，已改为**退回上游自身的形态**（Gemini 只有
   base64，故出 `b64_json`），并与 `openai/images@v1` 统一。**仍然坚持的底线：绝不把 data URI
   冒充 `url`。** 输入侧不变——那里是「请求根本组不出来」，没有可退回的形态，413 才是诚实答案。
   **教训：文档里写下的规则，要在每个分支都落实，而不是只在想起来的那个分支；但"落实情况"也要
   回头核对是否与业务规则一致 —— 一次"加严"可能只是把问题从静默换成了过度失败。**
2. **改脚本必须同步 `manifest.json` 的摘要**。digest 是脚本文本 sha256，本轮因为改 f-string、
   补类型注解各重算过一次。`pin_digests` 默认关，忘了不会拦请求，但**声明错了就是错的** ——
   改完脚本重算一次，别靠记忆（本轮已核算并写入 `963d3cd4…`）。
3. **测试断言里的"数量"是有效的性能门禁**：`assert _Vendor.gets == 0` 这类计数断言，比"看日志没有下载"
   可靠，也正是它证明了 URL 引用确实没被下载。性能判据同理（见 §10）。
4. **补齐纯函数单测的当下就抓到一个真 bug**：`_tier()` 在"请求档位低于模型最低档"时
   （例如给 3-pro 传 `512`），`allowed` 列表为空，而兜底取的是 `caps["sizes"]` 的**最大值** → **发出 4K**：
   尺寸不符，tokens 还翻倍（1120 → 2000）。正确语义是回落到该模型的**最低档**。修法 + 回归测试
   （`test_tier_clamps_to_the_model_ceiling`）已入库。
   **教训：端到端测试只覆盖了正常路径；边界（低于最低档、高于最高档、无档位模型）只有纯函数直测
   才便宜到能全测 —— 这一条也是"阶段 3 不该省"的最好证据。**

---

## 附录 D：真实环境实测记录（2026-09-11，网关 `api.chatfire.cn`）

环境：`POST https://api.chatfire.cn/v1beta/models/gemini-3.1-flash-image-preview:generateContent`，
鉴权 `Authorization: Bearer sk-…`（该网关接受 Bearer 形式的裸 key）。
手段：探针 `tools/probe_gemini_channel.sh`（11 用例，直连上游）+ `tools/e2e_google_channel.sh`
（5 用例，经适配器与 `X-Script-Ref: google/images@v1`）+ 第二轮 `probe_round2.py`（8 用例）。

> **网关画像**：错误文案（"所有令牌分组 default 下对于模型 X 均无可用渠道，请更换分组尝试"、
> "No available channel for model … under group …"）表明它是 **new-api（QuantumNous/new-api）系**聚合平台。
> 这解释了 `fileData` 的严格解析（protobuf 直通）与 503 `model_not_found` 的形状 —— 也意味着
> **模型名是渠道映射名**，"没有该模型"往往只是分组里没配渠道，不代表上游模型不存在。

### D.1 请求侧

| 项 | 实测结果 |
|---|---|
| `generationConfig.imageConfig`（形态 A） | **200** —— 1K / 2K / 4K 三档全部可用（4K 出图 b64 10.7 MB） |
| `generationConfig.responseFormat.image`（形态 B） | **200** —— **两代形态都被接受**，`image_config_style` 的两难在本网关不存在 |
| `responseModalities: ["IMAGE"]` / `["TEXT","IMAGE"]` | 均 **200** |
| 纯文本 ping `["TEXT"]` | 200，回 "pong"（模型可用性探针） |
| **`file_data`（snake_case）** | **失败**：`contents[0].parts[1].data: required oneof field 'data' must have one initialized field`（该键被当未知字段丢弃 → part 的 data oneof 为空） |
| **`fileData`（camelCase）** | **200**，参考图生效（`promptTokenCount` 11 → 271） |
| `inline_data`（snake_case） | **200**，**8.27 MB（原图 6.35 MB）也照收** |
| 双参考图（snake） | 与单图同样的失败；camel 未复测 |

**结论：本网关只认 camelCase 的 `fileData`，而 `inline_data` 两种拼写都行。** 脚本 `_uri_part()` 发的是
`file_data` → **必须改**。已用 `SCRIPT_OVERLAY_DIRS` 挂一个 camel 变体验证：`i2i_url` 由 **502 → 200**，
其余用例行为不变（未改仓库脚本，等确认）。

### D.2 响应侧

| 项 | 实测结果 |
|---|---|
| 输出 mime | **`image/jpeg`**（不是先前假设的固定 PNG）→ `response_format=url` 的扩展名必须用返回的 mime |
| 图片来源 | `candidates[].content.parts[].inlineData`（**camelCase**）—— 脚本已双读，无需改 |
| `usageMetadata` | `promptTokenCount` / `candidatesTokenCount` / `totalTokenCount` / `candidatesTokensDetails[{modality:IMAGE,tokenCount:1120}]`，另有网关私有字段 **`rawPromptTokenCount`**；**没有 `thoughtsTokenCount`** |
| usage 映射实测（图生图） | `input_tokens=271 output_tokens=1485 image_tokens=1120 text_tokens=365` —— 含图 token 的输入计费对得上 |

### D.3 端到端（经适配器）

| 用例 | 结果 |
|---|---|
| 文生图（脚本原样） | **200**，`data[0].b64_json` 591 KB，usage 正常 |
| **图生图 · URL（脚本原样）** | **502** `upstream_http_error`（上游 500，根因即 D.1 的 snake 问题） |
| 图生图 · URL（overlay/camel） | **200**，payload 1.2 MB |
| 图生图 · b64 小图（124 KB，inline） | **200** |
| 图生图 · b64 大图（8.27 MB，`auto`） | **413 `image_too_large`** —— 超 4 MB 阈值 → 需 re-host → 无 MinIO。**设计行为正确** |
| 图生图 · b64 大图（8.27 MB，强制 `inline`） | **200** —— 本网关 inline 上限 **≥ 8.27 MB**，比文档里最保守的 7 MB 口径宽 |

### D.4 由实测修正的文档结论

1. P0-1 的两难在本网关**不存在**：`imageConfig` 与 `responseFormat.image` 都接受。
2. ✅ **`file_data` 已改为 camelCase**（脚本 3 处 dict 字面量 + 回归测试
   `test_no_part_uses_the_snake_case_spelling`，钉住"出站不得出现 snake 拼写"）。
   修复后用**真实上游复验**：`i2i_url` 由 **502 → 200**（payload 783 KB），`t2i` / `i2i_b64_small` /
   `i2i_b64_big_inline` 全部 200，`i2i_b64_big_auto` 仍 413 —— 只有失效的那条路径变了行为。
3. 输出不是固定 PNG，而是 **JPEG**；`url` 形态的扩展名要走返回的 mime（脚本已如此）。
4. inline 上限得到**下界**：≥ 8.27 MB（原图 6.35 MB）。4 MB 阈值对本网关偏保守 —— 但阈值该按最弱上游定，
   在有更紧的上游之前不要放宽。
5. 本网关不返回 `thoughtsTokenCount`，`_usage()` 的兜底（`candidates` 即 output）是有效路径。
6. 探针脚本首版的两个 bug 已修：bash 3.2 下空数组 `"${extra[@]}"` 会报 unbound（用 `${extra[@]+…}`）；
   8 MB 的 inline body 不能走 `-d "$body"`（超 ARG_MAX）→ 改 `--data-binary @file`。
7. **工具已落库**（都不含凭据，从 `GEMINI_KEY` 环境变量读）：
   - `tools/probe_gemini_channel.sh` —— 直连上游的 11 用例探针（两代字段形态、1K/2K/4K、
     `["IMAGE"]` vs `["TEXT","IMAGE"]`、`fileData` snake/camel、双参考图、大 inline b64、纯文本 ping）
   - `tools/e2e_google_channel.sh` —— 经适配器的 5 用例端到端，支持 `OVERLAY=<dir>` 挂 overlay 目录
     验证脚本改动，并强制空 `REDIS_URL`/`MINIO_*`，**不会写到生产 Redis 或桶**
   换上游或新增渠道时可直接复用这两支脚本，不必重新摸索。

### D.5 第二轮探针（补齐 P0-5 / P0-9 / P0-10 与边界档位）

| 用例 | 结果 | 结论 |
|---|---|---|
| `512` 档（3.1 flash） | **200** | 512 可用 → 脚本 `_tier()` 对 3.1 系列放行 512 是对的 |
| 极端比例 `1:8` | **200** | 极端比例可用 → `wide=True` 的判定正确 |
| **mime 不符**（`.jpg` 的 URL 标成 `image/png`） | **200** | **P0-9：上游容忍 mime/内容不一致** → 4.4 靠扩展名推断 mime 是安全的，推错也不会 400 |
| **`fileUri` 内网地址**（`http://127.0.0.1:1/x.jpg`） | **500**，`error.code=400`，`Cannot fetch content from the provided URL.` | **P0-10：URL 拉取失败形状** = HTTP 500 + 400 错误码 + 该文案 |
| **`fileUri` 404**（不存在的对象） | 同上 | 同一形状，与"私网/不可达"不区分 |
| `gemini-2.5-flash-image`（有无 `imageSize` 各一次） | **503** `model_not_found`（"No available channel for model …"） | **P0-5 仍无法测**：该网关分组里没有这个模型，不是脚本问题 |
| 未知模型 `gemini-9-fake-image` | **503** `model_not_found`（中文文案） | 模型名由渠道映射决定；换成真实渠道映射名即可用 |

**P0-10 的处置**：上游用 500 表达"拉不到 URL"，引擎的 `raise_for_status` 会把 ≥500 映射成
`UpstreamError(status=502, code="upstream_http_error")`，客户端拿到 502 + 上游原文。
语义上这更像客户端的错（URL 不可达），但要改就得动引擎（HTTP 非 2xx 时脚本的 response 相位不会执行）。
**本轮不改**，记为改进项：可选方案是在 `transport.raise_for_status` 里识别该文案 → 400 `url_retrieval_failed`。

**仍未测**：只有 P0-5（该网关没有 `gemini-2.5-flash-image` 渠道）。

### D.6 第三 / 四轮：安全拦截与 inline 上限（同一网关）

| 用例 | 结果 | 结论 |
|---|---|---|
| 无害 prompt（战争题材海报）+ **全部 harm 类别设为 `BLOCK_LOW_AND_ABOVE`** | **200，正常出图** | **`safetySettings` 在该网关不生效**（与不带该字段的那次结果完全一致）→ 不能靠调阈值做灰度；**P0-6 无法用配置触发** |
| 同一 prompt 不带 `safetySettings` | **200，正常出图** | 对照组，证明上一条的差异为 0 |
| inline 10.0 MB b64（噪声图，直连上游） | **200** | 上限下界提升到 ≥ 10 MB |
| inline **15.3 MB** b64 | **200 但 `imgs=0` + `finishReason=IMAGE_RECITATION`** | 体积没被拒；模型**拒绝**为噪声图出图 —— 这正是"HTTP 200 却没有图" |
| inline **21.6 MB** b64 | **200，`imgs=1`** | **上限 ≥ 21.6 MB**，本网关不卡内联体积（4 MB 阈值纯属保守，保留） |
| 同 15 MB 噪声图经**适配器**重试 4 次 | 4/4 **200 且正常出图** | 说明上一行的"无图"是**概率性**的 → 该失败路径无法稳定复现（见下） |

**P0-6 的结论，以及它的证据边界**：拦截类响应走 **`candidates[0].finishReason`**
（`IMAGE_SAFETY` / `IMAGE_PROHIBITED_CONTENT` / `IMAGE_RECITATION` …），而不是 `promptFeedback.blockReason`
—— 后者只在"prompt 直接被拦、压根没有 candidates"时才出现；脚本两条分支都留着。
`FILTERED` 已覆盖这些值，所以"200 无图"会转成 **400 `content_filter`**，而不是 500 或空 `data`。

**但要如实标注证据等级**：该形态在真实上游**只被观察到一次**（本轮 15.3 MB 噪声图 → `IMAGE_RECITATION`
且无图），随后用**同一输入经适配器重试 4 次，全部正常出图** —— 模型"拒绝出图"是**概率性**的，无法稳定复现。
因此这条映射的保障仍主要来自**假 vendor 的 4 个用例**（`IMAGE_SAFETY` / `promptFeedback.blockReason` /
纯文本拒绝 / `NO_IMAGE`）；真实上游的那一次观察只用于证明"这种响应形状确实存在"。

> **含义**：图片类上游的失败形态**不能指望端到端复现**（同一输入的结果是随机的）。正确做法是用假上游钉死
> 每一个分支、用单测钉死边界 —— 这与 C.3 第 4 条（`_tier()` 的 4K 误判只有直测才抓得到）是同一个道理。
