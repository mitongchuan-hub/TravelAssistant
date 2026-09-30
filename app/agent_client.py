from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import AgentAction, AgentCard, IdeaCard, Message, PlanVersion, Trip, TripPlan
from .prompts import BASE_SYSTEM_PROMPT, FINALIZE_TRIP_PLAN_PROMPT, GATHER_TRIP_INFORMATION_PROMPT
from .tools import run_tool, tool_specs
from .response_schema import TRAVEL_RESULT_RESPONSE_FORMAT


APP_DIR = Path(__file__).resolve().parents[1]
load_dotenv(APP_DIR / ".env", override=False)
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentReply:
    body: str
    card: AgentCard
    idea_cards: list[dict]
    plan: dict | None = None


VALID_CARD_KINDS = {"requirement-summary", "missing-info", "conflict", "itinerary-draft", "revision"}
VALID_IDEA_KINDS = {"地点", "预算", "节奏", "禁忌", "待归类"}


MAX_TOOL_ROUNDS = 1


SYSTEM_PROMPT = BASE_SYSTEM_PROMPT


def is_llm_enabled() -> bool:
    disabled = os.getenv("TRAVEL_AGENT_DISABLE_LLM", "").strip().lower()
    if disabled in {"1", "true", "yes", "on"} or os.getenv("PYTEST_CURRENT_TEST"):
        return False
    return bool(os.getenv("OPENAI_API_KEY"))


def generate_agent_reply(
    trip: Trip,
    messages: list[Message],
    user_body: str,
    idea_cards: list[IdeaCard] | None = None,
    plan: TripPlan | None = None,
    plan_versions: list[PlanVersion] | None = None,
    metrics: dict[str, int | str] | None = None,
    planning_mode: bool = False,
) -> AgentReply | None:
    if not is_llm_enabled():
        return None

    try:
        from openai import BadRequestError, OpenAI

        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=float(os.getenv("OPENAI_TIMEOUT_SECONDS", "180.0")),
            max_retries=0,
        )
        model = os.getenv("OPENAI_MODEL") or os.getenv("MODEL_ID") or "qwen-plus"
        context = _build_context(
            trip,
            messages,
            user_body,
            idea_cards=idea_cards,
            plan=plan,
            plan_versions=plan_versions,
            metrics=metrics,
        )

        try:
            response = _request_model(client, model, context, planning_mode)
        except Exception as exc:
            fallback_model = os.getenv("OPENAI_FALLBACK_MODEL", "qwen-plus").strip()
            if not _is_rate_limit_error(exc) or not fallback_model or fallback_model == model:
                raise
            logger.warning("Primary model was rate limited; trying fallback model")
            response = _request_model(client, fallback_model, context, planning_mode)

        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("Empty model response")
        try:
            return _parse_reply(content)
        except json.JSONDecodeError:
            # Plain text is usable; broken structured output is not a successful reply.
            if content.startswith(("{", "[", "```")):
                raise
            return AgentReply(
                body=content,
                card=AgentCard(
                    kind="requirement-summary",
                    title="已收到",
                    summary=content[:80] or "我会继续结合你的需求整理行程。",
                    bullets=["回复已直接显示在聊天里", "可以继续补充预算、时间和偏好"],
                    actions=[AgentAction(label="查看旅行想法", target="board")],
                ),
                idea_cards=[],
                plan=None,
            )
    except Exception as exc:
        # Do not log provider responses, credentials, or conversation contents.
        status_code = getattr(exc, "status_code", None)
        logger.warning("Agent reply failed (%s, status=%s)", type(exc).__name__, status_code)
        if type(exc).__name__ == "RateLimitError" or status_code == 429:
            return AgentReply(
                body="模型服务当前比较繁忙，你的消息已保留，请稍后再试。",
                card=AgentCard(
                    kind="missing-info",
                    title="模型服务暂时限流",
                    summary="上游模型当前请求过多，不是你的 API Key 配置错误。",
                    bullets=["消息已经保留", "稍后可以重新生成", "也可以先继续补充饮食和穿衣偏好"],
                    actions=[AgentAction(label="稍后重试", target="chat")],
                ),
                idea_cards=[],
                plan=None,
            )
        if "timeout" in type(exc).__name__.lower():
            return AgentReply(
                body="模型服务响应超时，你的消息已保留，请稍后重试。",
                card=AgentCard(
                    kind="missing-info",
                    title="模型响应超时",
                    summary="请求超过等待时间，消息没有丢失。",
                    bullets=["消息已经保留", "稍后可以重新生成", "复杂行程可能需要更长时间"],
                    actions=[AgentAction(label="稍后重试", target="chat")],
                ),
                idea_cards=[],
                plan=None,
            )
        return AgentReply(
            body="这次模型回复没有完成，你的消息已保留，请稍后重试。",
            card=AgentCard(
                kind="missing-info",
                title="模型调用暂时失败",
                summary="消息已保留，可以检查 API Key、Base URL 和模型名后再试。",
                bullets=["消息已经保留", "请检查模型服务配置", "稍后可以直接重新发送"],
                actions=[AgentAction(label="查看旅行想法", target="board")],
            ),
            idea_cards=[],
            plan=None,
        )


def _request_model(client, model: str, context: str, planning_mode: bool) -> object:
    if planning_mode:
        tool_messages = []
        try:
            tool_messages = _collect_tool_messages(client, model, context)
        except Exception as exc:
            # Some OpenAI-compatible providers expose chat completions but not tools.
            # The final structured call can still produce a useful plan without weather.
            if _looks_like_unsupported_tools(exc) and type(exc).__name__ == "BadRequestError":
                logger.info("Model does not support tool calls; continuing without tools")
            else:
                raise
        return _create_chat_completion(
            client,
            model,
            [
                {"role": "system", "content": f"{BASE_SYSTEM_PROMPT}\n\n{FINALIZE_TRIP_PLAN_PROMPT}"},
                {"role": "user", "content": context},
                *tool_messages,
            ],
            response_format=TRAVEL_RESULT_RESPONSE_FORMAT,
        )
    return _create_chat_completion(
        client,
        model,
        [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ],
    )


def _is_rate_limit_error(exc: Exception) -> bool:
    return type(exc).__name__ == "RateLimitError" or getattr(exc, "status_code", None) == 429


def _collect_tool_messages(client, model: str, context: str) -> list[dict]:
    messages: list[dict] = [
        {"role": "system", "content": f"{BASE_SYSTEM_PROMPT}\n\n{GATHER_TRIP_INFORMATION_PROMPT}"},
        {"role": "user", "content": context},
    ]
    tool_messages: list[dict] = []
    for _ in range(MAX_TOOL_ROUNDS):
        response = _create_chat_completion(client, model, messages, tools=tool_specs())
        assistant = response.choices[0].message
        calls = list(getattr(assistant, "tool_calls", None) or [])
        if not calls:
            break
        assistant_message = {"role": "assistant", "content": getattr(assistant, "content", None) or ""}
        assistant_message["tool_calls"] = []
        for call in calls:
            function = getattr(call, "function", None)
            assistant_message["tool_calls"].append({
                "id": getattr(call, "id", "tool-call"),
                "type": "function",
                "function": {
                    "name": getattr(function, "name", ""),
                    "arguments": getattr(function, "arguments", "{}") or "{}",
                },
            })
        messages.append(assistant_message)
        tool_messages.append(assistant_message)
        for call in calls:
            function = getattr(call, "function", None)
            name = getattr(function, "name", "")
            raw_arguments = getattr(function, "arguments", "{}") or "{}"
            try:
                arguments = json.loads(raw_arguments)
                if not isinstance(arguments, dict):
                    raise ValueError("tool arguments must be an object")
                result = run_tool(name, arguments)
            except (TypeError, ValueError, json.JSONDecodeError):
                result = {"status": "unavailable", "reason": "工具参数无法解析，结果待确认。"}
            tool_message = {
                "role": "tool",
                "tool_call_id": getattr(call, "id", "tool-call"),
                "name": name,
                "content": json.dumps(result, ensure_ascii=False),
            }
            messages.append(tool_message)
            tool_messages.append(tool_message)
    return tool_messages


def _looks_like_unsupported_tools(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(word in text for word in ("tool", "function", "unsupported", "not support"))


def _create_chat_completion(
    client,
    model: str,
    messages: list[dict],
    *,
    tools: list[dict] | None = None,
    response_format: dict | None = None,
) -> object:
    from openai import BadRequestError

    params: dict[str, object] = {"model": model, "temperature": 0.4, "messages": messages}
    if tools:
        params["tools"] = tools
    if response_format:
        params["response_format"] = response_format

    while True:
        try:
            return client.chat.completions.create(**params)
        except BadRequestError as exc:
            text = str(exc).lower()
            if "temperature" in params and "temperature" in text and "not supported" in text:
                params.pop("temperature", None)
                continue
            if "response_format" in params and _looks_like_unsupported_response_format(text):
                if isinstance(params["response_format"], dict) and params["response_format"].get("type") == "json_schema":
                    params["response_format"] = {"type": "json_object"}
                else:
                    params.pop("response_format", None)
                continue
            if "tools" in params and _looks_like_unsupported_tools(exc):
                raise
            raise


def _looks_like_unsupported_response_format(text: str) -> bool:
    return (
        "response_format" in text
        or "json_schema" in text
        or "structured output" in text
        or ("json" in text and ("not supported" in text or "unsupported" in text))
    )


def _build_context(
    trip: Trip,
    messages: list[Message],
    user_body: str,
    idea_cards: list[IdeaCard] | None = None,
    plan: TripPlan | None = None,
    plan_versions: list[PlanVersion] | None = None,
    metrics: dict[str, int | str] | None = None,
) -> str:
    recent_messages = [message for message in messages if message.status == "sent"][-20:]
    chat_lines = [
        f"{'用户' if message.sender_type == 'user' else 'Agent'}: {message.body}"
        for message in recent_messages
    ]
    idea_lines = [
        f"- [{card.kind}/{card.status}] {card.title}｜来源={card.author}：{card.body}"
        for card in (idea_cards or [])[-8:]
    ]
    plan_lines = _plan_context_lines(plan)
    version_lines = [
        f"- {version.label}｜{version.status}｜{version.change_summary}"
        for version in (plan_versions or [])[-5:]
    ]
    metric_lines = [f"- {key}: {value}" for key, value in (metrics or {}).items()]

    return "\n".join(
        [
            "你拥有 TravelAssistant 三个导航页的全局上下文：聊天、想法、行程。回答和生成计划时必须综合这些信息，不要只看最新一句话。",
            "当前是用户与你的一对一私人对话，只包含用户和 Agent。",
            "",
            "旅行信息：",
            f"目的地/标题：{trip.destination}",
            f"时间：{trip.date_range}",
            f"预算：{trip.budget}",
            f"风格：{trip.style}",
            f"备注：{trip.note or '无'}",
            "",
            "全局整理指标：",
            *(metric_lines or ["- 暂无指标"]),
            "",
            "想法页 / 想法墙：",
            *(idea_lines or ["- 暂无想法卡"]),
            "",
            "聊天页 / 最近对话：",
            *(chat_lines or ["- 暂无对话"]),
            "",
            "行程页 / 当前计划：",
            *(plan_lines or ["- 暂无行程"]),
            "",
            "行程页 / 版本历史：",
            *(version_lines or ["- 暂无版本历史"]),
            "",
            f"用户最新消息：{user_body.strip()}",
        ]
    )


def _plan_context_lines(plan: TripPlan | None) -> list[str]:
    if not plan:
        return []
    lines = [f"- 当前行程：{plan.title}｜{plan.status}"]
    for item in plan.preparation.clothing[:4]:
        lines.append(f"- 旅行准备/衣物：{item.title}｜{item.status}｜{item.body}")
    for item in plan.preparation.accommodation[:4]:
        lines.append(f"- 旅行准备/住宿：{item.title}｜{item.status}｜{item.body}")
    for day in plan.days[:5]:
        item_titles = "、".join(f"{item.time} {item.title}@{item.location}" for item in day.items[:6])
        lines.append(f"- {day.label}：{item_titles or '暂无安排'}")
    return lines


def _parse_reply(content: str) -> AgentReply:
    data = _load_json(content)
    body = data.get("body")
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Model response is missing its reply body")
    card_data = data.get("card") if isinstance(data.get("card"), dict) else {}
    kind = str(card_data.get("kind") or "missing-info")
    if kind not in VALID_CARD_KINDS:
        kind = "missing-info"

    bullets = card_data.get("bullets")
    if not isinstance(bullets, list):
        bullets = []
    clean_bullets = [str(item).strip() for item in bullets if str(item).strip()][:4]
    if not clean_bullets:
        clean_bullets = ["继续补充时间、预算和必去地点", "我会把新消息整理进计划约束"]

    action_target = str(card_data.get("action_target") or "board")
    if action_target not in {"board", "itinerary"}:
        action_target = "itinerary" if kind in {"itinerary-draft", "revision", "conflict"} else "board"

    return AgentReply(
        body=body.strip(),
        card=AgentCard(
            kind=kind,
            title=str(card_data.get("title") or "偏好已更新").strip()[:40],
            summary=str(card_data.get("summary") or "我会结合当前对话继续整理你的旅行需求。").strip()[:120],
            bullets=clean_bullets,
            actions=[AgentAction(label="查看行程" if action_target == "itinerary" else "查看旅行想法", target=action_target)],
        ),
        idea_cards=_parse_idea_cards(data.get("idea_cards")),
        plan=_parse_plan(data.get("plan")),
    )


def _parse_idea_cards(raw_cards: object) -> list[dict]:
    if not isinstance(raw_cards, list):
        return []

    idea_cards = []
    for raw_card in raw_cards[:3]:
        if not isinstance(raw_card, dict):
            continue
        kind = str(raw_card.get("kind") or "待归类").strip()
        if kind not in VALID_IDEA_KINDS:
            kind = "待归类"
        title = str(raw_card.get("title") or "新的旅行想法").strip()[:24]
        body = str(raw_card.get("body") or title).strip()[:120]
        status = str(raw_card.get("status") or "已整理").strip()[:16]
        if title and body:
            idea_cards.append({"kind": kind, "title": title, "body": body, "status": status})
    return idea_cards


def _parse_plan(raw_plan: object) -> dict | None:
    if not isinstance(raw_plan, dict):
        return None
    raw_days = raw_plan.get("days")
    if not isinstance(raw_days, list):
        return None

    days = []
    for raw_day in raw_days[:5]:
        if not isinstance(raw_day, dict):
            continue
        raw_items = raw_day.get("items")
        if not isinstance(raw_items, list):
            continue
        items = []
        for raw_item in raw_items[:6]:
            if not isinstance(raw_item, dict):
                continue
            title = str(raw_item.get("title") or "待定安排").strip()[:40]
            if not title:
                continue
            satisfies = raw_item.get("satisfies")
            if not isinstance(satisfies, list):
                satisfies = []
            category = str(raw_item.get("category") or "activity").strip()
            if category not in {"activity", "meal", "transport", "accommodation", "rest"}:
                category = "activity"
            items.append(
                {
                    "time": str(raw_item.get("time") or "待定").strip()[:16],
                    "title": title,
                    "location": str(raw_item.get("location") or "地点待定").strip()[:40],
                    "duration": str(raw_item.get("duration") or "时长待定").strip()[:24],
                    "reason": str(raw_item.get("reason") or "根据你的旅行偏好安排。").strip()[:120],
                    "notes": str(raw_item.get("notes") or "可以继续在对话里调整。").strip()[:120],
                    "satisfies": [str(item).strip()[:16] for item in satisfies if str(item).strip()][:4],
                    "category": category,
                    "meal": _parse_meal(raw_item.get("meal")),
                }
            )
        if items:
            days.append({"label": str(raw_day.get("label") or f"Day {len(days) + 1}").strip()[:16], "items": items})
    if not days:
        return None
    return {
        "title": str(raw_plan.get("title") or "旅行行程草案").strip()[:60],
        "status": str(raw_plan.get("status") or "草案").strip()[:16],
        "preparation": _parse_preparation(raw_plan.get("preparation")),
        "days": days,
    }


def _parse_meal(raw_meal: object) -> dict | None:
    if not isinstance(raw_meal, dict):
        return None
    return {
        "meal_type": str(raw_meal.get("meal_type") or "用餐").strip()[:16],
        "recommendation": str(raw_meal.get("recommendation") or "当地特色餐食待确认").strip()[:80],
        "cuisine": str(raw_meal.get("cuisine") or "当地菜").strip()[:40],
        "budget": str(raw_meal.get("budget") or "预算待确认").strip()[:32],
        "reservation": str(raw_meal.get("reservation") or "待确认").strip()[:24],
    }


def _parse_preparation(raw_preparation: object) -> dict:
    if not isinstance(raw_preparation, dict):
        return {"clothing": [], "accommodation": []}

    def parse_items(key: str) -> list[dict]:
        raw_items = raw_preparation.get(key)
        if not isinstance(raw_items, list):
            return []
        items = []
        for raw_item in raw_items[:6]:
            if not isinstance(raw_item, dict):
                continue
            title = str(raw_item.get("title") or "待确认事项").strip()[:40]
            body = str(raw_item.get("body") or "可以在生成行程后继续补充。").strip()[:140]
            status = str(raw_item.get("status") or "待确认").strip()[:16]
            if title and body:
                items.append({"title": title, "body": body, "status": status})
        return items

    return {
        "clothing": parse_items("clothing"),
        "accommodation": parse_items("accommodation"),
    }


def _load_json(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end >= start:
        text = text[start : end + 1]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("Agent response is not a JSON object")
    return data
