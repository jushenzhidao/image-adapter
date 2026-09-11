# 08 · MPS 图片高级超分适配方案（Advanced Super Resolution）

> **上游文档**：企业微信文档《MPS图片高级超分》（`doc.weixin.qq.com/doc/w3_ATEArQamACgCNv5k31W88SqybWTNH`），
> 2026-09-11 通过接管本地浏览器（CDP）逐屏截图读取；截图存于 `/tmp/mps_*.png`。
> 该文档是 canvas 渲染，DOM 里只有目录，所以以下内容来自**视觉识别**——个别措辞可能有误差，
> 凡有歧义处都标了「待实测」，不当作既成事实。
>
> **状态**：方案，**未实现**（脚本、能力表、渠道配置都还没写）。

---

## 1. 上游契约（文档原文）

| 项 | 值 |
|---|---|
| 接口 | **`ProcessImage`**（MPS，version `2019-06-12`，`mps.tencentcloudapi.com`） |
| SDK 要求 | version **≥ 3.0.1545** |
| 语义 | **异步任务**：发起返回 `TaskId`，用另一个接口查询结果 |
| 查询接口 | 文档写 **`DescribeImageTaskDetail`**（注意：腾讯云公开文档里是 `DescribeTaskDetail`，**待实测**） |
| 鉴权 | TC3 签名（`X-TC-Action` / `X-TC-Version` / `X-TC-Timestamp` / `Authorization`） |
| 参考 | 腾讯云 MPS「发起图片处理」「查询图片处理任务详情」+ API Explorer 任务发起/查询 |

### 1.1 输入 / 输出（这块比我预想的好）

| 项 | 可选形态 | 说明 |
|---|---|---|
| 输入 | `InputInfo.Type = "COS"`（`CosInputInfo{Bucket,Region,Object}`） | 需要 COS 对象 |
| 输入 | **`InputInfo.Type = "URL"`（`UrlInputInfo.Url`）** | **免 COS**：直接给公网可读 URL（文档「兜底模式」示例） |
| 输出 | `OutputStorage.Type = "COS"`（自建桶 + `OutputDir`/`OutputPath`） | 需要自建 COS |
| 输出 | **`OutputStorage.Type = "TOS"`（MPS 图片处理托管存储）** | **免自建桶**：结果给 **`SignUrl`**，文档注明「图片 url 访问有效期，**1 小时**有效」 |

> 我在 `docs/07` §10.3 把「存储桥」和「取回」列为接这类能力的两个真难点 —— **上游把这两个都解决了**：
> URL 输入 + 托管输出 + 签名 URL。落地方案因此比我原先设想的简单得多。

### 1.2 超分配置

位置：`ImageTask.EnhanceConfig.AdvancedSuperResolutionConfig`

```jsonc
"EnhanceConfig": {
  "AdvancedSuperResolutionConfig": {
    "Switch": "ON",
    "Type": "ultra",          // 超分类型
    "Mode": "percent",        // percent | aspect | fixed
    "Percent": 1.5,           // 等比倍数
    "Width": 1080,            // 目标宽
    "Height": 1080,           // 目标高
    "LongSide": 1920,         // 目标长边
    "ShortSide": 1080         // 目标短边
  }
}
```

参数表（文档原文，含其自带的注意事项）：

| 参数 | 说明 | 取值 / 约束 |
|---|---|---|
| `Type` | 超分类型 | 推荐 `ultra`；另有 `super` / `superResolution`（文档三档描述文字有歧义，**待实测**） |
| `Mode` | 超分模式 | `percent`（**默认**）/ `aspect` / `fixed` |
| `Percent` | 等比放大倍数 | **1.0 – 10.0，默认 2.0**；文档注「`Mode≠percent` 时无效」 |
| `Width` | 目标宽度 | **1 – 4096**；文档注「`Mode≠fixed` 时无效」 |
| `Height` | 目标高度 | **1 – 4096**；文档注「`Mode≠fixed` 时无效」 |
| `LongSide` | 目标长边 | **1 – 4096**；`Mode=aspect` 且未设 Width/Height 时有效 |
| `ShortSide` | 目标短边 | **1 – 4096**；同上 |

> ⚠️ **文档自相矛盾（P0-1）**：参数表说 `Width` 仅在 `Mode=fixed` 有效，但「按照宽高超分」示例用的是
> **`Mode: "aspect"` + `Width: 1280`**，输出宽度确实是 1280。以**实测**为准，不以注释为准。

### 1.3 四种业务模式（文档示例）

| 模式 | 配置 | 效果 |
|---|---|---|
| 按照比例超分 | `Mode: percent, Percent: 1.5` | 等比放大 1.5 倍 |
| **保持分辨率画质增强** | **`Mode: percent, Percent: 1.0`** | **尺寸不变、只增强画质** —— 这是个很有用的模式，等于「画质修复」 |
| 按照宽高超分 | `Mode: aspect, Width: 1280` | 输出宽 1280（与参数表冲突，见上） |
| 按照长短边超分 | `Mode: aspect, LongSide: 1280` | 输出长边 1280 |

### 1.4 耗时（选型与超时预算的依据）

| 输入 | 目标 | 参考耗时 |
|---|---|---|
| 1280×720 | 2560×1440 | ~7s |
| 720×1280 | 1440×2560 | ~7s |
| 1920×1080 | 3840×2160 | ~14s |
| 1080×1920 | 2160×3840 | ~14s |
| 3840×2160 | 7680×4320 | ~50s |
| 2160×3840 | 4320×7680 | ~50s |

文档里那次托管模式任务的真实时间线：`CreateTime 08:49:05` → `FinishTime 08:49:22` = **17s**（与表里 1080p→4K 的 ~14s 吻合）。

### 1.5 查询结果结构（托管模式）

```jsonc
{
  "Response": {
    "CreateTime": "2026-07-07T08:49:05",
    "FinishTime": "2026-07-07T08:49:22",
    "ImageProcessTaskInfoSet": [
      {
        "Output": {
          "Path": "/share/248001680/2026/09/07/ccf74c7f-...-3-1c7b504c347a.png",
          "SignUrl": "https://xxx.cos.ap-shanghai.myqcloud.com/share/.../xxx.png"  // 1 小时有效
        },
        "Status": "SUCCESS"
      }
    ],
    "ImageTask": { "EncodeConfig": {...}, "EnhanceConfig": {...} },
    "RequestId": "5f39b5d4-...",
    "Status": "FINISH",
    "TaskType": "WorkFlowTask"
  }
}
```

观察到的状态取值：**`SUCCESS`**（托管模式，`ImageProcessTaskInfoSet[].Status`）与 **`FINISH`**（`Response.Status`）——
两者层级不同，别混用（**P0-5** 要把全集测出来）。

---

## 2. 映射到本项目的 OpenAI 契约

| 客户端（`/v1/images/generations`） | 上游 |
|---|---|
| `model: "image-upscale"` | 控制面按 model 路由到本渠道（适配器不持注册表，见 `docs/07` §13） |
| `image`（URL） | `InputInfo.Type = "URL"` + `UrlInputInfo.Url` —— **零下载、零 re-host**，与 Gemini 渠道的直通策略一致 |
| `image`（data URI / 裸 b64） | 需先落存储（`ctx.upload_temp_image()` → 给 `UrlInputInfo`）——**这是我们唯一的存储依赖** |
| `size` | 映射到 `Mode`：见下表 |
| 厂商扩展（原样透传） | `super_resolution: {Type, Mode, Percent, Width, Height, LongSide, ShortSide}` |
| `response_format: "url"` | 直接把 `SignUrl` 返回（**注意 1h 有效期**，见 P0-3） |
| `response_format: "b64_json"` | 下载 `SignUrl` → `ctx.image_b64` 语义返回 |
| `n` / `mask` / `quality` | 不支持 → **400 `unsupported_parameter`**（前置校验，别推给上游） |

`size` 的映射规则（**不猜，缺省即用上游默认**）：

| 客户端给的 | 上游 | 理由 |
|---|---|---|
| `"2048x2048"` 或 `"1920x1080"` | `Mode: fixed` + `Width`/`Height` | 明确像素 → 明确目标 |
| 扩展字段 `scale: 2` | `Mode: percent` + `Percent: 2.0` | 倍数语义与上游一致 |
| 扩展字段 `long_side: 1920` | `Mode: aspect` + `LongSide` | 同上 |
| **什么都不给** | `Mode: percent, Percent: 2.0`（上游默认） | **不要自己编一个倍数** |

---

## 3. 同步转异步（按 `docs/07` §14 执行）

上游是异步任务（7~50s），但**客户端仍然只发一次请求、只收一个结果**：

```
X-Async: poll=3,timeout=300
```

- `PHASES = ("request", "response", "poll_request", "poll_response")`，脚本用模块级 `PHASES` 声明（缺声明会 400）。
- `request` 相位：构造 `ProcessImage`，拿到 `TaskId`。
- `poll_request`/`poll_response`：轮询 `DescribeImageTaskDetail`，直到任务级 `Status` 终态。
- `poll=3s`（别更小：这些任务 7s 起步，密轮询没意义）+ `timeout=300s`（4K→8K 可能 50s+，还要留重试余量）。
- 状态映射：成功 → 继续收尾；失败 → `ctx.fail(..., status=502, code="upstream_error")`；
  **`poll_timeout`（上游一直没完成）与 `stage_timeout`（本级超预算）保持两个码**，不要合并。
- 前端可见延迟 = 排队 + 处理 + 轮询间隔 → **控制面要把这条渠道的 timeout 调到 ≥ 60s**，
  否则客户端会在适配器还在轮询时就断开（那时我们仍在计费）。

---

## 4. 落地三件套（`docs/07` §13.5 的清单）

1. **脚本** `script_store/tencent_mps/images@v1.py`
   - `request`：OpenAI body → `ProcessImage`（`InputInfo` 按引用形态分流：URL 直通 / b64 先 re-host）
   - `poll_*`：查任务
   - `response`：`SignUrl`（或下载后的 bytes）→ `{"created":…, "data":[…], "usage":…}`
   - 错误出口一律走 `ctx.fail`（TC3 错误码 → 可读 400/502）
2. **能力表** `script_store/capabilities/tencent_mps.json`
   ```jsonc
   "image-upscale": {
     "kind": "upscale",
     "requires": ["image"],
     "outputs": ["image"],
     "async": true,                    // "需要内部轮询"，不是"对外异步"（§14.4）
     "accepts": ["image", "size", "super_resolution", "response_format"],
     "ignores": ["prompt", "n", "quality", "mask"]
   }
   ```
3. **控制面渠道**：`X-Upstream-Url: https://mps.tencentcloudapi.com` + `X-Script-Ref` + TC3 凭据 +
   `X-Async: poll=3,timeout=300`
4. **上线前**：用 `tools/` 下的探针脚本对真实上游跑一轮（含"坏 URL"、"不存在的 TaskId"、`Status` 终态全集）

---

## 5. 待实测清单（P0）

| # | 待确认 | 怎么测 | 为什么重要 |
|---|---|---|---|
| P0-1 | `Mode=aspect` 时 `Width`/`Height` 到底有效吗（文档表与示例冲突） | 同一张图两种发法，比输出尺寸 | 决定 `size` 映射表怎么写 |
| P0-2 | `Type` 三档的确切名称与效果差异（`ultra` / `super` / `superResolution`） | 各跑一次，比耗时与观感 | 默认值选谁 |
| P0-3 | 托管 `SignUrl` 的有效期与**可否再分发**（文档写 1h） | 取回后等 1h 再 GET | 决定 `response_format=url` 能不能直接透传给客户端 |
| P0-4 | 查询接口的确切 Action 名（`DescribeImageTaskDetail` vs 公开文档的 `DescribeTaskDetail`） | 空跑 + 用真 TaskId 跑 | 写错就整条链路不通 |
| P0-5 | `Status` 取值全集与失败态形状（已见 `SUCCESS` / `FINISH`） | 构造失败任务 + 正常任务 | 轮询终止条件与错误映射 |
| P0-6 | `UrlInputInfo` 是否限制域名/需备案/可达性 | 用我方 MinIO presigned URL 试 | 决定 b64 输入能否走"先 re-host" |
| P0-7 | 输入尺寸下限（文档提到"小于 1080P"时 `ultra` 的行为） | 小图跑一次 | 决定是否要前置拒绝过小的图 |
| P0-8 | URL 输入与 COS 输入是否**都**支持 `TOS` 托管输出 | 交叉组合 | 最省事的组合是 URL 进 + TOS 出 |

---

## 6. 与既有设计的一致性检查

| 设计约束 | 本方案 |
|---|---|
| `docs/07` §14「同步优先」 | ✅ 异步上游在适配器内轮询转同步，客户端无感 |
| `docs/07` §13「能力即独立模型」 | ✅ 走 `/v1/images/generations`，`model: image-upscale`，不加新端点 |
| `docs/07` §11「能力表独立」 | ✅ `kind/async/requires/accepts/ignores` 字段直接可用，无需改 schema |
| `docs/07` §10.2「远端能力不进 ctx」 | ✅ 独立渠道 + 脚本，ctx 只提供形态三态与存储 |
| `docs/07` §9「上线前探针实测」 | ✅ §5 的 P0 清单就是探针要跑的用例 |
| 合规（`docs/07` §10.4） | ⚠️ 超分是**只放大/增强、不擦除、不改语义**，是能力层里风险最低的一类 —— **可以不默认关闭**（与擦除/去水印区分对待），但仍建议记录调用审计 |

> **一句话结论**：这条链路里最贵的部分不是适配（脚本工作量与 Gemini 渠道相当），而是**轮询带来的长占用**——
> 一个 4K→8K 任务要占住一个 worker 连接约 50s。所以渠道并发上限要按"单请求占用时长"折算，
> 而不是照抄其它渠道的数字。
