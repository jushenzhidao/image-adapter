# 腾讯混元 Image 3.5 (TokenHub) 接入指南

## 概述

`script_store/tencent_hunyuan/images@v1` 把 TokenHub 的混元生图 v3.5 接入适配器。
上游是**chat 形状、同步返回**的接口：一次 POST 直接回终态帧，无任务 ID、无轮询
（与 ark 不同；依据官方文档
[1823/135745](https://cloud.tencent.com/document/product/1823/135745)）。
**2026-09-30 已真实下发 6 发多形态实测**（见
`reports/2026-09-30_tencent-hunyuan-probe/`，工具 `tools/probe_tencent_hunyuan.py`）。

- 接口：`POST https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v35-generation`
- 鉴权：`Authorization: Bearer <TOKENHUB_API_KEY>`（上游透传的 key）
- 参考图：`http(s)` URL 或 `data:` URI，单图 ≤ 20MB，最多 20 张
- 输出：签名 COS 链接，**12 小时过期**

## 配置步骤（New API 侧）

```
X-Upstream-Url:    https://tokenhub.tencentmaas.com/v1/wand/hunyuan-image/v35-generation
X-Script-Ref:      tencent_hunyuan/images@v1
Authorization:     Bearer <TOKENHUB_API_KEY>
```

可选：

```
X-Channel-Options: {"model": "hy-image-v3.5-preview"}          # 缺省即此值
X-Model-Map:       gpt-image-2=hy-image-v3.5-preview           # 优先级 > 选项 > 默认
X-Channel-Options: {"rehost_url": true}                        # 12h 链接转存为本站链接
```

## 字段映射

| 规范字段 | 上游字段 | 说明 |
|---|---|---|
| `prompt` | `messages[-1].content[].type=text` | 永远只有一条 user 消息，图片 part 在前、文本在后 |
| `image` (str/list) | `content[].type=image_url` | URL/data URI 直通；bare base64 补 `data:` 前缀 |
| `size` | `size` | 两侧同为 `"宽x高"` 拼写，原样转发 |
| `seed` `session` `footnote` `generate_max_pixels` `resize_max_pixels` `use_search_tool` | 同名透传 | payload 优先，渠道选项补位；都没有就不发 |
| `response_format=b64_json` | （转换） | 上游恒出 URL ⇒ 本地下载后编码 |
| `n` `quality` `style` `mask` `watermark` | （丢弃） | 上游无对应字段 |

## 实测事实（2026-09-30，6 发真实下发）

| 形态 | 结果 |
|---|---|
| 文生图（默认） | 1536×1536（1.5K 默认档，模型自选画布），18.9s |
| size=1024x1024 + seed=42 | **精确 1024×1024**，size 被采纳，9.1s |
| 参考图 data URI | 1536×1536，图生图通，11.0s |
| 参考图 bare base64 | 1536×1536，**补前缀后上游接受**，13.1s |
| response_format=b64_json | 1248×1872（模型自选竖版），适配器下载+编码通，11.7s |
| size=100x100 | **HTTP 200、256×256 出图**——越界 size 被**静默 clamp** 到边界，不是 400 |

- 每发耗时 7~19s；产物全部 PNG。
- **实测响应无 `usage` 字段**（文档称有 `usage.total_tokens`）。脚本转发分支保留
  （字段在才带出），上游将来恢复即可透传，无需改脚本。
- 审核拒答帧、12h 链接过期、`rehost_url`、多轮 `assembled_history` 未在本轮覆盖。

## 响应映射

- 成功：`choices[0].delta.image.{url,width,height}` → `data[0]`；`usage.total_tokens` 转发
- 失败：200 + `error` 对象 → `502 upstream_error`（**无图 200 同样判失败**，按文档建议以
  `image.url` 是否存在为准，而非 `finish_reason`）
- 拒答：命中共享审核词（`capabilities/_shared.json`）→ `400 content_filter`；
  厂商专属 code 未实测，暂不添加

## 重要注意事项

1. **输出默认带水印且无法关闭**：上游没有布尔开关，`footnote` 只是自定义文字。
   需要无水印交付的调用方请改走其他渠道。
2. **链接 12 小时过期**：正式消费方建议开 `rehost_url: true`（转存到对象存储），
   或拿到链接后立即下载。
3. **多轮编辑不在本脚本范围**：上游的 `assembled_history` 回灌机制被有意丢弃
   （适配器模型＝一次请求一个结果）；`session` 仅在调用方显式提供时透传。
4. **实测覆盖范围**：2026-09-30 已跑 6 发多形态（文生图/size+seed/双形态参考图/
   b64_json/越界 size）。未覆盖：审核拒答帧、`rehost_url` 转存（需对象存储）、
   12h 链接过期行为、多轮 `assembled_history`、`footnote` 水印文字——首次放量前建议补测。

## 测试验证

```
.venv/bin/python -m pytest tests/unit/test_tencent_hunyuan_script.py -q
```

## 参考文档

- 腾讯云 TokenHub 混元生图：https://cloud.tencent.com/document/product/1823/135745
- 脚本内 docstring（`script_store/tencent_hunyuan/images@v1.py`）为决策原文
