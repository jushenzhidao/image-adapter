# OPEN-DECISIONS 登记册

| Date | Source | Open Item | Related Constraints | Current Leaning | Blocked By | Resolves When | Status |
|------|--------|-----------|---------------------|-----------------|------------|---------------|--------|
| 2026-09-04 | Phase 1.5 | 真实异构上游接入（vendor 名称/API 文档/凭证） | 脚本映射需真实字段 | 先用 Mock 上游打通全链路 | waiting-on-external-condition：等用户提供上游 API 文档 | 用户提供后照 upstream_a/b 示例编写脚本 | OPEN |
| 2026-09-04 | Phase 1.5 | LOGFIRE_TOKEN 实际值注入 | 用户已确认持有 Token | 代码从环境变量读取，.env 留占位 | waiting-on-external-condition：等用户填入 .env | 用户填入后重启即上报云端 | RESOLVED (Resolution: 用户已提供 pylf_v1_us_flWPfCQt8TwVDPbZ6j0DxPnQf6hNfbxL0vvqL3qY466l，已写入 .env，Logfire 全链路追踪已启用) |
| 2026-09-04 | Phase 1.5 | 沙箱黑名单范围（移除 Subscript/Await） | 03_技术架构 §3.4 原设计误杀合法脚本 | 已裁决：只禁危险 import/exec/eval/dunder | - | 已在 Spec v1.0 落地 | RESOLVED (Resolution: Spec §11，禁 import 白名单外 + exec/eval/__import__/open/compile + dunder 属性访问) |

汇总：1 未决 + 2 已决
