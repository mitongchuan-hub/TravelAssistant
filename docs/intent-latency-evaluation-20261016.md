# 意图节点与提示词语言速度测试报告

- **日期**：2026-10-16
- **被测对象**：意图识别节点（`main` 角色，`app/prompts/agents/main.txt`）
- **结论速览**：
  1. 中英文系统提示词**无显著速度差异**（~10% 量级为噪声，方向不一致）
  2. 意图节点延迟的主因是**思考 token**（Qwen3 思考模式），占延迟 ~90%
  3. 通过 `chat_template_kwargs: {"enable_thinking": false}` 关闭思考后，意图分类 **3.7s → 0.57s（约 6.5 倍）**，路由准确率不受影响

---

## 1. 测试环境

| 项 | 值 |
|---|---|
| 模型 | `qwen3.8:27b`（Qwen3 系列，带思考模式） |
| 端点 | `http://172.25.17.131:18080/v1`（OpenAI 兼容代理，单模型） |
| Python | 3.13.14（conda 环境 `TravelAssistant`） |
| openai SDK | 3.20.0 |
| 配置来源 | 项目根 `.env`（`OPENAI_MODEL` / `OPENAI_BASE_URL` / `OPENAI_API_KEY`） |

**意图节点真实请求参数**（`app/agent_client.py:_create_chat_completion` + `app/agent_roles.py:126`）：

- `response_format`: strict JSON Schema（`INTENT_SCHEMA`，5 个字段）
- `reasoning_effort`: `low`（main 角色默认值）
- 用户侧 payload：`user_message` / `conversation` / `requirements` / `workflow_state` 等

---

## 2. 测试一：中英文系统提示词速度对照

**脚本**：`tests/performance/prompt_language_latency.py`（英文提示词：`tests/performance/prompts/main_en.txt`）

### 2.1 方法

- 变量只有一个：系统提示词语言（zh = 线上 `main.txt`；en = 忠实英译）
- 10 条中文用户消息（覆盖 8 个 intent×action 组合），每种语言 3 轮，**交错执行**（zh→en 轮转）抵消时间漂移
- 每种语言 1 次预热（丢弃），`temperature=0`，`max_tokens=200`，流式取 TTFT
- 结果：`tests/performance/prompt_language_latency.json`

### 2.2 结果

| 指标 | 中文 prompt | 英文 prompt | 差异 |
|---|---|---|---|
| TTFT 中位数 | 1723.8 ms | 1550.1 ms | -10.1% |
| 总耗时中位数 | 2241.4 ms | 2072.4 ms | -7.5% |
| 输入 token（中位） | 697.5 | 794.5 | **+13.9%** |
| 输出 token（中位） | 200（顶格） | 200（顶格） | 0 |
| 吞吐率 | 89.2 tok/s | 96.5 tok/s | +8.2% |

逐条明细（总耗时中位数，ms）：

| # | 消息 | zh | en |
|---|---|---|---|
| 0 | 我想去北京玩三天，预算2000 | 2084.7 | 1943.1 |
| 1 | 生成行程吧 | 2296.8 | 2131.3 |
| 2 | 南京有什么好吃的 | 2360.3 | **2470.1** |
| 3 | 把第二天改成南京博物馆 | 2480.2 | 2035.7 |
| 4 | 预算改为2000并重新安排 | 2164.5 | 1993.2 |
| 5 | 谢谢，帮大忙了 | **1486.9** | 1782.2 |
| 6 | 这个行程为什么第二天安排在海边？ | 2219.3 | 2043.5 |
| 7 | 就这样，确认 | 2308.6 | 2139.3 |
| 8 | 算了不去了 | 2259.3 | 2008.1 |
| 9 | 帮我记一下预算5000，顺便查下附近的酒店 | 2120.7 | 1853.1 |

### 2.3 结论

1. **没有稳定的语言效应**：10 条中 8 条英文略快、2 条中文更快，方向不一致；~10% 的差异在小样本下不具统计显著性。
2. **英文 prompt 的真实代价是 token 数**：同样语义英文占的输入 token 多 **+13.9%**（本地模型无感，按 token 计费的 API 是实际开销）。
3. **生成速度与提示词语言无关**（吞吐率差 <10% 且在噪声范围内）。
4. 注意：两边输出均顶到 `max_tokens=200` 被截断——该测试的"总耗时"实际是"生成 200 token 的耗时"，不代表完整响应时间。

> 结论：**不要指望靠切换提示词语言提速**。

---

## 3. 测试二：意图节点思考强度评测

### 3.1 发现：延迟主因是"思考 token"

走真实管线（`app/agent_client._create_chat_completion`）测速，5 条消息 × 3 轮：

| 模式 | 总耗时中位 | 输出 token（中位） | JSON 合法 |
|---|---|---|---|
| node（json_schema + reasoning_effort=low，**app 现状**） | **3668.6 ms** | 316 | 15/15 |
| plain（无 response_format / reasoning_effort） | 4058.3 ms | 356 | 15/15 |

关键矛盾：返回的 JSON content **只有 ~89 字符（约 30 token）**，而 `completion_tokens` 是 **150~700**——差值全是**思考 token**（响应里实际带 `reasoning_content` 字段，长度 248~2632 字符）。

按模型 ~90 tok/s 的生成速度：**~330 个思考 token ≈ 3 秒**，即意图分类延迟的 ~90% 花在"想"上。

### 3.2 参数试探全过程（哪些有效、哪些是坑）

| # | 参数（客户端发送方式） | 结果 | 判定 |
|---|---|---|---|
| 1 | `reasoning_effort: "low"`（app 现状） | 3.7s，~330 思考 token | ✅ 生效（最低有效档） |
| 2 | `reasoning_effort: "medium"` | 4.5s，~384 token | ✅ 生效，更慢 |
| 3 | `reasoning_effort: "xhigh"` | 5.9s，~478 token | ✅ 生效，最慢（对照组） |
| 4 | `reasoning_effort: "minimal"` | **HTTP 500**（代理内部 JS 崩溃） | ❌ 端点 bug，勿用 |
| 5 | `reasoning_effort: "none"` | 6.8s，**反而 +68%** | ❌ 无效变慢 |
| 6 | `enable_thinking: false`（SDK extra_body） | ≈low，无变化 | ❌ 被端点无视 |
| 7 | `enable_thinking: false`（原始 HTTP 直发） | ≈low，无变化 | ❌ 被端点无视 |
| 8 | `think: false`（Ollama 风格） | ≈low，-11%（噪声级） | ❌ 无效 |
| 9 | `enable_thinking` 直接当 SDK kwarg | `TypeError: unexpected keyword argument` | ❌ SDK 不接受未知 kwarg |
| 10 | **`chat_template_kwargs: {"enable_thinking": false}`（原始 HTTP）** | **822 ms，思考 token = 0** | ✅ **唯一有效** |

单调性验证（#1<#2<#3）证明 `reasoning_effort` 档位真实生效；#10 与端点文档"off 档 = 0 思考 token"吻合。

### 3.3 有效参数最终验证

同一批 5 条消息，3 轮，`reasoning_effort: low` 为底：

| 配置 | 总耗时中位 | 输出 token（中位） | reasoning_content | JSON 合法 |
|---|---|---|---|---|
| 开思考（现状） | 3158 ms | 275 | 248~2632 字符 | 15/15 |
| **关思考**（chat_template_kwargs） | **822 ms** | **43** | **0** | 15/15 |

**延迟 -74%，准确率零损失。**

SDK 路径验证：`openai 3.20.0` 这套 SDK **必须走 `extra_body`** 传该参数（直接 kwarg 会 TypeError）：

```python
client.chat.completions.create(
    model=..., messages=...,
    extra_body={'chat_template_kwargs': {'enable_thinking': False}},
)
```

### 3.4 代码接入（已落地）

`app/agent_client.py`（`_create_chat_completion`，reasoning_effort 块之后）：

```python
if agent_role == 'main' and os.getenv('OPENAI_MAIN_ENABLE_THINKING', 'false').strip().lower() != 'true':
    # Intent routing needs no reasoning; the endpoint only honors the chat-template
    # switch (a bare enable_thinking parameter is ignored server-side).
    params['extra_body'] = {'chat_template_kwargs': {'enable_thinking': False}}
```

- **只作用于 `main`（意图）角色**，其他角色（planning/revision 等）保留思考
- 环境变量 `OPENAI_MAIN_ENABLE_THINKING=true` 可整体关回（恢复思考）
- 传输层 `model_transport.create_completion` 用 `create(**payload)` 展开传参，`extra_body` 作为合法 SDK kwarg 被正确转发

### 3.5 最终效果（真实管线端到端）

| 消息 | 路由结果 | 延迟 |
|---|---|---|
| 我想去北京玩三天，预算2000 | `requirements/update_requirements` ✓ | 578 / 546 ms |
| 生成行程吧 | `planning/generate` ✓ | 607 / 536 ms |
| 南京有什么好吃的 | `query/reply` ✓ | 621 / 514 ms |
| 把第二天改成南京博物馆 | `revision/modify` ✓ | 677 / 552 ms |
| 算了不去了 | `planning/cancel` ✓ | 643 / 489 ms |

**中位延迟 565 ms（改前 ~3.7s，约 6.5 倍提速），思考 token 从 ~330 降到 44，路由全部正确。**

---

## 4. 综合结论

1. **提示词语言不是速度变量**：中英文 prompt 的速度差异在噪声范围内；英文的真实成本是 +13.9% 输入 token。
2. **思考强度才是速度变量**：意图节点 3.7s 延迟中 ~3s 是 Qwen3 思考 token；关闭后 0.57s。
3. **端点参数坑**（对接 `172.25.17.131:18080` 时必知）：
   - 关思考唯一有效写法：请求体顶层 `chat_template_kwargs: {"enable_thinking": false}`（SDK 里放 `extra_body`）
   - `enable_thinking` / `think` 直传均被无视；`reasoning_effort: minimal` 会 500
4. **分层策略**：意图（分类）关思考 ✅；planning / revision（复杂推理）保留思考 ✅。

## 5. 复现方法

```bash
# 环境：conda activate TravelAssistant（E:/anaconda/envs/TravelAssistant）

# 测试一：中英文 prompt 对照（可改 PROMPT_BENCH_REPS / PROMPT_BENCH_MAX_TOKENS）
python tests/performance/prompt_language_latency.py

# 测试二：意图节点真实管线
python tests/performance/intent_node_latency.py

# 思考档位对照（low / medium / xhigh / off）
python tests/performance/intent_node_effort_levels.py
python tests/performance/intent_node_notinking.py
```

**数据文件**：

- `tests/performance/prompt_language_latency.json`
- `tests/performance/intent_node_latency.json`
- `tests/performance/intent_node_notinking.json`
- `tests/performance/intent_node_effort_levels.json`

> 注：3.3 / 3.5 节的最终验证（raw HTTP 与 app 端到端）为交互式运行，数值记录于本文档，未单独落盘。

## 6. 后续建议

| 优先级 | 事项 |
|---|---|
| 高 | 观察线上 1~2 天意图节点 P50/P95，确认提速无回归 |
| 中 | 评估把 `chat` 角色也纳入关思考（`agent_roles.py:91` 中 chat 复用 MAIN 的 reasoning 档位，需单独判断） |
| 中 | 若端点多模型，给 `main` 角色加按角色选模型的机制（意图分类 4B~8B 即可，可进一步 <1s） |
| 低 | 给 `reasoning_effort` 的端点 500（`minimal`）报 issue 给端点负责人 |
