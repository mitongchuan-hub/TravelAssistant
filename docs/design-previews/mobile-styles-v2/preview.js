const concepts = [
  ['亲切通讯录式', '完整对话气泡，旅行依据收成一行。', '抹茶绿 / 通讯式', 'Memos + Vant'],
  ['清爽对话式', '回复不套容器，让正文和结果说话。', '樱花粉 / 开放正文', 'NextChat'],
  ['旅行短记式', '沿时间阅读，把修改记成连续短记。', '杏黄 / 时间轴', 'Memos'],
  ['随身笔记式', '文档排版，行程草案成为阅读附件。', '浅紫 / 文档式', 'Memos + NextChat'],
  ['日程便笺式', '日期固定在侧，旅行依据靠近对话。', '湖蓝 / 日期锚点', 'Vant + Memos'],
  ['想法清单式', '对话与想法并列有序，不画漂浮卡片。', '蜜桃 / 清单衔接', 'Blinko + Memos'],
  ['轻量工具式', '收起装饰，结果与撤销共用操作带。', '青绿 / 工具区', 'NextChat + Vant'],
  ['柔和分区式', '用整片底色划分区域，而非层层卡片。', '淡紫 / 底色分区', 'Vant'],
  ['温暖阅读式', '奶咖色里的连续正文，像翻一本随身本。', '奶咖 / 阅读优先', 'Memos + NextChat'],
  ['圆润原生式', '标准行式信息，轻圆角与明确触点。', '雾蓝 / 原生分组', 'Vant + Blinko'],
];
const paths = {
  back: 'm14 5-7 7 7 7',
  book: 'M4 4h6a3 3 0 0 1 2 3v14a4 4 0 0 0-4-2H4V4Zm16 0h-6a3 3 0 0 0-2 3m8-3v15h-4',
  arrow: 'M5 12h14m-5-5 5 5-5 5',
  up: 'M12 19V5m-6 6 6-6 6 6',
  check: 'm5 12 4 4L19 6',
  undo: 'M4 8h9a6 6 0 0 1 0 12M4 8l5-5M4 8l5 5',
  chat: 'M20 11a8 8 0 0 1-8 8H5l-3 3 1-7a8 8 0 1 1 17-4ZM7 10h10M7 14h6',
  idea: 'M9 18h6m-5 3h4M8 14a6 6 0 1 1 8 0c-1 1-1 2-1 3H9c0-1 0-2-1-3',
  pin: 'M19 10c0 5-7 10-7 10S5 15 5 10a7 7 0 1 1 14 0ZM14 10a2 2 0 1 1-4 0 2 2 0 0 1 4 0',
  down: 'm7 10 5 5 5-5',
  info: 'M12 8h.01M12 11v6M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0',
  wifi: 'M3 8a15 15 0 0 1 18 0M6 12a10 10 0 0 1 12 0M9 16a5 5 0 0 1 6 0M12 20h.01',
  signal: 'M4 17v3m5-7v7m5-12v12m5-16v16',
};
function icon(name) { return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${paths[name]}"/></svg>`; }
const question = '第一天想逛西湖和喝茶，安排松一点，不要太赶。';
const intro = '好，把第一天留给西湖和茶香。';
const detail = '已按你的想法调整路线，下午多留一点休息时间。';
const caution = '交通时间为查询时估算；营业与预约信息，出发前仍需核实。';
function avatar() { return '<span class="avatar" aria-hidden="true"><svg viewBox="0 0 28 28"><path d="M6 7c0-3 4-4 6-1h4c2-3 6-2 6 1v11c0 5-16 5-16 0Z"/><path d="M11 14h.01M17 14h.01m-6 4q3 2 6 0"/></svg></span>'; }
function route() { return `<div class="route" aria-label="路线摘要"><span>住处</span>${icon('arrow')}<span>西湖</span>${icon('arrow')}<span>喝茶</span>${icon('arrow')}<span>住处</span></div>`; }
function warning() { return `<p class="caution">${icon('info')}<span>${caution}</span></p>`; }
function outcome() { return `<div class="outcome"><span class="saved">${icon('check')}行程已保存<span class="draft">草案</span></span><button class="open-guide">查看攻略${icon('arrow')}</button></div>`; }
function undo() { return `<button class="undo">${icon('undo')}撤销上次修改<span>可撤销 1 步</span></button>`; }
function label() { return `<div class="byline">${avatar()}<b>旅行助手</b><time>10:24</time></div>`; }
function response() { return `<div class="reply-copy"><p>${intro}</p><p>${detail}</p></div>${route()}${warning()}`; }
function user() { return `<div class="user"><p>${question}</p><time>10:24</time></div>`; }
function context() { return `<div class="context">${icon('pin')}<span>住宿已匹配</span><i></i><span>公交地铁</span><i></i><span>慢节奏</span></div>`; }
function chat(n) {
  if (n === 3) return `<div class="journal-date"><b>11月14日</b><span>第一天的安排</span></div><div class="timeline"><section class="entry user-entry"><div class="entry-meta"><time>10:24</time><b>我的想法</b></div><p>${question}</p></section><section class="entry assistant-entry"><div class="entry-meta"><time>10:24</time><b>旅行助手</b></div>${response()}${outcome()}${undo()}</section></div>`;
  if (n === 4) return `${user()}<article class="note-document"><header>${avatar()}<div><b>第一天 · 西湖与茶香</b><p>旅行助手 · 根据刚才的想法调整</p></div></header><div class="note-body">${response()}</div><footer>${outcome()}</footer></article>${undo()}`;
  if (n === 5) return `<div class="date-rail"><div class="calendar"><span>11月</span><b>14</b><small>周六</small></div><div class="date-description"><b>第一天 · 杭州</b><p>住处出发，当天返回</p><span>11.14 — 11.15 · 共 2 天</span></div></div>${user()}<div class="assistant">${label()}<article class="reply">${response()}${outcome()}</article>${undo()}</div>`;
  if (n === 6) return `<div class="idea-reference"><span class="small-label">这趟旅行的想法</span><div><span>${icon('check')}慢一点</span><span>${icon('check')}少走路</span><span>${icon('check')}西湖喝茶</span></div></div>${user()}<div class="assistant">${label()}<article class="reply">${response()}</article><div class="check-result">${outcome()}${undo()}</div></div>`;
  if (n === 7) return `${user()}<div class="assistant">${label()}<article class="reply">${response()}</article></div><section class="result-toolbar">${outcome()}${undo()}</section>`;
  if (n === 8) return `<section class="question-zone"><div class="zone-meta">我的想法<time>10:24</time></div><p>${question}</p></section><section class="answer-zone">${label()}<article class="reply">${response()}${outcome()}</article>${undo()}</section>`;
  if (n === 9) return `<div class="reading-date"><span>11月14日，第一天</span><span>10:24</span></div><div class="reader-question"><b>我说</b><p>${question}</p></div><article class="reading-reply"><h2>那就，慢慢逛西湖。</h2><div class="reading-author">${avatar()}<span>旅行助手</span></div>${response()}${outcome()}${undo()}</article>`;
  if (n === 10) return `${user()}<div class="assistant">${label()}<article class="reply">${response()}</article></div><div class="native-result">${outcome()}${undo()}</div>`;
  return `${user()}<div class="assistant">${label()}<article class="reply">${response()}${outcome()}</article>${undo()}</div>`;
}
function phone(n) {
  return `<main class="phone b${n}" aria-label="B${String(n).padStart(2, '0')} ${concepts[n - 1][0]} 静态示例"><div class="status"><span>9:41</span><div>${icon('signal')}${icon('wifi')}<span class="battery"></span></div></div><header class="top"><button class="back" aria-label="返回旅行列表">${icon('back')}</button><div class="title"><h1>杭州慢慢逛</h1><p>11月14日 — 15日 · 2天</p></div><button class="guide">${icon('book')}攻略</button></header>${context()}<section class="content" aria-label="聊天记录"><p class="conversation-date">今天 · 示例对话</p>${chat(n)}</section><div class="dock"><div class="composer"><span>再说一个旅行想法…</span><button class="send" aria-label="发送消息">${icon('up')}</button></div><p class="sample">静态设计 · 测试展示，非真实行程</p></div><nav class="bottom" aria-label="工作区导航"><button>${icon('idea')}<span>想法</span></button><button class="selected" aria-current="page">${icon('chat')}<span>聊天</span></button></nav><div class="home"></div></main>`;
}
const params = new URLSearchParams(location.search);
const n = Number(params.get('style'));
const app = document.getElementById('app');
if (n >= 1 && n <= 10) {
  document.title = `B${String(n).padStart(2, '0')} ${concepts[n - 1][0]}`;
  document.body.className = 'single';
  app.innerHTML = phone(n);
} else {
  app.innerHTML = `<main class="gallery"><header class="gallery-heading"><div><h1>第二组，把空间还给对话。</h1><p>10 款手机端构造研究 · 同一业务内容 · 保留轻松、圆润，不再依赖装饰。</p></div><a class="compare-link" href="compare.html">对比第一组 ${icon('arrow')}</a></header><div class="gallery-explainer"><span>借鉴 Memos 的内容层级</span><span>Blinko 的移动记录组织</span><span>NextChat 的阅读与输入边界</span><span>Vant 的紧凑组件尺度</span></div><section class="grid">${concepts.map((c, i) => `<article class="concept"><a class="phone-link" href="?style=${i + 1}" target="_blank" rel="noopener"><div class="mini"><iframe title="${c[0]}" src="?style=${i + 1}" tabindex="-1"></iframe></div><h2>B${String(i + 1).padStart(2, '0')} ${c[0]}</h2></a><p>${c[1]}</p><small>${c[2]}</small></article>`).join('')}</section><footer class="gallery-footer"><p>独立原创实现，学习信息组织，不复制参考项目源码或品牌。<br>聊天、想法、攻略入口与撤销对应现有业务；无后端请求。</p><p>静态研究，不是 10 套已上线主题。第一组保持原样。<br><a href="RESEARCH.md">查看源码研究记录</a> · <a href="../mobile-styles/index.html">查看第一组</a></p></footer></main>`;
  const scale = () => document.querySelectorAll('.mini').forEach(el => { el.querySelector('iframe').style.transform = `scale(${el.clientWidth / 390})`; });
  new ResizeObserver(scale).observe(document.querySelector('.grid'));
  scale();
}
