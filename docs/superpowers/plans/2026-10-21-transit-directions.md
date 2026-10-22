# 公交地铁乘车说明实施计划

> **For agentic workers:** Use `executing-plans` task by task. The user has approved this design and requested implementation in the current workspace.

**Goal:** 同城行程使用公共交通时，正文与卡片能显示真实查询到的地铁线路、公交路数、上下车站、方向、换乘及步行接驳。

**Architecture:** `query_route` 保留所选方案的分段明细，规划能力将明细写入权威正文，Display Agent 保留正文中的乘车说明。现有地点与路线证据绑定继续为卡片提供确定性的交通详情，保持方案版本与方向匹配。

**Tech Stack:** Python、pytest、FastAPI、Jinja2、原生 CSS、现有高德 MCP 与模型客户端。

## Global Constraints

- 当前工作树已有未提交的目录调整，原地进行局部修改，保留其他改动。
- 不改需求、提交权限、版本保护和卡片生命周期；旧数据没有线路明细时正常降级。
- 线路、方向、站点、出口与途经站数仅取真实返回；不猜测缺失字段。
- 同一段的多个公交选项是替代关系；跨段才是连续乘坐与换乘。
- 路线总耗时继续使用所选完整方案，不把步行或候车重复相加。
- 保留既有雾蓝样式与行程结构，新增说明放在原交通区域。
- 使用 `E:/anaconda/envs/TravelAssistant/python.exe` 运行测试与脚本。

## Task 1：路线工具保留明细

文件：`app/tools/routes.py`、`tests/automated/test_transit_directions.py`。

- [x] 先测试直达地铁、公交换乘与步行、同段多个可选线路、第一条无效方案、明细缺失与异常字段。
- [x] 运行新增测试，确认当前实现遗漏线路的失败。
- [x] 在已选择的同一方案上提取 `transit_steps`：walking、ride、unknown。ride 内保留 `lines` 可选集合、入口与出口；每条线路保留原名、上下车站、明确方向、原始途经站数与估计时长。

```python
options.append((duration, distance, path))
duration, distance, selected_path = options[0]
if mode == 'transit':
    result['transit_steps'] = _transit_steps(selected_path)
```

- [x] 运行路线测试，确保未知明细不影响合法总耗时，步行、驾车和同一地点分支正常。

## Task 2：卡片接入可信交通明细

文件：`app/views/route_view.py`、`app/templates/partials/route_leg.html`、`app/templates/partials/display_itinerary.html`、`app/static/city-route.css`，以及 `app/main.py` 的静态资源版本。

- [x] 先测试由真实 `query_route` 输出构成的证据，经 `build_route_view` 和 Jinja 渲染后仍能看到线路、上下车站、步行与换乘。
- [x] 测试相反方向、不同方式、失效正文绑定及未匹配地点不显示错误线路，旧证据无明细仍能阅读。
- [x] `_route_records` 只携带当前匹配结果的公共交通明细；`_leg` 形成线路概览和有序步骤，替代线路保留“或”，连续乘车标明换乘。

```python
leg['transit_steps'], leg['line_summary'] = _transit_view(record.get('transit_steps'))
```

- [x] 模板使用默认自动转义呈现步骤，原有高德入口与查询依据继续保留；卡片内增加简短线路提示，详情沿用原折叠区域。
- [x] 测试恶意文本被转义，长线路名可换行；检查窄屏与桌面，不重排卡片。

## Task 3：正文与提取合同

文件：`app/prompts/agents/planning.txt`、`revision.txt`、`research.txt`、`display.txt`。

- [x] 规划、修改、查询提示词要求采用 `transit_steps`，说明具体线路、站点、换乘与有依据的步行接驳，缺失项保持未知。
- [x] Display 保留完整乘车顺序和可选关系，不压缩为“公交约若干分钟”，不将途经站数改为推算乘坐站数。
- [x] 用真实模型对包含明确乘车说明的测试正文执行 Display 提取，检查线路与换乘保留；单独检查规划能力是否将工具明细用于正文。

## Task 4：验证与交付

- [x] 运行新增测试和完整自动化回归。
- [x] 使用真实高德查询直达地铁及公交/换乘示例，检查工具输出和渲染内容，实际查询不写入用户业务数据。
- [x] 运行界面机械检查，批量查看手机和桌面渲染。
- [x] 审查修改差异与职责范围，记录实际验证结果及外部服务限制。

```powershell
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m pytest tests/automated/test_transit_directions.py -q
& 'E:/anaconda/envs/TravelAssistant/python.exe' -m pytest tests/automated -q
```

## 实施与验证记录（2026-10-21）

已完成工具、正文提示词、卡片提取和页面展示的接通。卡片默认显示线路概览，展开“地点与交通”后显示有序乘车与步行步骤；同段可选线路使用“或”，后续已知乘车段使用“换乘”。未知接驳保留为待核实段，不能跨过未知段推断直接换乘。原始方向字段为二进制标记或未提供时不解释为方向名称。

真实接口还存在嵌套空占位：`railway={"name":"","trip":""}`、`bus={"buslines":[]}` 和零距离步行。归一化已区分这些占位与有内容但无法识别的缺失段；同一地点无需交通接驳时不提示缺少线路。

| 验证 | 实际结果 |
| --- | --- |
| 自动化回归 | 原有 57 项，新添 24 项，完整 `tests/automated` 共 81 项通过；新增行为先复现失败再修复 |
| 真实地铁直达 | 火车东站(地铁站) → 龙翔桥(地铁站)，保留地铁1号线及两端车站 |
| 真实多段换乘 | 火车东站(地铁站) → 灵隐寺，保留地铁1号线 → 地铁3号线 → 黄龙体育中心至灵隐专线，以及各段步行和已提供的 B口 |
| 真实公交 | 杭州西湖风景名胜区-湖滨公园 → 太子湾公园-九曜山，保留4路、龙翔桥公交总站上车和赤山埠下车 |
| 真实规划能力 | 使用隔离的内存旅行，从杭州新侨饭店往返灵隐寺；正文采用查询返回的7路及去返程各自上下车站 |
| 真实 Display 提取 | 正文中的7路、湖滨、灵隐公交总站、胜利剧院(龙翔桥)均保留；另用合成案例验证28路或28路区间、换乘地铁2号线、A/B口和步行仍属于同一交通条目 |
| 页面验证 | 本机无头 Edge 检查390px手机和1280px桌面；4组卡片、14个交通步骤无横向溢出；已查看两种截图 |
| 界面机械检查 | `detect.mjs --json` 对三个界面修改文件返回 `[]` |
| 独立代码审查 | 复核最终修改及24项新增测试，无遗留审查项 |

原始查询、内存方案、模型整理输出和页面截图保存在本地 `.cache/transit-directions/`，没有发布或写入用户业务数据库。CSS 缓存版本更新为 `20261021-transit-directions`。

旧方案只有总耗时而没有乘车明细时会提示“线路明细未提供”；需要重新查询并生成或修改相关路段，单独重试卡片整理不能补出原文和证据中不存在的线路。接口未提供的独立方向、途经站数、出口和票价仍不保证可用。

实际模型样例另外出现了时间一致性问题：15:30出发、返程约65分钟，却写成16:00前返回。本次模型验证检查线路与提取保真；该时间问题需另行处理，测试样例未成为用户行程。
