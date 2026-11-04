"""Bounded AMap MCP client and adapters for existing synchronous travel tools."""
from contextlib import contextmanager, asynccontextmanager
from contextvars import ContextVar
from datetime import timedelta
from time import monotonic
import json
import logging
import os
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 环境配置由应用/脚本入口统一加载；地图适配器导入时不读取 .env。

import anyio
from anyio.from_thread import start_blocking_portal
import httpx
from jsonschema import validate
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.config import amap_api_key, amap_timeout, amap_url

ALLOWED_TOOLS = frozenset({'maps_text_search', 'maps_around_search', 'maps_search_detail', 'maps_direction_walking',
                          'maps_direction_driving', 'maps_direction_transit_integrated'})
_budget_context = ContextVar('amap_mcp_budget', default=None)
_phase_context = ContextVar('amap_mcp_phase', default=None)
_phase_observer = ContextVar('amap_mcp_phase_observer', default=None)
logger = logging.getLogger(__name__)


class AmapMCPError(RuntimeError):
    """Contains safe diagnostics only; SDK exceptions/URLs may contain the key."""

    def __init__(self, message, *, stage=None, tool=None, error_type=None):
        super().__init__(message)
        current = _phase_context.get() or {}
        self.stage = stage or current.get('stage', 'connection')
        self.tool = tool or current.get('tool')
        self.error_type = error_type


@contextmanager
def observe_phases(callback):
    """Optional diagnostics sink; callbacks receive no URLs, credentials or payloads."""
    token = _phase_observer.set(callback)
    try:
        yield
    finally:
        _phase_observer.reset(token)


@contextmanager
def phase(stage, tool=None):
    progress = _phase_context.get()
    if progress is not None:
        progress.update(stage=stage, tool=tool)
    started = monotonic()
    row = {'stage': stage, 'tool': tool, 'status': 'completed'}
    try:
        yield
    except BaseException as exc:
        row.update(status='failed', error_type=type(exc).__name__)
        raise
    finally:
        row['seconds'] = monotonic() - started
        observer = _phase_observer.get()
        if observer:
            try:
                observer(dict(row))
            except Exception as exc:
                logger.warning('AMap MCP diagnostics callback failed: %s', type(exc).__name__)
        if row['status'] == 'failed':
            logger.warning('AMap MCP failed: stage=%s tool=%s error_type=%s', stage, tool, row['error_type'])


def api_key():
    try:
        return amap_api_key()
    except ValueError:
        # Missing/invalid map credentials keep map tools unavailable, never live.
        return ''


def configured():
    return bool(api_key())


def timeout_seconds():
    try:
        value = amap_timeout()
    except ValueError:
        raise AmapMCPError('高德MCP超时配置无效') from None
    scope = _budget_context.get()
    return min(value, scope['budget'].remaining()) if scope else value


def endpoint():
    try:
        parts = urlsplit(amap_url())
    except ValueError:
        raise AmapMCPError('高德MCP地址须为HTTPS且不含账号或片段') from None
    # Credentials have one configuration source and never enter tool arguments.
    query = [(k, v) for k, v in parse_qsl(parts.query) if k.lower() != 'key']
    query.append(('key', api_key()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ''))


class RedactMCPLogs(logging.Filter):
    _travel_amap_redactor = True

    def filter(self, record):
        import re
        import traceback
        def scrub(text):
            for name in ('AMAP_MAPS_API_KEY', 'AMAP_MAP_KEY'):
                secret = os.getenv(name, '').strip()
                if secret:
                    text = text.replace(secret, '[redacted]')
            return re.sub(r'([?&]key=)[^&\s\"\'<>]+', r'\1[redacted]', text)
        record.msg, record.args = scrub(record.getMessage()), ()
        if record.exc_info:
            record.exc_text = scrub(''.join(traceback.format_exception(*record.exc_info)))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = scrub(record.exc_text)
        return True


_redactor = RedactMCPLogs()

def redact_transport_logs():
    for name in ('httpx', 'httpcore', 'mcp.client.streamable_http', 'mcp.shared.session', 'mcp.client.session'):
        logger = logging.getLogger(name)
        if not any(getattr(item, '_travel_amap_redactor', False) for item in logger.filters):
            logger.addFilter(_redactor)


@contextmanager
def budget_scope(budget):
    """The existing tool dispatch counts its first external call; details count too."""
    token = _budget_context.set({'budget': budget, 'calls': 0} if budget else None)
    try:
        yield
    finally:
        _budget_context.reset(token)


def remaining_budget():
    """Check shared cancellation/deadline without consuming another external call."""
    scope = _budget_context.get()
    return scope['budget'].remaining() if scope else float('inf')


def consume_call():
    scope = _budget_context.get()
    if scope:
        scope['budget'].remaining()
        if scope['calls']:
            scope['budget'].external()
        scope['calls'] += 1


def result_object(result):
    if result.isError:
        raise AmapMCPError('高德MCP工具返回错误，信息待核实')
    if isinstance(result.structuredContent, dict):
        value = result.structuredContent
    else:
        blocks = [block.text for block in result.content if getattr(block, 'type', None) == 'text']
        if len(blocks) != 1:
            raise AmapMCPError('高德MCP返回格式无效')
        try:
            value = json.loads(blocks[0])
        except (TypeError, ValueError):
            raise AmapMCPError('高德MCP未返回JSON对象') from None
    if not isinstance(value, dict):
        raise AmapMCPError('高德MCP未返回JSON对象')
    if str(value.get('status', '1')) in {'0', 'false', 'False'} or value.get('error'):
        raise AmapMCPError('高德MCP权限、配额或查询暂不可用')
    return value


@asynccontextmanager
async def _connection(url, seconds):
    async with httpx.AsyncClient(timeout=httpx.Timeout(seconds), follow_redirects=False) as http:
        async with streamable_http_client(url, http_client=http) as (read, write, _):
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=seconds)) as client:
                with phase('initialize'):
                    await client.initialize()
                specs = {}
                cursor = None
                for _ in range(8):
                    with phase('discovery'):
                        response = await client.list_tools(cursor=cursor)
                    specs.update({tool.name: tool.inputSchema for tool in response.tools if tool.name in ALLOWED_TOOLS})
                    cursor = response.nextCursor
                    if not cursor:
                        break
                else:
                    raise AmapMCPError('高德MCP工具清单分页超出上限')
                yield client, specs


class MCPTools:
    def __init__(self, portal, client, specs, deadline):
        self.portal, self.client, self.specs, self.deadline = portal, client, specs, deadline
        self.calls = 0

    def call(self, name, arguments):
        from time import monotonic
        if name not in ALLOWED_TOOLS or name not in self.specs:
            raise AmapMCPError('高德MCP缺少所需只读工具')
        try:
            validate(arguments, self.specs[name])
        except Exception:
            raise AmapMCPError('高德MCP工具参数不匹配') from None
        if self.calls >= 6:
            raise AmapMCPError('本次高德MCP查询次数已达上限')
        remaining = min(timeout_seconds(), self.deadline-monotonic())
        if remaining <= 0:
            raise AmapMCPError('本次高德MCP查询超时，信息待核实')
        consume_call()
        self.calls += 1
        async def request():
            with anyio.fail_after(remaining):
                return await self.client.call_tool(name, arguments)
        with phase('tool', name):
            try:
                return result_object(self.portal.call(request))
            except AmapMCPError:
                raise
            except Exception as exc:
                raise AmapMCPError('高德MCP请求失败或超时，信息待核实', error_type=type(exc).__name__) from None


@contextmanager
def session():
    if not configured():
        raise AmapMCPError('未配置高德MCP Key')
    redact_transport_logs()
    from time import monotonic
    seconds = timeout_seconds()
    deadline = monotonic()+seconds
    async def connection():
        # Initialization, discovery and requests share the per-tool wall budget.
        with anyio.fail_after(seconds):
            async with _connection(endpoint(), seconds) as connected:
                yield connected
    token = _phase_context.set({'stage': 'connection', 'tool': None})
    try:
        with start_blocking_portal() as portal:
            with portal.wrap_async_context_manager(asynccontextmanager(connection)()) as (client, specs):
                yield MCPTools(portal, client, specs, deadline)
    except AmapMCPError:
        raise
    except Exception as exc:
        # SDK task groups may wrap an exception raised by the synchronous caller.
        # Business cancellation/budget errors must not turn into provider fallback.
        from app.agents.budget import AgentBudgetError
        def budget_error(error):
            if isinstance(error, AgentBudgetError):
                return error
            for child in getattr(error, 'exceptions', ()):
                found = budget_error(child)
                if found is not None:
                    return found
            return None
        found = budget_error(exc)
        if found is not None:
            raise found from None
        raise AmapMCPError('高德MCP连接失败或超时，信息待核实', error_type=type(exc).__name__) from None
    finally:
        _phase_context.reset(token)


def search_pois(city, keywords, limit=5):
    """MCP search, then at most five distinct REST attempts outside that session."""
    from app.services.maps import amap_rest
    limit = max(1, min(int(limit), 5))
    deadline = monotonic() + timeout_seconds()
    with session() as tools:
        params = {'keywords': keywords, 'citylimit': bool(city)}
        if city:
            params['city'] = city
        payload = tools.call('maps_text_search', params)
    pois = payload.get('pois')
    if not isinstance(pois, list):
        raise AmapMCPError('高德MCP地点列表无效')
    candidates, seen = [], set()
    for poi in pois:
        remaining_budget()
        if len(seen) >= limit or monotonic() >= deadline:
            break
        if not isinstance(poi, dict) or not isinstance(poi.get('id'), str) or not poi['id'].strip() or poi['id'] in seen:
            continue
        seen.add(poi['id'])  # Failures count too; never fan out to fill successes.
        try:
            detail = amap_rest.detail(poi['id'], deadline=deadline)
        except amap_rest.AmapRESTError:
            continue
        if detail.get('id') == poi['id']:
            # Search summaries cannot supply coordinates or missing detail fields.
            candidates.append(detail)
    remaining_budget()
    return {'pois': candidates, 'total_matched': len(pois), 'transport': 'amap_rest'}


def call(name, arguments):
    """One native tool call; results retain the upstream structure."""
    try:
        with session() as tools:
            return tools.call(name, arguments)
    except AmapMCPError as exc:
        return {'status': 'unavailable', 'reason': str(exc)}
