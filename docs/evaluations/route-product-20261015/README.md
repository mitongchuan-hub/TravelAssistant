# 路线产品化验收记录

正式汇总见 [verification.json](verification.json)，用户步骤见[路线优先体验](../../route-product-20261015.md)。

## 结果

- 最终全量：841 passed，63.22秒；之后增加旧进程模板兼容一项，路线Web专项9 passed，6.35秒（没有将单独复测拼成新的全量成绩）。
- 依赖与差异空白检查通过；首次全量的测速常量解析失败已保留说明并修复复测。
- 浏览器：四宽320/390/430/1000px，主线5项、状态6项，首轮和确认轮分别保留，脚本错误0。
- 独立视觉复核：首轮fix；ready首屏、站点操作、未知费用、失败状态集中修复后，最终ship，限定项clear。

## 本机原始材料

下列运行产物位于 `.cache/route-product-20261015/`，不包含业务数据库或密钥，不进入源码提交：

- `browser-first/report.json`、`states-first/report.json`：首次浏览器记录及HTML/PNG。
- `browser-final/report.json`、`states-final/report.json`：最终确认记录及HTML/PNG。
- `browser-final/synthetic-route-viewport-390.png`：手机路线首屏。
- `browser-final/synthetic-route-viewport-1000.png`：桌面手机壳首屏。
- `browser-final/synthetic-adjust-390.png`：站点调整。
- `states-final/synthetic-generation-failed-390.png`：生成失败和重试入口。

这些是**合成离线夹具**，不是高德在线查询或真实出行样本；外链只校验参数，没有打开。没有执行真实模型、外部地图或实体手机内容验收。

## 隔离实例

`http://127.0.0.1:9012`，账号 `city-preview@example.com`，密码 `city-preview-2026`。

该实例为内存数据，重启清空；现有8000业务服务没有被重启。真实业务验收需按主文档重新启动现有服务，并核对`/health`版本 `20261015-route-2`。
