# 高德MCP接入验证归档

2026-10-13。后端已切换Streamable HTTP MCP，天气仍Open-Meteo。实施说明见[高德MCP记录](../../amap-mcp-implementation.md)。

归档包含：

- discovery：实际MCP服务/协议/工具清单、搜索结果、地点/餐厅详情和三种路线响应。官方服务名amap-sse-server，实际传输是Streamable HTTP。
- initial-success/results.json：第一轮直接应用工具7步全部available，共19次MCP tools/call，公交复查命中缓存；单次成功不能证明长期可用。初轮候选曾附加来源字段，随后根据回归反馈移到顶层，避免破坏可信目录/旧对象。
- interrupted-recheck：首个地点20秒回退，第二个在途请求受执行工具40秒截止中断；只有1份完成结果，不伪造第二份失败。
- independent-recheck：两个地点20秒回退，随后餐厅10.22秒成功；网络/握手阶段与工具阶段分开记录。
- final-smoke：最终只读发现阶段blocked，0个实际tools/call，未继续堆积查询。首次失败和独立补验全部保留。
- automated-tests.log：529项全量回归通过；automated-first-failures.log保留第一次完整回归的3项失败，修复后通过。
- source_snapshot：最终MCP适配、业务预算接入、协议测试、独立冒烟入口、计时工具及依赖/示例配置快照，不含.env或业务内容。
- business-before/after：本次上线前后只读数据计数和计划指纹；14趟旅行、85条消息、全部计划内容一致。只存指纹，不存用户原文。
- archive-hashes.json：归档内容SHA-256，除指纹文件本身。

本轮证明协议与适配接入、受控保护、部分真实查询和失败回退。后续真实查询存在上游超时，不能将第一次7/7当作稳定成功率，也没有执行真实LLM完整规划全量验收。路线/餐厅信息均需保留未来变化、报价参考及未知边界。

这些查询直接读取公开地点/餐厅/路线，不使用模型/业务Store，不向8000业务服务提交测试业务。Key没有进入MCP工具参数和归档；扫描未发现当前密钥值。
