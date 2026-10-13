# 七项优化评测证据

对应[验收记录](../../seven-optimizations-20261013.md)。所有真实模型输出均来自隔离Store/测试进程；没有向8000业务服务发送测试旅行。部署验证单独只读比较业务快照。

- display-historical：最初JSON Schema输出4份，结构通过但七日严重遗漏及引号截断。
- acceptance：首轮新边界、连续流程、v2边界、七日真实规划、MCP和分类配对。连续追加、需求提取和七日预算失败保留。
- display-candidate：增强完整性提示词、low/medium各2份；活动改善，标题仍截断。
- display-object-candidate / display-plain-candidate：JSON对象和无约束JSON失败，不能作为通过。
- quote-transport：含引号最小复现，plain句意完整、JSON对象及Schema均截断。plain的引号字形改变也记录。
- display-yaml-candidate：块文本格式单样本验证，标题、活动和关键条件完整，仍用原Schema本地校验。
- final-acceptance：正式YAML四份固定原文、三份边界；五个业务边界初次复测、七日真实外部规划成功、MCP7/7、分类交替配对。连续精确编辑引用/空行失败保留。
- followup-acceptance：五个v2业务边界补测全部机器检查通过；连续流程追加仍多空行，记录没有覆盖。
- edit-final：取消模型选择分隔符后，以此前真实生成原文独立补测；精确追加和只读解释两步均通过，修改前台13.42秒、2次SDK、无外部查询，后台卡片89.61秒。
- final-source：最终运行逻辑、提示词、Schema、配置示例和依赖的源码快照。
- deployment：本次独立前后业务基线，schema5、14旅行、85消息，五类业务对象hash均一致，无活跃任务。
- *.log：自动回归、浏览器及执行进度日志，首次失败保留；最终自动581 passed。

sha256.json记录证据文件指纹。历史源码的嵌套归档没有重复拷贝，原仓库对应历史证据继续保留。输出日志扫描当前服务密钥并脱敏，包含合成用户输入，不包含真实用户聊天正文。

机器检查、结构日期通过、任务成功和进程退出0都不等于全面语义通过。人工残余及性能范围见验收记录。没有声称46条业务质量全量或26场景真实耗时全量已经完成。

本地服务已加载：2026-10-13，PID2604，http://127.0.0.1:8000。备份 `.cache/backups/travelassistant-before-schema-20261013-123408-025297.sqlite3`。未提交或推送远程。
