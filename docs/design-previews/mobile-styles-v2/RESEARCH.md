# 第二组：GitHub 源码研究与应用边界

本次实际读取公开源码，不下载或执行第三方项目。源码来自可变的 main 分支，研究不是运行截图评测，也不能证明这些作品所有交互均完善。设计实现独立编写，不复制品牌、图像或源码。

## Memos：内容优先

- 仓库：https://github.com/usememos/memos （MIT）
- 页面：https://github.com/usememos/memos/blob/main/web/src/pages/Home.tsx
- 内容：https://github.com/usememos/memos/blob/main/web/src/components/MemoView/MemoView.tsx
- 容器尺度：https://github.com/usememos/memos/blob/main/web/src/components/MemoView/constants.ts
- 移动导航：https://github.com/usememos/memos/blob/main/web/src/components/AppSidebar/AppSidebar.tsx

观察：编辑器、列表、单条内容分工明确；元信息和正文分离。内容容器使用 px-4 / py-3 / mb-2 / rounded-lg 等紧凑尺度，移动头 h-12。迁移到 B03 的连续条目、B04 的文档边界、B09 的阅读层级。不照搬公开分享、多空间、评论或地图功能。

## Blinko：移动记录组织

- 仓库：https://github.com/blinkospace/blinko （GPL-3.0；只学习组织方式，不复制代码）
- 页面：https://github.com/blinkospace/blinko/blob/main/app/src/pages/index.tsx
- 底栏：https://github.com/blinkospace/blinko/blob/main/app/src/components/Layout/MobileNavBar.tsx
- 元信息：https://github.com/blinkospace/blinko/blob/main/app/src/components/BlinkoCard/cardHeader.tsx

观察：录入入口、加载空态、滚动区、内容卡拆分；移动单列，卡片头弱化时间等元信息。迁移到 B06 的文本想法上下文和 B10 的结果操作分组。不复制 RSS、分享、回收站、知识库等业务。其底栏源码未见完整可见标签/安全区设计，不能原样照搬。

## NextChat：消息阅读与输入边界

- 仓库：https://github.com/ChatGPTNextWeb/NextChat （MIT）
- 结构：https://github.com/ChatGPTNextWeb/NextChat/blob/main/app/components/chat.tsx
- 样式：https://github.com/ChatGPTNextWeb/NextChat/blob/main/app/components/chat.module.scss

观察：满高纵向布局，独立消息滚动区和输入区；手机收起桌面工具；消息正文、时间、输入有不同尺度；阅读旧消息时不强制跟随底部。迁移到 B01 的通讯布局、B02 的开放回复、B07 的清晰操作区。当前为静态稿，不声称已实现滚动跟随或动态输入。不引入模型切换、重生成、语音、附件等能力。

## Vant：移动网页组件尺度

- 仓库：https://github.com/youzan/vant （MIT）
- 导航：https://github.com/youzan/vant/blob/main/packages/vant/src/nav-bar/NavBar.tsx
- 导航样式：https://github.com/youzan/vant/blob/main/packages/vant/src/nav-bar/index.less
- 行式内容：https://github.com/youzan/vant/blob/main/packages/vant/src/cell/index.less
- 输入：https://github.com/youzan/vant/blob/main/packages/vant/src/field/index.less
- 底栏项：https://github.com/youzan/vant/blob/main/packages/vant/src/tabbar-item/index.less

观察：导航内容默认 46px，标题限宽，左右控制分区；信息使用连续行与内缩分隔线；选中态不改变底栏几何。迁移到全体紧凑标题/单行旅行依据，以及 B05 日期锚点、B08 分区、B10 原生行组。

## 反例而非母版

https://github.com/lmyddd/LangGraph-Travel 的 travel-h5/src/views/home/index.vue 与 chat/index.vue：虽然技术和场景相近，首页欢迎横幅、推荐卡、快捷宫格与渐变/emoji叠加，不符合用户此次减轻模板感的目标；聊天逐段强滚底也不值得照搬。未核实许可证，不复制。

## 统一真实业务边界

- 住宿日期表单创建，地图匹配后进入聊天；通过聊天生成、调整和确认。
- 底栏仍只有想法/聊天，顶部攻略为覆盖预览，真实产品可下载 HTML。
- 撤销最多最近三次实际 Agent 修改，示例显示一条可用，不是重做或恢复失败任务。
- 不增加预订、支付、社区、实时地图、PDF或多版本历史。
- 本目录全部测试展示、无业务请求；B06 的三个想法是合成背景示例，不表示本预览查询了真实想法或能手动勾选采纳。
