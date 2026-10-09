# 按 Agent 角色设置思考强度

历史的严格结构化响应探测只用于接口兼容性证据；当前行程链路直接保存文字，不发送行程 response_format，也没有结构化角色。

角色默认思考强度没有因文字行程改造变化，具体链路见 [当前实现](agent-role-implementation.md)。

## 默认值与配置

| 角色/模式 | 默认请求值 |
| --- | --- |
| main（聊天和委派） | low |
| requirements | low |
| research | low |
| planning | medium |
| revision（修改） | medium |
| revision / explain（只读解释） | low |

配置优先级：单角色环境变量 > 全局环境变量 > 上表代码默认值。环境变量留空表示未覆盖；显式写 `default` 表示不发送思考参数，恢复服务端默认行为。

```env
# 可选全局覆盖：全部使用服务端默认行为
OPENAI_REASONING_EFFORT=default

# 可选角色覆盖：优先于全局配置
OPENAI_REASONING_EFFORT_MAIN=low
OPENAI_REASONING_EFFORT_REQUIREMENTS=low
OPENAI_REASONING_EFFORT_RESEARCH=low
OPENAI_REASONING_EFFORT_PLANNING=medium
OPENAI_REASONING_EFFORT_REVISION=medium
```

上面展示配置语法，不是项目必须添加的设置。未配置这些变量时直接使用代码默认值。

显式设置 REVISION 或全局值时，也会覆盖解释模式的默认低档。应用允许 `default / none / minimal / low / medium / high / xhigh / max`，但实际支持集合由模型服务决定；`none` 不等于通用可用的关闭思考开关。非法配置值在发请求前报错，不静默改成别的值。

修改 `.env` 后需重启本地服务才能加载。新模型服务若暂未验证，可先配置 `OPENAI_REASONING_EFFORT=default`，再验证支持情况。

## 当前 Responses 模型配置

2026-10-08 本机切换为 `https://mirror.xinshu.ai/v1` 的 `gpt-5.6-sol`，使用 `OPENAI_WIRE_API=responses`、`OPENAI_STORE_RESPONSES=false`，并关闭备用模型。`/models` 列出目标模型，实际 `/responses` 调用返回对应模型且状态 completed。

Responses 将统一的角色档位映射到 `reasoning.effort`，工具定义映射为顶层 function，工具结果使用 function_call_output。每次请求发送 store=false，工具续轮在本地重放必要的 reasoning/function_call 条目，不传 previous_response_id；请求 include reasoning.encrypted_content 以兼容无服务端存储的续轮。加密推理状态不写入文字日志或业务数据。非 completed 输出视作失败，不保存部分正文。

协议适配位于 `app/model_transport.py`；`test_model_transport.py` 覆盖真实 SDK 的请求转换、工具续轮、加密条目保留、格式、存储开关、参数拒绝与部分响应保护。共享角色 Loop 和文字行程保存协议沿用现有实现。

此次回归310项通过。真实模型在独立数据中完成聊天、需求更新、查询、规划、修改五类验收，共11次Responses请求，覆盖五个角色、low/medium档位与4次工具续轮，无思考参数降级；每次捕获请求均为目标模型、store=false且无previous_response_id。生成与修改各新增一个文字版本，独立SQLite重启保留内容，业务数据库哈希未变化。查询工具采用受控未知结果，这证明接口与保存链路兼容，不代表整套旅行质量验收已通过。

本机原始结果：`C:\Users\16156\AppData\Local\Temp\travelassistant-gpt-switch-947a71114841491d867d11d982038264\smoke.json`。

## 历史 Kimi K3 服务的证据

核验日期：2026-10-05。实际端点为 `dashscope.aliyuncs.com/compatible-mode/v1`，模型为 `kimi-k3`。

官方参考：

- [阿里云百炼 Kimi API](https://help.aliyun.com/zh/model-studio/kimi-api)：说明 Kimi K3 为始终思考模型，并明确不支持 `thinking_budget`；此页未列出 `reasoning_effort`。
- [阿里云百炼深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)：介绍思考开关和 token 预算，并说明 Kimi K3 不支持 `thinking_budget`。

实际接口返回与页面描述存在差异，不能单凭文档或单次 HTTP 成功断定档位是否生效。本次使用合成短消息探测：

| 请求 | 观察结果 |
| --- | --- |
| 不设置思考参数 | 请求成功，返回 reasoning token 统计 |
| `reasoning_effort=low` | 请求成功，答案正常，返回 reasoning token 统计 |
| `reasoning_effort=unsupported_probe_value` | HTTP 400，明确列出 `none/minimal/low/medium/high/xhigh/max` 合法枚举 |
| `thinking_budget=64` | HTTP 400，明确说明不支持该参数 |
| `enable_thinking=false` | 此端点短请求成功，返回 reasoning_tokens=0；与页面“不可关闭”的描述不一致，本次不据此接入关闭开关 |

非法值被拒绝说明此端点实际识别并校验 `reasoning_effort`。随后真实集成检查通过：主 Agent 低档委派需求 Agent 低档，需求变更候选正确；规划中档能输出真实工具调用，收到工具结果后能返回严格结构化结果。均未触发思考参数降级。

这些证据验证参数兼容性、枚举校验和调用链，不能量化模型内部努力程度，也不能证明低档对所有任务都更快或质量等同。简单探测的 token 数与时延不作为强度效果的对照实验。

原始探测摘要在 `.cache/reasoning-check/probe-results.json`；集成结果在 `.cache/reasoning-check/live-integration.json`。文件只记录参数、答案、耗时和 token 计数，不记录思考正文，也不写入用户数据库。

## 兼容与日志

- 仅当 HTTP 400 明确指向 `reasoning_effort` 或 Responses 的 `reasoning.effort` 不受支持或档位无效，才移除该参数重试一次。
- 请求的消息、工具、输出格式保持原样。限流、认证失败、超时、服务错误或其他字段错误不会触发思考参数降级。
- 重试和格式兼容回退都消耗共享模型调用预算；剩余额度不足时停止。
- 在本次任务中按服务端点、协议、模型和档位记住明确拒绝，后续相同请求直接采用服务端默认。另一个模型、另一个档位或下一轮任务重新判断，防止主模型拒绝影响备用模型。
- 每次实际模型请求记录角色、请求档位、实际发送档位以及默认/降级状态。成功仅标记 `parameter_accepted`，不会伪称已验证内部强度。
- 响应只增加耗时、prompt/completion/reasoning token 计数；服务未提供时记为 null，不补造为零。不新增 `reasoning_content` 正文日志。

## 实现位置与验证

- `app/agent_roles.py`：角色默认值、环境变量解析与优先级。
- `app/agent_runtime.py`：把角色/模式对应的值传到每次实际请求，维护本轮拒绝记录。
- `app/agent_client.py`：发送参数、窄范围降级与重试、请求/响应计数日志。
- `.env.example`：可选配置示例。
- `test_reasoning.py`：真实 OpenAI SDK 的 MockTransport 请求检查，覆盖默认值、覆盖优先级、原始请求参数、错误分类、共享预算、备用模型隔离、格式回退与不记录思考正文。
- `tests/live_reasoning_case.py`：可手动运行的真实接口验收，不导入业务 Store，不修改业务数据库。

最终 Python 回归为 **267 passed in 17.52s**；compileall 与 git diff --check 通过。真实低/中档接口和工具往返检查通过，业务数据库哈希未变化。此改动不改变 UI、子 Agent 权限或业务保存协议。

改造前涉及文件保存在 `.cache/versions/before-reasoning-20261005-133604/`。

总结：当前按角色请求低/中思考强度，提供全局和单角色覆盖；服务明确拒绝时回到默认，并如实记录参数与统计，不承诺未测量的提速或内部推理效果。
