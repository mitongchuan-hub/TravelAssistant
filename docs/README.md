# 文档导航

## 当前规则与实现

- [项目 README](../README.md)：安装、启动、验证入口与运行边界。
- [PRODUCT.md](PRODUCT.md)：产品目标、范围、业务约束。
- [DESIGN.md](DESIGN.md)：既有手机布局和交互保护；技术迁移不等于视觉改版。
- [backend-architecture.md](backend-architecture.md)：后端分层、原子存储与 API 合同。
- [frontend-backend-migration.md](frontend-backend-migration.md)：Vue / FastAPI 迁移合同。
- [single-agent-crud-design.md](single-agent-crud-design.md)：单 Agent CRUD 的设计背景；当前路径与运行方法以根 README 和实际代码为准。
- [architecture.svg](architecture.svg)：架构图资料。
- [deployment/nginx.conf](deployment/nginx.conf)：可选同源部署示例，本地开发不需要 Nginx。

## 测试、评测与调试证据

- [tests/](../tests/)：Python 自动化、范围评测与浏览器验证入口；运行方式见项目 README。
- [qa/TEST_REPORT.md](../qa/TEST_REPORT.md) 与 [qa/](../qa/)：既有测试报告及测试源码。
- [known-issues/](known-issues/)：日期化问题记录、范围评测与验证证据。
- [debug-report-2026-10-30.md](debug-report-2026-10-30.md)：专项调试记录。

## 设计预览资料

- [design-previews/mobile-styles/](design-previews/mobile-styles/)：样式设计说明、HTML 预览、渲染脚本与截图。
- [design-previews/mobile-styles-v2/](design-previews/mobile-styles-v2/)：第二版研究、设计、对比预览与验证资料。

## 研究与面试资料

- [archive/v3-agent-architecture-pi-reference.md](archive/v3-agent-architecture-pi-reference.md)：历史架构研究背景，不作为当前代码说明。
- [career/travelassistant-interview-qa.md](career/travelassistant-interview-qa.md)：面试问答材料，不作为当前能力或指标证明。

历史方案、评测与报告只描述记录时的状态，不代表当前实现或当前测试仍然通过。
