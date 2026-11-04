"""Shared request budgets for business execution and standalone display conversion."""
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from time import monotonic


def _locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return guarded


# 表示本轮时间、调用次数或令牌额度已无法继续安全执行。
class AgentBudgetError(RuntimeError):
    pass


# 集中维护整轮调用、时间、取消和令牌预算，不依赖业务执行器。
@dataclass
class TurnBudget:
    max_models: int | None = 16
    max_tools: int | None = 64
    max_external: int | None = 32
    seconds: float = 360
    cancelled: object = None
    model_calls: int = 0
    tool_calls: int = 0
    external_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    total_token_limit: int | None = None

    def __deepcopy__(self, memo):
        from copy import deepcopy
        clone = type(self).__new__(type(self))
        memo[id(self)] = clone
        for key, value in self.__dict__.items():
            setattr(clone, key, RLock() if key == '_lock' else deepcopy(value, memo))
        return clone

    # 根据本轮时限建立单调时钟截止点。
    def __post_init__(self):
        self._lock = RLock()  # Execution resource, not part of the serializable budget data.
        self.deadline = monotonic() + self.seconds

    # 回调只表明本轮已停止，不能据此推断是手动终止还是新消息替代。
    def check_current(self):
        if self.cancelled and self.cancelled():
            raise AgentBudgetError('本次处理已停止。')

    # 返回剩余秒数，并在取消或超时后立即中止。
    def remaining(self):
        self.check_current()
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise AgentBudgetError('本次任务已达到时间上限，原行程已保留。')
        return remaining

    # 汇总已记录的输入与输出令牌。
    @property
    def total_tokens(self):
        return self.input_tokens + self.output_tokens

    # 预检拟执行工作及预留收尾资源是否仍在总预算内。
    @_locked
    def reserve(self, *, models=0, tools=0, external=0, seconds=0.0):
        """Check that work plus a caller-declared finishing reserve can still fit."""
        remaining = self.remaining()
        if self.max_models is not None and self.model_calls + models > self.max_models:
            raise AgentBudgetError('本次模型调用次数已达上限，原行程已保留。')
        if self.max_tools is not None and self.tool_calls + tools > self.max_tools:
            raise AgentBudgetError('本次工具调用次数已达上限，未执行超额工具。')
        if self.max_external is not None and self.external_calls + external > self.max_external:
            raise AgentBudgetError('本次外部查询次数已达上限，剩余事实待核实。')
        if remaining <= seconds:
            raise AgentBudgetError('本次任务剩余时间不足以安全执行并收尾。')

    # 在模型请求开始前占用一次调用额度。
    @_locked
    def model(self):
        self.reserve(models=1)
        if self.total_token_limit is not None and self.total_tokens >= self.total_token_limit:
            raise AgentBudgetError('本次模型令牌预算已用尽，未开始新的模型调用。')
        self.model_calls += 1

    # 累加服务端返回的令牌用量，超限时丢弃当前结果。
    @_locked
    def record_usage(self, input_tokens=None, output_tokens=None):
        for value, field_name in ((input_tokens, 'input_tokens'), (output_tokens, 'output_tokens')):
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                setattr(self, field_name, getattr(self, field_name) + value)
        if self.total_token_limit is not None and self.total_tokens > self.total_token_limit:
            raise AgentBudgetError('本次模型令牌预算已超出，丢弃当前结果并安全停止。')

    # 导出本轮预算消耗摘要。
    @_locked
    def usage(self):
        return {'model_calls': self.model_calls, 'tool_calls': self.tool_calls,
                'external_calls': self.external_calls, 'input_tokens': self.input_tokens,
                'output_tokens': self.output_tokens, 'total_tokens': self.total_tokens}

    # 校验并累计工具调用次数。
    @_locked
    def tools(self, count):
        if type(count) is not int or count < 0:
            raise AgentBudgetError('工具调用计数无效。')
        self.reserve(tools=count)
        self.tool_calls += count

    # 在实际外部访问前占用一次外部查询额度。
    @_locked
    def external(self):
        self.reserve(external=1)
        self.external_calls += 1


