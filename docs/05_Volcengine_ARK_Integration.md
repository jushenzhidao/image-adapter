# 火山方舟 (Volcengine ARK) 接入指南

## 概述
火山方舟 doubao-seedream-5-0-260128 模型已成功接入 OpenAI Images 适配器。

## 配置步骤

### 1. 环境变量
在 `.env` 文件中添加：
```bash
VOLCENGINE_ARK_API_KEY=ark-your-api-key-here
```

### 2. 路由配置
已在 `config/upstreams.yaml` 中添加：
```yaml
volcengine_ark:
  base_url: "https://ark.cn-beijing.volces.com"
  timeout: 60
  async_mode: false
  auth_type: "bearer"
  auth_config:
    env_key: "VOLCENGINE_ARK_API_KEY"
  models:
    - doubao-seedream-5-0-260128
    - doubao-seedream
  endpoints:
    - images
```

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
- `sequential_image_generation: "disabled"` - 禁用顺序生成
- `watermark: true` - 启用水印
- `stream: false` - 禁用流式响应

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
启动适配器后运行：
```bash
python test_volcengine_integration.py
```

## 已知限制

1. **最小尺寸限制**：不支持低于 2k 的尺寸
2. **批量生成**：暂不支持 `n > 1`（需在适配器层聚合多次调用）
3. **图片有效期**：生成的 URL 有效期 86400 秒（24小时）

## 参考文档
- 官方文档: https://console.volcengine.com/ark/region:cn-beijing/docs/82379/1541523
- 模型名称: `doubao-seedream-5-0-260128`
- API 端点: `https://ark.cn-beijing.volces.com/api/v3/images/generations`
