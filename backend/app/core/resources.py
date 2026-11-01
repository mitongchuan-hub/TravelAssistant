"""集中管理进程级模型客户端：启动时创建，请求借用，退出时排空关闭。

Bootstrap enters client_scope; requests only borrow. No environment loading,
client construction, or application/store import happens at module import time.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from threading import Condition
from typing import Any, Callable, Iterator

from app.core import config


class ResourceUnavailable(RuntimeError):
    """The shared model client is disabled, not started, or shutting down."""


@dataclass
class _Owner:
    client: Any = None
    state: str = 'starting'
    borrowers: int = 0


_condition = Condition()
_owner: _Owner | None = None


# 启动入口持有唯一所有权；退出前等待已借出的请求完成。
@contextmanager
def client_scope(
    *,
    timeout_seconds: float | None = None,
    client_factory: Callable[..., Any] | None = None,
) -> Iterator[None]:
    """Own one client until all request leases drain, then close it exactly once.

    Concurrent/nested scopes are rejected, including while startup or shutdown
    is running. An explicit factory permits offline fakes under pytest, but
    still requires explicit connection settings and respects model disablement.
    The default OpenAI factory is never used under pytest.
    """
    global _owner
    owner = _Owner()
    with _condition:
        if _owner is not None:
            raise ResourceUnavailable('A model client scope is already active')
        _owner = owner  # Reserve before construction; startup may be slow.

    try:
        disabled = os.getenv('TRAVEL_AGENT_DISABLE_LLM', '').strip().lower() in {'1', 'true', 'yes', 'on'}
        if not disabled:
            config.required_string('OPENAI_API_KEY')
            config.model_base_url()
            if timeout_seconds is None:
                config.model_timeout()
            elif not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise config.ConfigurationError('timeout_seconds: 必须为有限正数秒数')
        enabled = config.llm_enabled() if client_factory is None else config._llm_configured()
        if enabled:
            # Capture client settings once per owner, not once per request.
            settings = {
                'api_key': config.required_string('OPENAI_API_KEY'),
                'base_url': config.model_base_url(),
                'timeout': config.model_timeout() if timeout_seconds is None else timeout_seconds,
                'max_retries': 0,
            }
            if client_factory is None:
                from openai import OpenAI
                client_factory = OpenAI
            owner.client = client_factory(**settings)
        with _condition:
            owner.state = 'ready'
        yield None
    finally:
        try:
            with _condition:
                owner.state = 'closing'
                _condition.notify_all()
                while owner.borrowers:
                    _condition.wait()
            if owner.client is not None:
                owner.client.close()
        finally:
            with _condition:
                _owner = None
                owner.state = 'closed'
                _condition.notify_all()


# 请求只持有借用租约，不创建客户端，也不负责关闭共享资源。
@contextmanager
def borrow_client() -> Iterator[Any]:
    """Lease the initialized client without owning or closing it.

    Admission and release are synchronized; requests and client close never run
    under the lifecycle lock. Missing/disabled owners do not create a client.
    """
    with _condition:
        owner = _owner
        if owner is None or owner.state != 'ready' or owner.client is None:
            raise ResourceUnavailable('The model client is unavailable')
        owner.borrowers += 1
    try:
        yield owner.client
    finally:
        with _condition:
            owner.borrowers -= 1
            if not owner.borrowers:
                _condition.notify_all()
