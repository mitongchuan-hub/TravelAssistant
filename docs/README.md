# 文档索引

当前功能与运行方式以项目[README](../README.md)、应用源码和下面的当前说明为准。带日期的验收记录反映当时测试条件，服务PID、样本数及阶段状态不能当作最新运行状态。

## 当前说明

- [同城游玩](city-outings-20261015.md)：`main` 的住宿地址入口、可选设置、每日往返、分支与数据库隔离。
- 2026-10-15之前的完整旅行规则和评测保留为兼容模式及历史证据，`travel-full` 分支保存改造前版本。

- [产品边界](product-boundaries-v1.md)：已确认的首版范围，区分目标与已实现能力。
- [当前架构](agent-architecture-current.md)与[角色实现](agent-role-implementation.md)：四意图、工具循环、当前行程保存和后台展示。
- [思考强度配置](model-reasoning.md)、[高德MCP](amap-mcp-implementation.md)、[展示协议](travel-itinerary-display-schema.md)。
- [规划提示词](planning-prompt-redesign-20261013.md)、[用餐约束](planning-meals-20261013.md)、[少样本示范](planning-fewshot-20261014.md)、[少样本复测](planning-fewshot-test-20261014.md)与[交付改进及残余](planning-delivery-20261014.md)：当前规则、示例对照、独立复测、接驳检查、概览预览及模型复核移除记录。
- [聊天界面](chat-redesign-20261013.md)、[行程卡片](itinerary-cards-ui-20261012.md)与[去重及最终布局](itinerary-repetition-20261012.md)。
- [测试目录与执行入口](../tests/README.md)。

## 实施与验收记录

- [当前行程简化](latest-itinerary-only-20261010.md)、[异步展示](async-display-implementation.md)、[日期完整性](display-completeness-implementation.md)、[七项优化](seven-optimizations-20261013.md)。
- [早期工作流验收](workflow-evaluation-20261010.md)与[根因排查](workflow-root-cause-20261010.md)：包含已被后续改造替代的阶段，保留问题与验证依据。
- [2026-10-08评测](travel-agent-evaluation-20261008.md)、[旧工作流评测](travel-agent-evaluation-workflow-20261010.md)、[最新版行程评测](travel-agent-evaluation-latest-only-20261010.md)。首轮失败与补测独立保留，不能合并成新的成绩。
- [准备事项独立提取实验](display-split-experiment-20261012.md)、[合并单次提取实验](display-combined-weather-experiment-20261012.md)及[忠实度阶段记录](display-fidelity-implementation.md)。生产最终协议与残余以七项优化和当前源码为准。
- [原始证据目录](evaluations/)与[界面截图](ui/)。
- [改造前全流程快照](agent-full-flow.md)：旧模型、旧执行结构的真实输入输出，保留追溯，不作为当前架构说明。

## 规划背景

[产品路线记录](product-roadmap.md)、[性能优化方案](agent-latency-optimization-proposal.md)和[2026-10-12优化计划](optimization-plan-20261012.md)仍含未完成目标、测量依据及取舍。阅读实施状态时应对照当前说明，不能按这些旧计划重复实施已完成的改造。

本次删除已被替代的Agent设计讨论稿、V1/V2角色协议、早期行程页面优化计划和四意图工作流讨论稿及其配图。真实评测、全流程原始快照、展示测试示例及归档指纹保留；旧版本可以通过Git历史查看。
