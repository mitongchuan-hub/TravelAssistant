# 69字未完行程：单次真实模型复查（2026-11-04）

本节为新增独立诊断，不修改原验收报告的已确定事实。

## 范围与重建差异

目标原请求 `live-simple-plan-1793772863801`、任务 `380ff8d4d5454cf295cb3ff741e1c816`、合成测试旅行 `trip-103`。已定向读取 activation-snapshots、trace-summary 与 api-evidence，沿用完整用户原话、最新需求“不吃花生／尽量少走路”、北京饭店住宿、2026-11-05、transit 固定方式和原始初始投影。

原最终逐轮模型输入及完整工具返回没有留存，不能精确重放。此次使用当前 travel prompt、附件中的初始投影及一条明确标为诊断重建的 user 消息（不是伪造的工具转录）。仅引用确有记录的地点名称、ID、查询参数及成功状态；没有把原69字中的“900米”当作路线事实，没有编造天气值、返程耗时、票价、开放或预约条件。为尝试一次直接取得完整正文，额外发送了强制 `tool_choice=submit_changes`；这与原自动选工具不同，且本次端点未按该指定返回。

## 实际请求与返回

- 固定 conda 解释器，隔离脚本，仅读 `.env` 到内存，不回显凭证；复用实际 registry 的 model specs，包括 submit 根 anyOf 的现有 wire 投影。没有加载 app.main，阻止 sqlite 连接，没有执行 Store 或业务写入。
- 模型 `qwen3.8:27b`；Chat Completions；`reasoning_effort=low`（沿当前配置）；未发送 thinking 覆盖；非流式；timeout=240 秒；SDK `max_retries=0`；未发送温度与显式输出 token 上限，保持端点默认。未发送 store 参数，与现有 Chat Completions transport 一致；不能据此保证供应商端不留日志。
- HTTP 请求计数 **1**，HTTP 响应计数 **1**，HTTP **200**，耗时 **23.300 秒**。没有重试、备用模型、后续模型调用、工具执行、业务保存或网页生成。
- 原始 HTTP 与 SDK `finish_reason` 均为 **tool_calls**。
- 原始 HTTP 与 SDK `content` 原文均为：“我先读取当前保存的需求正文，确认最新状态。”，其后两个换行。
- 唯一返回工具为 `read_context`，参数原文 `{"section":"requirements"}`。实际发送体中明确指定的是 `submit_changes`，该差异在原始 HTTP 中已存在，非 SDK 改写。
- usage：prompt_tokens=**5694**（cached_tokens=**5430**）、completion_tokens=**2088**、total_tokens=**7782**。原始 HTTP 与 SDK 的各实际返回 usage 字段一致；SDK 额外补充若干 null 默认字段，故整对象直接相等为 false，不代表 token 值不同。
- 结束原因、content、tool-call ID／name／arguments 原始与 SDK 一致。原始响应另有 reasoning_content，已保留在脱敏 JSON；不能把其中的草稿当作最终 itinerary 或正式业务交付。

## 结果与边界

**没有生成 itinerary.text，也没有 submit_changes。因此本次不能判定“69字正文末尾停在‘符合’”是否复现，更不能判定问题修复。** 本次仅确认：该请求的原始服务响应返回了与指定 tool_choice 不同的工具。不能仅凭一次重建请求确定是模型、模板、服务端参数处理或其他环节导致；不能据原始 reasoning_content 草稿的未完句就断言原问题同源、输出上限或截断原因。

未生成正文，故禁忌、三餐、少走路、估算和末句完整性均不能作为正式行程验收。原始69字行程事实仍以旧证据为准。

证据：`qa/acceptance-2026-11-04/recheck-incomplete-plan.json`，含实际发送 JSON（无凭证 headers）、原始 HTTP body及 SHA-256、SDK 解析结果、计数、耗时、参数和字段一致性检查。

执行末尾发生一次 GBK 终端打印 UnicodeEncodeError；它发生在 JSON 完整写盘后，不是模型请求失败，未因此追加任何请求。临时脚本已清理。产品代码、.env、数据库及原报告未修改，未提交 git。
