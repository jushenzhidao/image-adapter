# 火山方舟 (Volcengine ARK) 接入指南

## 概述
火山方舟 doubao-seedream-5-0-260128 模型已成功接入 OpenAI Images 适配器。

## 配置步骤

### 1. 环境变量
在 `.env` 文件中添加：
```bash
VOLCENGINE_ARK_API_KEY=ark-your-api-key-here
```

### 2. 渠道配置（控制面 / New API 侧）

适配器**不持有上游注册表**，不存在 `config/upstreams.yaml` 这类路由文件。
上游由调用方随请求以 header 指定，脚本按 ref 从 `script_store/` 加载：

| Header | 值 |
|---|---|
| `X-Upstream-Url` | `https://ark.cn-beijing.volces.com/api/v3/images/generations` |
| `X-Script-Ref` | `volcengine_ark/images@stable`（**推荐写法**；`@v1` 与 `@latest` 指向同一版） |
| `Authorization` | `Bearer <VOLCENGINE_ARK_API_KEY>` |
| `X-Channel-Options` | `{"model": "doubao-seedream-5-0-260128"}`（可选，见下） |

可选的 `X-Channel-Options` 键（都是按渠道生效，改数据不发版）：

| 键 | 作用 |
|---|---|
| `model` | 接入点 ID（渠道身份；body 里的 `model` 不读） |
| `image_ref_mode` | `url`（默认）/ `data_uri`（别名 `inline`）/ `base64`，见 §4.1 |
| `ref_max_edge` / `ref_max_bytes` / `ref_fmt` / `ref_quality` | 参考图压缩策略，见 §4.2 |
| `watermark` | 透传开关，**默认 `false`**（优先级：请求体 > 渠道选项 > 默认） |
| `sequential_image_generation` | 透传开关，仅在显式提供时才发送（5-0-pro 系列会 400） |

适配脚本在 `script_store/volcengine_ark/images@v1.py`，**每个渠道只有一个版本**。

🔴 **2026-09-13 起版本切分被取消**：此前 `@v2`–`@v8` 存在（`@v5` 曾经是对着 `@v2` 写的，
所以 `@v3` 的 `ref_*` 与 `@v4` 的默认翻转都没有并入 `@v5`–`@v7` 那条线），现在全部删除，
最优行为合并进 `@v1`。旧版本内容保留在 git 历史中
（提交 `chore(script_store): 归档 ark v3–v8 后再合并为单一 v1`），需要复核时从那里取。

**旧 ref 怎么办**：`@v2` 及以上已删除、manifest 也没为它们留别名，但链条会**降级到 `@stable`**
（打一条 warning），所以渠道头指旧版本**不会让请求失败**，只是拿到的不是它原先钉的那一版。
渠道头**推荐写 `@stable`**（用户口径：以 `@stable` 为准）—— 它既是别名、又恰好是降级目标；
只有需要钉死某一版时才写 `@v1`。降级本身是兜底，不该当常态用。
⚠️ 前提是 manifest 每个条目都定义了 `stable`（启动时缺它会 warning）。降级会开一个
`script_ref_fallback` span（`requested` → `serving`），可在 Logfire 里查。

`X-Script-Ref` 由 `adapter/script_source.py::_resolve_ref_path()` 解析到该文件，
根目录由 `SCRIPT_REF_DIR` 控制（默认 `<项目根>/script_store`）。

> 图生图复用同一端点与同一脚本：请求体带 `image` 时 Seedream 即执行编辑，
> 无需单独路由。

## API 调用示例

### 通过适配器调用（OpenAI 格式）
```bash
curl -X POST http://localhost:8080/v1/images/generations \
-H "Content-Type: application/json" \
-d '{
  "model": "doubao-seedream-5-0-260128",
  "prompt": "星际穿越，黑洞，黑洞里冲出一辆快支离破碎的复古列车",
  "n": 1,
  "response_format": "url",
  "size": "1024x1024"
}'
```

### 直接调用火山方舟 API
```bash
curl -X POST https://ark.cn-beijing.volces.com/api/v3/images/generations \
-H "Content-Type: application/json" \
-H "Authorization: Bearer ark-your-api-key" \
-d '{
  "model": "doubao-seedream-5-0-260128",
  "prompt": "一只可爱的白色小猫",
  "size": "2k",
  "response_format": "url",
  "sequential_image_generation": "disabled",
  "watermark": true,
  "stream": false
}'
```

## 响应示例

```json
{
  "model": "doubao-seedream-5-0-260128",
  "created": 1788467397,
  "data": [
    {
      "url": "https://ark-acg-cn-beijing.tos-cn-beijing.volces.com/...",
      "size": "2048x2048"
    }
  ],
  "usage": {
    "generated_images": 1,
    "output_tokens": 16384,
    "total_tokens": 16384
  }
}
```

## 重要注意事项

### 1. 尺寸要求
- **最小像素数**：3,686,400（约等于 2048x2048）
- OpenAI 的 `1024x1024` 会自动升级为 `2k`
- 支持的格式：
  - 预设尺寸：`2k`、`3k`、`4k`（小写）
  - 自定义尺寸：`WIDTHxHEIGHT` 格式（需满足最小像素要求）

### 2. 参数映射

| OpenAI 参数 | 火山方舟参数 | 说明 |
|-------------|--------------|------|
| `size: "1024x1024"` | `size: "2k"` | 自动升级到最小尺寸 |
| `size: "2048x2048"` | `size: "2k"` | 直接映射 |
| `size: "3840x2160"` | `size: "4k"` | 高清尺寸 |
| `response_format: "url"` | `response_format: "url"` | 直接透传 |
| `response_format: "b64_json"` | `response_format: "b64_json"` | 直接透传 |

> **这里刻意不做输出形态归一化**（2026-09-11 确认）。ARK 上游**本身就同时支持** `url` 与
> `b64_json` 两种 `response_format`，脚本按 90 行显式转发后，上游会给回请求的那种形态，
> 所以响应相位原样返回即可 —— 符合「上游场景优先于我方的转换」这条设计优先级。
> 只有对 `response_format` 撒谎的上游才需要归一化：`openai/images@v1` 面向的兼容网关实测会
> 拿 `data:` URI 冒充 `url`，`google/images@v1` 只出 base64，那两个脚本才做转换（见
> `docs/06` §5.2 与 README「OpenAI 原生上游」）。**别为求统一而把转换搬到 ARK 上**。

### 3. 固定参数
适配器自动添加以下参数：
- `watermark: false` - **默认关闭水印**。ARK 自身默认是 `true`，所以「关」必须**显式发值**、不能靠省略字段；请求体或渠道选项显式传 `true` 仍可开启（优先级：请求体 > 渠道选项 > 默认）
- `stream: false` - 禁用流式响应
- `sequential_image_generation` - 仅当调用方/渠道选项显式提供时才发送（5-0-pro 系列模型不支持该参数，硬编码会导致 400，2026-09-09 实测）

### 4. 参考图的两个决定（`image_ref_mode` 与 `ref_*`）

参考图是本渠道唯一「会在上游侧失败」的输入：ARK **自己**去拉我们给的 URL，而那次下载在
**方舟服务端有固定 5s 硬超时、任何请求参数都改不了**（FAQ `docs/6390/1359411`、接入指南
`docs/82379/2666490`）；同一份接入指南还写着「建议压缩至 100kB 以下」。而一次请求只发一次上游调用、
非 2xx 在响应相位之前就抛出 ⇒ **失败后无法重试**，形态与体积都必须在发起前定好。这就是下面两组选项的全部理由。

#### 4.1 线上形态 `image_ref_mode`

| 值 | 行为 | 何时选 |
|---|---|---|
| `url`（默认） | 客户端 URL **原样转发**（我方零下载）；inline 参考图由我方上传到对象存储换成链接 | 源站快、ARK 能在 5s 内拉到 |
| `data_uri`（别名 `inline`） | 任何形态都转 data URI；客户端 URL **由我方下载**后内联，ARK 完全不需要出网 | 慢源 / 海外源 |
| `base64` | 裸 base64（无 `data:` 前缀） | 仅当实测过该模型接受它 —— 260128 实测 400 `invalid url specified` |

#### 4.2 压缩策略 `ref_*`

| 键 | 类型 | 作用 |
|---|---|---|
| `ref_max_edge` | 正整数（px） | 长边**硬上限**：超过则等比缩小，永不放大 |
| `ref_max_bytes` | 正整数（字节） | **目标是目标，不是保证**：先降 quality（下限 40），再降尺寸，且只在缩小确实达到目标时才动尺寸；达不到就返回「不缩尺寸」的最小结果 |
| `ref_fmt` | `jpeg` / `jpg` / `webp` / `png` / `gif` | 目标格式；缺省＝保持原格式。显式指定时**即使变大也照办**（模型只认某种格式时这就是需求）；只有「格式没变、也没变小」才退回原图 |
| `ref_quality` | 1..100 | JPEG/WEBP 的质量；**必须同时给 `ref_fmt`**，否则报 `channel_config_error` —— 不静默失效 |

四条规则：

1. **只作用于「我方自己物化字节」的参考图**。`url` 模式下的 http(s) URL 永不下载、也永不压缩
   （那是 5s 赌局的豁免）；inline（`url` 模式）以及 `data_uri` / `base64` 模式下的参考图全部适用。
   *没有哪一种输入形态会被静默跳过* —— 这一条是硬纪律，改这里之前先读 `docs/07` §14.2.1。
2. **解不开的图原样送上游**。`ctx.image` 的格式白名单只有 PNG/JPEG/WEBP/GIF，而门面接受更多，
   所以 BMP/AVIF 参考图是正常请求、只是压不了：退回原字节，不把优化变成 400。
   但**下载失败不在此列** —— 那是对该 URL 的事实，重试只会把同一个超时花两遍。
3. **非法选项值在发任何上游请求之前**报 `channel_config_error`（400）。错误码指向
   `X-Channel-Options`，而不是客户端的图 —— 客户端修不了运维的错别字。
4. **观测**：`storage_put` span 的 `ext` / `bytes` 就是实际上传的东西。`ext` 变成 `jpeg`/`webp`
   即压缩生效；`ref_max_bytes` 没达成时那里也会显示真实字节数。

配置示例（长边 2048 + 100kB 目标，两者都不改代码）：

```
X-Channel-Options: {"model": "doubao-seedream-5-0-260128", "ref_max_edge": 2048, "ref_max_bytes": 102400}
```

> 选项与脚本是两件事：只配了 `ref_*` 而渠道 ref 不指向 v1，表现就是「配了没用」。
> 今天每渠道只有一版，渠道头按 `@stable` 写即可（`@v1` / `@latest` 指向同一版）。

#### 4.3 并发物化

`image` 数组里的 N 张参考图经 `ctx.fanout` 并发物化，N 张只等一拍。这与
`openai/images@v1`、`google/images@v1` 用的是同一个原语（`docs/07` §14.2.2）。

- **并发度 = `min(fanout_concurrency, N)`**（现配置 5）。N=1 仍走串行路径；纯文生图请求根本
  不进扇出。
- **请求体不受影响**：并发改变的只是**在调用方图源上的瞬时并发**（从 1 变成最多 5），
  上游收到的 body 与串行时相同。
- **失败语义不变**：仍是「任一项失败即整体失败」，且重抛**输入顺序里最早那个失败**并保留其类型
  ⇒ 脚本 `ctx.fail()` 的 400 仍是 400（不会变成 `ExceptionGroup` 的 500）。
- **为什么这里曾经是串行**：早先的理由是「压缩是 CPU，GIL 不释放，并发加不了吞吐」。
  这条只对**重编码**成立；一张参考图的耗时大头是它周围的**等待**（下载 URL、上传 re-host），
  而扇出隐藏的正是等待。每个 item 内部的重编码仍是一个一个跑。
- **代价**：调用方源站瞬时压力 ×cap（可能触发对方限流，且我方观测不到）；最坏内存
  `cap × max_asset_bytes`。⚠️ 前提是**延迟受限**——带宽受限时抬并发零收益、还更易撞
  `IMAGE_DOWNLOAD_TIMEOUT`，此时该压单张体积（`ref_*`）而不是抬并发。


## 测试验证

### 真实 API 测试
已通过直接调用验证：
```bash
✅ 状态码: 200
✅ 响应格式: OpenAI 兼容
✅ 图片 URL: 有效（86400秒有效期）
✅ usage 字段: 完整
```

### 集成测试
脚本侧的异步轮询与参数映射由集成测试覆盖：
```bash
LOGFIRE_TOKEN="" .venv/bin/python -m pytest tests/integration -q
```

> 注：早期文档中的 `test_volcengine_integration.py` 已不存在，勿再引用。

## 已知限制

1. **最小尺寸限制**：不支持低于 2k 的尺寸
2. **批量生成**：暂不支持 `n > 1`（需在适配器层聚合多次调用）
3. **图片有效期**：生成的 URL 有效期 86400 秒（24小时）

### 4. Seedream 5.0 pro 专属（2026-09-09 实测）

- **图层拆分**：请求带 `"layer_decomposition": true` + 恰好 1 张输入图（prompt 可留空自动识别
  主体），返回 底图(1 张 jpeg) + 最多 16 个透明 PNG 图层；`data[]` 每项额外携带
  `z_index` / `name` / `description` / `bounding_box`（absolute 像素 + 0-1000 归一化坐标）。
  脚本已通过 PASSTHROUGH 透传该参数，多图响应与元数据原样返回客户端。
- **pro 不支持** `sequential_image_generation`（组图为 5.0-lite / 4.5 / 4.0 能力），
  详见 script_store/volcengine_ark/images@v1.py 头部约束清单。
- **`size` 支持 `1k` 档位**（直通不升级），计费约为 2K 的 1/4（实测 1K ≈ 4k tokens/张，
  2K ≈ 16k tokens/张）；图层拆分按返回张数计费（11 张 ≈ 42k tokens）。
- **usage 透传**：脚本 response 相位转发 ark 的 `usage`（`generated_images` 对图层拆分
  即返回张数，控制面计费依赖此字段）。

## 参考文档
- 官方文档: https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1541523
- 模型名称: `doubao-seedream-5-0-260128`
- API 端点: `https://ark.cn-beijing.volces.com/api/v3/images/generations`
