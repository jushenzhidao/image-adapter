# OPEN-DECISIONS 登记册

| Date | Source | Open Item | Related Constraints | Current Leaning | Blocked By | Resolves When | Status |
|------|--------|-----------|---------------------|-----------------|------------|---------------|--------|
| 2026-09-04 | Phase 1.5 | 真实异构上游接入（vendor 名称/API 文档/凭证） | 脚本映射需真实字段 | 先用 Mock 上游打通全链路 | waiting-on-external-condition：等用户提供上游 API 文档 | 用户提供后照 upstream_a/b 示例编写脚本 | OPEN |
| 2026-09-04 | Phase 1.5 | LOGFIRE_TOKEN 实际值注入 | 用户已确认持有 Token | 代码从环境变量读取，.env 留占位 | waiting-on-external-condition：等用户填入 .env | 用户填入后重启即上报云端 | RESOLVED (Resolution: 用户已提供 `LOGFIRE_TOKEN`，已写入 .env，Logfire 全链路追踪已启用。**凭据值不记入本文件**——本行原先把实际 token 明文写在这里，随公开仓库泄漏，2026-09-11 已脱敏并轮换) |
| 2026-09-04 | Phase 1.5 | 沙箱黑名单范围（移除 Subscript/Await） | 03_技术架构 §3.4 原设计误杀合法脚本 | 已裁决：只禁危险 import/exec/eval/dunder | - | 已在 Spec v1.0 落地 | RESOLVED (Resolution: Spec §11，禁 import 白名单外 + exec/eval/__import__/open/compile + dunder 属性访问) |
| 2026-09-11 | 线上报错定位 | 四个入口如何统一到规范契约 | README:5「以 `/v1/images/generations` 为唯一规范格式、脚本只实现一套契约」；`ctx.endpoint` 存在但无脚本使用 | 用户裁决：**按请求 path 判定，路由做前门，无条件、零配置**；不引入渠道选项开关 | - | 已实施（`docs/09`，`api/frontdoor.py`） | RESOLVED (Resolution: chat / responses 各补入口折叠 + 出口封装；脚本零改动。实测四路径全 200，改前 chat / responses 均 400) |
| 2026-09-11 | 用户裁决 | 前门的历史：拒绝还是截断（两个门） | `docs/01` §4.1 与 `docs/09` §3 原裁决「400 优于静默丢上下文」；规范契约只有一个 `prompt` / 一个 `image`，历史无处承载 | 用户裁决：**截断到最后一条 `user` 轮**（文本与图都只取最后一条，`assistant` 轮忽略），**两个门一致**；未知 `role` 仍 400 | - | 已实施（`frontdoor.messages_to_canonical` / `input_to_canonical`） | RESOLVED (Resolution: 原「多轮一律 400」改为截断。**代价是接受"静默丢上下文"这一最难点查的失败模式**——原裁决拒绝的理由正是它，现由调用方承担。`docs/01`/`docs/09`/`docs/06` 与验收项已同步修订。chat 门按 `messages` 逐轮切分；responses 门按"轮"切分且**连续裸 part 属同一轮**——按 item 截断会削掉指令只留图（`test_responses_bare_parts_are_one_turn_not_several` 守卫，已用变异验证会报红）) |

汇总：1 未决 + 3 已决
