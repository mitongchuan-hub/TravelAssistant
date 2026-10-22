# 模型请求超时：目录迁移排查记录

状态：用户修复服务器后，真实主 Agent 恢复响应。恢复探测 S02、P01 均正常结束，记录为 `.cache/scope_eval/recovered-probe-e6a8939e.json`；随后启动全量 50 条评测。服务端具体故障原因和修复操作由用户处理，本地未修改调用代码。

## 本地检查

- `app/agents/agent_client.py` 的 `Path(__file__).resolve().parents[2]` 正确指向项目根目录，加载项目 `.env`。
- 41 个 Python 模块均可实际导入，所有本地延迟导入目标及符号可解析。
- `app/agents/model_transport.py` 与 Git HEAD 的迁移前版本相比，函数 AST 未变化。
- 模型配置为 `qwen3.8:27b`，`http://172.25.17.131:18080/v1`，协议 `chat_completions`。
- `git diff` 中 `_create_chat_completion` 除导入路径外有关闭主控 thinking 的设置；独立最小请求、原生接口请求均失败，不能据此认定该设置或目录迁移导致故障。

## 项目外独立验证

独立 Python 进程只导入 dotenv、OpenAI SDK（不导入 app），使用同一配置：模型列表正常返回 `qwen3.8:27b`，最小 Hello 请求、最多 16 token，20 秒后 APITimeoutError，底层 ReadTimeout。

服务响应头标识为 llama.cpp。`/health` 返回 200、`{"status":"ok"}`；这不代表实际推理正常。原生 `/completion` 对空闲槽位 0 仅请求 4 token，15 秒后同样 ReadTimeout，排除了仅 OpenAI 兼容适配层失败这一解释。

两次槽位快照中的计数完全一致：

| 槽位 | 任务 | 处理中 | prompt tokens | 已处理 | 缓存 |
| --- | ---: | --- | ---: | ---: | ---: |
| 0 | 18770 | false | 8825 | 0 | 0 |
| 1 | 20331 | true | 40981 | 2461 | 38321 |

槽位 1 未观察到处理计数变化。槽位 0 空闲但新原生请求仍未产生结果。该现象提示后端推理/任务调度停滞；不能仅凭 HTTP 状态证明 GPU、推理线程或任务队列正常，也不能确认任务 20331 的来源或是否由本项目触发。

## 下一步

模型地址不是当前 Windows 机器的本地网卡地址，当前机器未发现 llama/ollama/LM Studio 进程，现有工具没有远程管理连接。已向用户请求启动脚本位置或已有远程连接方式。

获取后检查 llama.cpp 服务日志、任务 20331、GPU/内存状态和实际启动参数；根据证据修复或重启服务。恢复后先验证最小 API 请求，再验证真实主运行时，最后继续 50 条测试。当前不盲改项目模型地址、协议或业务代码，不以 import 检查通过宣称推理已修复。
