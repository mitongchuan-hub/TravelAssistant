# 会话轨迹、进度通知和运行记录持久化。
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from app.core.config import database_path

logger = logging.getLogger(__name__)


# 收集单次会话的可展示事件和持久化记录，并负责最终落库。
@dataclass
class ConversationTrace:
    # 当前任务的稳定请求标识，用于把进度、模型调用和运行记录关联起来。
    request_id: str
    # 本轮执行的开始时间，用于计算整轮耗时并写入运行记录。
    started_at: str
    # 当前任务所属旅行的标识，用于保存运行记录和校验数据归属。
    trip_id: str
    # 用户本轮发送的原始消息，用于审计上下文和故障排查。
    user_body: str
    # 按发生顺序保存模型、工具和任务生命周期事件；不直接暴露给前端。
    records: list[dict] = field(default_factory=list, repr=False)
    # 标记运行记录是否已经落库，避免正常路径和异常路径重复写入。
    written: bool = field(default=False, repr=False)
    # 可选的实时进度回调；有值时把新增事件转发给任务进度持久化层。
    on_progress: Callable[[str, object], None] | None = field(default=None, repr=False)

    # 保存结构化事件，并尽力通知实时进度回调。
    def add(self, title: str, value: object) -> None:
        self.records.append({"title": title, "value": value})
        if self.on_progress:
            try:
                self.on_progress(title, value)
            except Exception as exc:
                logger.warning('Could not update execution progress (%s)', type(exc).__name__)

    # 确保执行轨迹最多写入一次，并将落库失败限制为日志告警。
    def write(self, error: BaseException | None = None) -> None:
        if error is not None:
            self.add("调用异常", f"{type(error).__name__}: {error}")
        if self.written:
            return
        self.written = True
        try:
            record_agent_run(self, error=error)
        except Exception as exc:
            logger.warning("Could not record agent run (%s)", type(exc).__name__)


# 将轨迹整理为数据库行并写入配置的城市助手数据库。
def record_agent_run(trace: ConversationTrace, *, error: BaseException | None = None) -> None:
    from app.storage.persistence import insert_agent_run
    insert_agent_run(str(database_path()), _agent_run_row(trace, error))


# 从轨迹中提取最终输出、实际模型、用量和工具次数，生成持久化字段。
def _agent_run_row(trace: ConversationTrace, error: BaseException | None) -> dict:
    records = trace.records
    # 当前传输层使用“模型响应”记录正文和工具调用。
    output = None
    for record in reversed(records):
        if record['title'] != '模型响应' or not isinstance(record['value'], dict):
            continue
        response = record['value']
        if isinstance(response.get('content'), str) and response['content'].strip():
            output = response['content']
        elif response.get('tool_calls'):
            output = json.dumps(response['tool_calls'], ensure_ascii=False)
        break
    # 模型字段随请求和备用模型切换记录更新，以反映最终使用者。
    model = None
    for record in records:
        value = record["value"]
        if not isinstance(value, dict):
            continue
        if record["title"] == "模型请求" and value.get("model"):
            model = value["model"]
        elif record["title"] == "切换备用模型" and value.get("to"):
            model = value["to"]
    # 聚合多次模型响应的输入和输出令牌统计。
    tokens_in = 0
    tokens_out = 0
    for record in records:
        if record["title"] != "模型响应":
            continue
        value = record["value"]
        usage = value.get("usage") if isinstance(value, dict) else None
        if not isinstance(usage, dict):
            continue
        if isinstance(usage.get("prompt_tokens"), int):
            tokens_in += usage["prompt_tokens"]
        if isinstance(usage.get("completion_tokens"), int):
            tokens_out += usage["completion_tokens"]
    # 执行器在结束时记录整轮预算，不再依赖旧工作流事件名称。
    tool_calls = 0
    for record in reversed(records):
        if record['title'] == 'Task Runtime预算' and isinstance(record['value'], dict):
            tool_calls = int(record['value'].get('tool_calls') or 0)
            break
    process = [
        {"title": record["title"], "value": record["value"]}
        for record in records
        if record["title"] != "输入上下文"
    ]
    return {
        "request_id": trace.request_id,
        "trip_id": trace.trip_id or None,
        "user_input": trace.user_body,
        "process": process,
        "output": output,
        "ok": error is None,
        "error": f"{type(error).__name__}: {error}" if error is not None else None,
        "model": model,
        "tokens_in": tokens_in or None,
        "tokens_out": tokens_out or None,
        "tool_calls": tool_calls,
        "duration_ms": int((datetime.now() - datetime.fromisoformat(trace.started_at)).total_seconds() * 1000),
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
