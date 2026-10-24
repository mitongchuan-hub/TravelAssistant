# 行前信息展示优化 Implementation Plan

> **For agentic workers:** 按本计划在当前会话内顺序实施；用户已确认天气摘要与装备建议列表方案。

**Goal:** 用户直接看清温度、天气提醒和简短装备建议，长建议可完整展开。

**Architecture:** 复用现有展示数据，修改 Jinja2 行前信息模板及局部 CSS。长建议使用原生 details，短建议直接呈现；不截断、改写生成内容。

**Tech Stack:** Jinja2、CSS、原生 HTML details、pytest、本地 Chromium。

## Global Constraints

- 延续雾蓝配色、单列手机布局和天气／装备／日期导航的顺序。
- 保留原始温度限定词、天气说明和建议全文；没有数据的字段不补造。
- 修改当前项目中的展示文件，保留已有用户文件；不执行提交或发布。

## Task 1: 天气摘要与装备列表

**Files:** `app/templates/partials/display_itinerary.html`、`app/static/workflow.css`、`app/templates/workspace.html`。

- [x] 天气日期在上，温度约 28px，天气概况在旁；降水为辅助行，提醒完整显示在底部。
- [x] 天气使用浅雾蓝表面，装备使用近白表面；两者保留 16px 圆角与内边距。
- [x] 装备建议不超过 80 字时直接展示，超过时使用完整的原生展开项。纯标题条目正常展示。展开操作具有明确文案、箭头、44px 点击区域和键盘焦点。

```jinja2
{% if key == 'clothing' and item.body and item.body | length > 80 %}
  <details class="display-prep-entry display-prep-disclosure">
    <summary><div><h5>{{ item.title }}</h5><span class="display-prep-toggle">展开详情</span></div><span class="display-chevron" aria-hidden="true"></span></summary>
    <p>{{ item.body }}</p>
  </details>
{% else %}
  <article class="display-prep-entry"><h5>{{ item.title }}</h5>{% if item.body %}<p>{{ item.body }}</p>{% endif %}</article>
{% endif %}
```

- [x] 更新行程页 workflow.css 的 URL 版本后缀，使刷新加载新样式。

## Task 2: 验证和设计记录

**Files:** `DESIGN.md`；浏览器检查产物写入 `.cache/preflight-ui/`。

- [x] 执行相关现有测试：`E:/anaconda/envs/TravelAssistant/python.exe -m pytest tests/automated/test_itinerary_fields.py tests/automated/test_transit_directions.py -q`。
- [x] 使用生产模板和样式渲染测试展示；检查 320px、390px 和桌面窗口，验证日期、完整原文、缺失天气字段、长文本换行、鼠标和键盘展开／收起及颜色对比度。
- [x] 在同一检查轮次查看手机与桌面截图，发现问题集中修正；最多追加一轮确认。
- [x] 执行一次 `node C:/Users/16156/.codex/skills/impeccable/scripts/detect.mjs --json app/templates/partials/display_itinerary.html app/static/workflow.css`，区分既有与新增发现。
- [x] 更新 DESIGN.md 的行前信息说明，执行 `git diff --check` 并确认本地服务可访问。

## 验证结果

- 相关现有测试通过：37 passed。
- 使用本地已保存行程的只读数据渲染生产模板，完成 320px、390px 和 1280px 窗口检查，无横向溢出；同时验证三天天气与长文本样例。
- 简短建议直接可见；长建议支持键盘展开、整行点击收起、可见焦点，完整原文和 HTML 转义保留；减少动态效果设置有效。
- 缺失预报保留说明、空行前信息隐藏；检查元素的最低文字对比度为 4.88:1。
- 机械样式扫描仅报告已有固定色值的 advisory；本次新增样式使用现有配色变量。
- 服务健康检查正常，HTTP 已返回更新后的样式。截图与浏览器结果保存在 `.cache/preflight-ui/`。
