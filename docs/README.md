# 文档导航

## 当前规则与实现

- [PRODUCT.md](PRODUCT.md)：产品目标、范围、业务约束。
- [DESIGN.md](DESIGN.md)：既有手机布局和交互保护；技术迁移不等于视觉改版。
- [frontend-backend-migration.md](frontend-backend-migration.md)：Vue / FastAPI 迁移合同。
- [single-agent-crud-design.md](single-agent-crud-design.md)：单 Agent CRUD 的设计背景；当前路径与运行方法以根 README 和实际代码为准。
- [deployment/nginx.conf](deployment/nginx.conf)：可选同源部署示例，本地开发不需要 Nginx。

## 分类资料

- `archive/`：旧框架提案、旧运行循环解释与研究记录；不能当成当前代码说明。
- `learning/`：学习计划；旧代码路径和旧框架示例可能已失效。
- `career/`：简历、面试材料；包含历史阶段内容，不作为当前能力或指标证明。
- 其他既有专题目录保留其原用途；日期化方案和评测只描述当时状态。

文档归档不是删除功能，也不代表历史测试成绩仍然有效。启动与验证的唯一入口说明见 [项目 README](../README.md)。

当前不提供失败任务重试，失败后仅提示重新发送新的 chat；历史任务只读保留。网络送达不确定的同 request_id 恢复/去重、undo 网络幂等与 HTML 纠错/429 备用保留。

当前支持每旅行最近三次 Agent 成功业务修改逐步撤销；范围、手动修改屏障和无重做限制见 PRODUCT，原子存储与API合同见 backend-architecture。
