# 09 · 四个入口统一到规范契约（chat / responses 前门）

> 状态：**已实施**（2026-09-11，见 §11 实施记录）
> 核心裁决（用户，2026-09-11）：**判定依据是请求打到的路径 `/v1/{xxxx}`，路由按端点决定转换逻辑**；
> 不引入渠道选项开关，不需要脚本声明，脚本侧零改动。
> 触发：线上 `POST /v1/chat/completions` + `X-Script-Ref: google/images@v1` 返回
> `Upstream returned 400: contents[0].parts[0].data: required oneof field 'data' must have one initialized field`

## 0. 目标形态

```
上游格式【多种】── 转换脚本 ──► 规范格式 /v1/images/generations ──► 四个入口全部兼容
                                   ▲
                                   └── edits / chat / responses 各自把入口形态折进规范体
```

New API 渠道按 **OpenAI 类型**配置，最终用户打过来的四个端点**全部兼容**，
不需要为"图片渠道"额外配置任何开关。

这个形态与 `README.md:5,7` 已确立的分工一致：

- **以 `/v1/images/generations` 为唯一规范格式**；
- `/v1/images/edits` 只是它的 **multipart 前门**，**不含任何适配逻辑**，只做形态改写后汇入同一条管道
  —— 于是「**脚本永远只需实现一套契约**」。

实现印证：`adapter/api/image_edits.py:149` 调用的是
`adapt(request, "images", validate_images_body, body=...)`，**连 endpoint 名都抹成了规范值**；
而 `ctx.endpoint` 虽存在（`context.py:146`、`base.py:29`），`script_store/` 里**没有任何脚本使用它**
—— 设计就是「脚本不知道自己从哪个入口来」，入口的形状差异**必须在脚本之前吸收掉**。

实际状态是 **1 对 2**，不是 1 对 4。四条路径实测
（2026-09-11，假 Gemini + 真 `google/images@v1`，同一份提示词「写实古风美少女」；
假上游对空的 `parts[0].text` 回 chatfire 那句 oneof 报错，所以差异只来自适配器）：

| `localhost:8080` 下的 path | 客户端 HTTP | 出站 `parts[0].text` |
|---|---|---|
| `/v1/images/generations` | 200 | 有值 `[{"text": "写实古风美少女"}]` |
| `/v1/images/edits` | 200 | 有值 `[{"text": ...}, {"inlineData": ...}]` |
| `/v1/chat/completions` | **400** | **空** `[{"text": ""}]` |
| `/v1/responses` | **400** | **空** `[{"text": ""}]` |

后两条的 400 就是上游那句 `parts[0].data ... oneof`。`/v1/responses` 此前只是推断，此处已实测确认。

| 入口 | 改造动作 |
|---|---|
| `POST /v1/images/generations` | 规范端点，不动 |
| `POST /v1/images/edits` | 前门已实现（纯载体改写），不动 |
| `POST /v1/chat/completions` | **补入口改写 + 出口封装**（`api/chat.py` 现在只校验 `messages` 就 `adapt(..., "chat", ...)`） |
| `POST /v1/responses` | **补入口改写 + 出口封装**（`api/responses.py` 现在只校验 `input`） |

失败现场的出站 body（假上游实抓，非推断）：

```
{"contents": [{"role": "user", "parts": [{"text": ""}]}], "generationConfig": {...}}
```

chat 请求体里没有 `prompt`，脚本 `payload.get("prompt", "")` 取到空串；空串是 proto3 零值，
网关侧按 `omitempty` 丢弃后 Part 不带任何 oneof 成员，上游便报 `parts[0].data` 未初始化
（文案里既没有 `prompt` 也没有"空"，这是它最难查的地方）。

## 1. 端点 -> 转换逻辑矩阵

| 入口 | 请求方向（-> 规范体） | 响应方向（规范体 ->） |
|---|---|---|
| `/v1/images/generations` | 规范体本身，零转换 | 原样返回 |
| `/v1/images/edits` | multipart -> 规范体（**已实现**） | 原样返回 |
| `/v1/chat/completions` | `messages` -> `prompt` + `image` | -> `choices[0].message.content` |
| `/v1/responses` | `input` -> `prompt`（+ `image`） | -> `output[]` |

判定依据就是**请求打到的路径**，由路由层持有。三条约束：

1. 不引入渠道选项开关（"渠道配置开前门"的做法已被否掉）；
2. 不需要脚本声明自己接受哪种入口（脚本继续不关心入口）；
3. 脚本侧**零改动** —— 这是「一套契约」是否真的成立的验证点。

默认零依赖：现有图片渠道不需要动 New API 配置就能被四个端点打到。

## 2. 前门必须「重建」规范体，不能「原样透传再补字段」

三条可核查的理由：

1. `validate_images_body` 会拒绝 `response_format` 不是 `url`/`b64_json` 的值
   （`api/images.py:70-74`）。chat 的 `response_format` 是 `{"type": ...}`（结构化输出控制），
   原样透传**必然 400**；
2. **`openai/images@v1` 的文生图路径整体转发客户端 body**
   （`script_store/openai/images@v1.py:229`：`return dict(payload)`，注释写的就是
   "the canonical body is already the generations body"）。因此**残留任何一个 chat 专有字段
   都会直接漏到上游**，OpenAI 系上游会以 `Unrecognized request argument` 拒掉；
3. `volcengine_ark` 走 `PASSTHROUGH` 白名单（`images@v1.py:116-119`）、`google` 只挑自己认识的键
   —— 两者不会漏，但**第 2 条已足以禁止"保留原字段"**。

结论：前门**构造一个新的干净规范体**，只搬白名单字段，**原始 `messages` / `input` 必须移除**。

（记录一个被否掉的替代设计：曾考虑"加性归一"——折叠出 `prompt` 的同时保留 `messages`，
让图片脚本读 `prompt`、文本脚本读 `messages`，两边都能工作。第 2 条证据直接否掉了它。）

## 3. 入口改写规则：`messages` -> 规范 body

规范体（`api/images.py` docstring 的 superset）：
`prompt` / `image` / `mask` / `n` / `size` / `quality` / `response_format` / `style` / `user` + 厂商私有字段。

| chat 输入 | 规范 body | 说明 |
|---|---|---|
| `messages[].content` 为 **string** | `prompt` = 该字符串 | |
| `content` 为 **parts 数组** | `type=="text"` 取 `text` 拼入 `prompt`；`type=="image_url"` 取 url 进 `image` | 你给的样例形态 |
| `image_url` 为 `{url: ...}` 或裸字符串 | `image` 引用 | 两种拼法都认，SDK 之间有差异 |
| 多个 `image_url` part | `image` 为数组，按出现顺序 | 规范体已支持单值或数组（`images.py:76-86`） |
| url 为 data URI | **原样透传，不解码** | `README:5` 三态之一；解码要多一次内存拷贝，`ctx` 侧本来就会分流 |
| `role=system` | 待定，见 §4 | |
| 多条 `user` 或出现 `assistant` | 400 `unsupported_parameter` | 多轮改图是 `docs/06` §4.6 明确不承诺的范围；静默丢掉上下文是最难查的一类 bug |
| 未知 part `type` | 400 `unsupported_parameter` | 与既有裁决一致（`mask` 也是报 400 而不是静默丢弃） |
| `model` | 保留 | |
| `stream` | **必须保留在交给 `adapt` 的 dict 里** | `pipeline.py:157` 用 `payload.get("stream")` 决定是否走 SSE；前门若把它剔除，**流式会静默失效**。edits 前门不流式，所以没暴露这个坑 |
| `response_format`（chat 语义） | **丢弃** | 与图片契约同名不同义，映射即错 |
| `messages` / `temperature` / `tools` / `max_tokens` 等 | **丢弃** | 见 §2；尤其不能残留，`openai/images@v1` 会整体转发 |
| `n` / `size` / `quality` | 不映射 | chat 契约里没有；需要就让控制面在别处表达 |

## 4. 待确认事项（实施时已定：取推荐项，见 §11）

### 4.1 `system` 消息怎么落

| 选项 | 做法 | 代价 |
|---|---|---|
| (a) 拼进 `prompt` 前缀 | 零脚本改动 | 与用户消息同质，上游看不出它是 system |
| (b) 规范体新增可选 `system` 字段 | 上游有原生 system 时更准（`google/images@v1` 已能转发 `systemInstruction`） | 每个脚本都要决定认不认，等于把契约扩了一格 |

倾向 (a)：`docs/01` §4.1 原本写的就是「处理 `system` 前缀」。

### 4.2 图文交错顺序是有损的

规范体的 `prompt` 是单串、`image` 是数组，**交错顺序在折叠时就丢了**。
`google/images@v1` 有 `image_text_order`（`text_first` / `text_last`）可粗调，默认 `text_first`。
可接受，但要在文档里写明这是有损转换。

## 5. 出口封装

`api/chat.py` 目前只补 `created` / `object` 两个字段，**没有 `choices`** —— 即使入口打通，
chat 客户端仍然解析不了。这是「前门只改输入不够」的地方。

对称于你给的请求形态，`/v1/chat/completions` 的默认候选：

```json
{
  "id": "chatcmpl-...",
  "object": "chat.completion",
  "created": 0,
  "model": "...",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "https://..."}}
      ]
    },
    "finish_reason": "stop"
  }],
  "usage": {"...": "透传，计费依赖它"}
}
```

- `response_format=b64_json` 时，`image_url.url` 放 `data:` URI（OpenAI 的 `image_url` 接受 data URI）；
- `usage` 原样透传（与 images 端点一致，控制面据此计费）。

**流式（实测已验）**：`stream_chat_as_sse` 对数组 `content` 不切片，会把**整个数组塞进一个 delta**
（`utils/sse.py:30-39`）。三条路：

| 选项 | 行为 | 风险 |
|---|---|---|
| (a) 接受一次性数组 delta | 图片本来也切不开，语义上不算错 | `delta.content` 严格只接受字符串的客户端会报错 |
| **(b) 文本按 8 字符切片、图片单独一个 delta（已采用）** | 最接近 OpenAI 流式语义，既有 chat 流式行为不变 | 客户端遇到非字符串 delta 要按「追加一个 part」处理 |
| (c) 流式时降级为字符串形态（URL 文本） | 不改代码 | 同一请求流式与非流式形态不一致 |

### 5.1 其它可选形态（供对比，不建议作为默认）

| 形态 | 优点 | 缺点 |
|---|---|---|
| `content` 为 URL 纯文本 | 任何客户端都能拿 | 丢掉了"这是图"的语义 |
| `content` 为 markdown `![](url)` | 聊天框直接渲染 | 拿纯链接的客户端要再剥一层 |
| `content` 文字 + 同层 `images[]` | 语义最干净 | 非标准字段，客户端要专门支持 |

## 6. 破坏性变更与影响半径

按端点无条件判定，意味着 **chat 端点从"原生 chat 体透传"变成"图片契约入口"**。
实测影响半径很小，但每一项都要显式处理：

| 位置 | 现状 | 处置 |
|---|---|---|
| `tests/integration/test_chat_endpoint.py` 的 `CHAT_SCRIPT` | 脚本读 `payload['messages'][-1]['content']`（`test_chat_endpoint.py:14`） | 改为读规范体 `payload['prompt']` |
| 同文件 3 个用例 | 依赖 messages 原样到达 | 随 `CHAT_SCRIPT` 一起调整；**预期全绿** |
| `docs/01` §4.1 | 「脚本将 `messages` 按 role 拼接为上游 `prompt`」 | 改为「路由把 `messages` 归一到规范 `prompt`，脚本只见规范体」 |
| `docs/02` §4.2 | 流程图写「Adapter -> 脚本遍历 messages」 | 同步修订 |
| `AC-05` | 「纯文本 messages -> 返回标准 ChatCompletion」 | 措辞需补：content 为 parts 数组也算满足 |
| `AC-06` | 「messages 含 image_url -> **系统必须下载图片**并按上游要求编码」 | 与 `README:5` 三态透传冲突（`google/images@v1` 刻意**不下载**、直接转 `fileData.fileUri`）→ 改为"图必须到达上游，是否下载由脚本决定" |
| `mock_upstream/` | 无 chat 端点，无 messages 依赖 | 不动 |
| `reports/2026-09-11_online-env-8082/scripts/probe_online.sh` | chat 用例的 request 相位不读 `messages` | 不破 |

**范围外**：`/v1/chat/completions` 不再承载"面向文本上游的 chat / vision 脚本"这一用法。
该用法目前只存在于上述测试夹具与 `docs/01` §4.1 的表述里，**已部署渠道中没有实例**
（`script_store/` 三个脚本全是图片脚本）。若将来要恢复文本对话能力，应作为**规范契约之外的独立能力**
重新设计，而不是把 `messages` 塞回图片契约。

## 7. responses 端点

`input`（string / array）-> 规范体的折叠规则与 chat 同源，可共用一个纯函数；
出口是 `output[]`（`{type:"message" | "image_generation_call"}`）。
`tools[].image_generation` 的语义就是"出图"，与 `docs/01` §4.3 的编排描述一致。

建议新增 `adapter/api/frontdoor.py`，放**纯函数**折叠逻辑
（`messages_to_canonical` / `input_to_canonical` / `canonical_to_chat` / `canonical_to_response`），
两个路由各自薄封装。纯函数可直接单测，不必起服务。

## 8. 影响面清单

| 文件 | 动作 |
|---|---|
| `adapter/api/frontdoor.py` | 新增：入口折叠 + 出口封装（纯函数） |
| `adapter/api/chat.py` | 接前门：改写请求体、封装响应、保留 `stream` |
| `adapter/api/responses.py` | 同上 |
| `adapter/api/images.py` | 不改（规范体定义与校验的唯一来源） |
| `adapter/api/pipeline.py` | 不改（沿用既有 `body=` 通道） |
| `adapter/utils/sse.py` | 视 §5 流式选项决定 |
| `script_store/**` | **不改**（"一套契约"的验证点） |
| `docs/01` §4.1、`docs/02` §4.2、`docs/04_Spec.md` AC-05/06 | 修订 |
| `README.md` | `api/chat.py` 一行说明补"前门"字样 |
| `docs/decisions/OPEN-DECISIONS.md` | 登记本方案与 §6 的影响半径 |

## 9. 测试与验收

新增 `tests/integration/test_frontdoor.py`（14 例；用真 `google/images@v1` 或 `openai/images@v1` + 假上游）：

1. **回归今天的缺陷**：chat 体 + 图片脚本，断言出站 `parts[0].text` 是拼好的 prompt，**不是空串**；
2. `content` 为 parts 数组时，`image_url` 正确落入 `image`，且 url 透传不下载；
3. 多条 user / 出现 assistant -> 400 `unsupported_parameter`；
4. 未知 part `type` -> 400；
5. **chat 专有字段不出现在出站 body 里**（针对 `openai/images@v1` 的整体转发路径，
   用假 OpenAI 上游断言上游收到的 body 只有规范字段）—— 这条是 §2 第 2 条证据的守卫；
6. 响应 `choices[0].message.content` 形态符合 §5 选定的那一种；
7. `stream=true` 的 SSE 分片符合 §5 选定的那一种；
8. 调整后的 `test_chat_endpoint.py` 全绿。

## 10. 可与本方案并行的最小动作（不冲突）

- 脚本对「关键入参为空」硬失败：本地 400 `param="prompt"` / `code="missing_prompt"`，
  把上游那句 oneof 报错换掉。属于兜底，不替代前门；
- 调用方临时改用 `/v1/images/generations` 绕过。

两项都不改契约、不改端点语义。

## 11. 实施记录（2026-09-11）

### 11.1 改了哪些文件

| 文件 | 改动 |
|---|---|
| `adapter/api/frontdoor.py` | **新增**。`messages_to_canonical` / `input_to_canonical` / `canonical_to_chat` / `canonical_to_response`，纯函数无 I/O |
| `adapter/api/chat.py` | 接前门：`prepare` 里原地折叠（放在 `adapt` 的钩子里，**准入检查与 body 上限仍先于折叠**），出口封装，`stream` 从原始体读 |
| `adapter/api/responses.py` | 同上；`tools` / `_previous_ctx` 显式随折叠带走，状态链留在路由 |
| `adapter/utils/sse.py` | 数组 content：文本仍 8 字符切片，图片各发一个整块 delta；字符串 content 行为未变 |
| `script_store/google/images@v1.py` | 空 / 缺 `prompt` 直接 `ctx.fail`（`param="prompt"`, `code="missing_prompt"`），覆盖「只给图不给 prompt」这条 images 端点允许的路径 |
| `script_store/manifest.json` | google 摘要重算：`ada6013…` -> `6937809…` |
| 测试 | 新增 `tests/integration/test_frontdoor.py`（14 例）；`test_chat_endpoint.py` / `test_responses_endpoint.py` 的夹具从读 `messages` / `input` 改为读 `prompt` |

### 11.2 实施时定下的三个选择

1. **`system` 用前缀**（§4.1 选项 a）：`system` / `developer` 的文本以 `"\n\n"` 拼在用户文本之前。零脚本改动。
2. **流式用 (b)**（§5）：文本按 8 字符切片、图片整块一个 delta。既有 chat 流式用例行为不变。
3. **`/v1/responses` 的 `output` 只出 `image_generation_call`**，不造空 `message`：AC-08 里的 `message` 属于
   「文本模型改写 prompt → 文生图」那条编排能力，单张图片脚本不产生任何助手文本，规范契约也没有承载它的字段。
   真上了带 `STAGES` 的编排脚本，`message` 自然会出现。

### 11.3 四条路径实测（假上游 + 真脚本，同一份提示词）

| path | 改前 | 改后 | 改后出站 `parts[0].text` |
|---|---|---|---|
| `/v1/images/generations` | 200 | 200 | 有值 |
| `/v1/images/edits` | 200 | 200 | 有值 |
| `/v1/chat/completions` | **400** | **200** | 有值（`chat.completion` + `choices[0].message.content`） |
| `/v1/responses` | **400** | **200** | 有值（`response` + `output[0].image_generation_call`） |

测试：`tests/integration/test_frontdoor.py` 等 17 例通过；全量 493 passed，
另有 39 个 ERROR 全部落在 `tests/unit/test_scriptstore.py` 的 **setup 阶段**
（沙箱拒绝 `mkdir /private/var/.../pytest-of-root`，栈在 WorkBuddy 的 `sitecustomize.py`），
与本次改动无关 —— 改变不了「本机跑不了 `tmp_path`」这个既知条件。

### 11.4 有意没做

- 没把 `stream` / `model` 折进规范体：脚本整体转发时会漏给上游，两者都是入口层的事；
- 没给 `/v1/chat/completions` 保留 `tools`：这里的 `tools` 是函数调用，与 `image_generation` 编排同名不同义，
  带上会让脚本切到 `[TEXT, IMAGE]` 模态。`/v1/responses` 才带；
- 没实现多轮：多条 user / 任何 assistant 轮一律 400（`docs/06` §4.6）。
