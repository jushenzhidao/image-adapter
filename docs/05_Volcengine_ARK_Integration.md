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
| `X-Script-Ref` | `volcengine_ark/images@v1` |
| `Authorization` | `Bearer <VOLCENGINE_ARK_API_KEY>` |
| `X-Channel-Options` | `{"model": "doubao-seedream-5-0-260128"}`（可选） |

适配脚本在 `script_store/volcengine_ark/images@v1.py`；
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

### 3. 固定参数
适配器自动添加以下参数：
- `watermark: true` - 启用水印
- `stream: false` - 禁用流式响应
- `sequential_image_generation` - 仅当调用方/渠道选项显式提供时才发送（5-0-pro 系列模型不支持该参数，硬编码会导致 400，2026-09-09 实测）

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
