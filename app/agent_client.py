from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from .models import AgentAction, AgentCard, IdeaCard, Message, PlanVersion, Trip, TripPlan


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


SYSTEM_PROMPT = """
你是 TravelAssistant 的私人旅行规划 Agent。当前对话只有一位用户和你，用户发送每条消息后都期待你的直接回复，不需要使用 @ 唤醒你。

请始终使用简体中文，语气轻松、具体，像可靠的私人旅行助手，避免办公化措辞。
你既要自然回答旅行问题，也要把对话中明确的时间、预算、地点、节奏和禁忌整理成可执行约束。请结合旅行基础信息、最近对话、想法墙、当前行程和版本历史，返回一个 JSON 对象，不要输出 Markdown。

JSON 格式：
{
  "body": "一句适合显示在聊天气泡里的直接回复，60 字以内",
  "card": {
    "kind": "requirement-summary | missing-info | conflict | itinerary-draft | revision 之一",
    "title": "卡片标题，18 字以内",
    "summary": "卡片摘要，60 字以内",
    "bullets": ["要点 1", "要点 2", "要点 3"],
    "action_target": "board 或 itinerary"
  },
  "idea_cards": [
    {
      "kind": "地点 | 预算 | 节奏 | 禁忌 | 待归类 之一",
      "title": "适合贴到想法墙的短标题，14 字以内",
      "body": "把用户原话整理成一条可执行约束，60 字以内",
      "status": "已整理 | 约束 | 候选 | 冲突提醒 | 待补充 之一"
    }
  ],
  "plan": {
    "title": "行程标题",
    "status": "草案 | 已修改",
    "days": [
      {
        "label": "Day 1",
        "items": [
          {
            "time": "09:30",
            "title": "安排标题",
            "location": "地点",
            "duration": "停留时长",
            "reason": "为什么这样安排",
            "notes": "提醒或待确认事项",
            "satisfies": ["对应的偏好或约束"]
          }
        ]
      }
    ]
  }
}

选择规则：
- 提到预算、时间、地点、饮食或旅行偏好：kind 用 requirement-summary 或 missing-info。
- 提到不要、太累、冲突、不能接受：kind 用 conflict。
- 明确要求生成、调整或查看行程：kind 用 itinerary-draft 或 revision。
- 信息不足时不要假装已生成完整行程，应直接追问最关键的一项信息。
- idea_cards 最多返回 3 条，只抽取最新消息或最近对话里明确的约束，不要重复空泛内容。
- 只有用户明确要求生成、修改、安排或查看行程时才返回 plan；否则 plan 为 null 或省略。
""".strip()


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
) -> AgentReply | None:
    if not is_llm_enabled():
        return None

    try:
        from openai import OpenAI

        client = OpenAI(
            api_key=os.getenv("OPENAI_API_KEY"),
            base_url=os.getenv("OPENAI_BASE_URL") or None,
            timeout=45.0,
            max_retries=0,
        )
        model = os.getenv("OPENAI_MODEL") or os.getenv("MODEL_ID") or "qwen-plus"
        response = _create_chat_completion(
            client,
            model,
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": _build_context(
                        trip,
                        messages,
                        user_body,
                        idea_cards=idea_cards,
                        plan=plan,
                        plan_versions=plan_versions,
                        metrics=metrics,
                    ),
                },
            ],
        )
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
        logger.warning("Agent reply failed (%s)", type(exc).__name__)
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


def _create_chat_completion(client, model: str, messages: list[dict]) -> object:
    from openai import BadRequestError

    params: dict[str, object] = {"model": model, "temperature": 0.4, "messages": messages}
    try:
        return client.chat.completions.create(**params)
    except BadRequestError as exc:
        text = str(exc).lower()
        if "temperature" in text and "not supported" in text:
            params.pop("temperature", None)
            return client.chat.completions.create(**params)
        raise


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
            items.append(
                {
                    "time": str(raw_item.get("time") or "待定").strip()[:16],
                    "title": title,
                    "location": str(raw_item.get("location") or "地点待定").strip()[:40],
                    "duration": str(raw_item.get("duration") or "时长待定").strip()[:24],
                    "reason": str(raw_item.get("reason") or "根据你的旅行偏好安排。").strip()[:120],
                    "notes": str(raw_item.get("notes") or "可以继续在对话里调整。").strip()[:120],
                    "satisfies": [str(item).strip()[:16] for item in satisfies if str(item).strip()][:4],
                }
            )
        if items:
            days.append({"label": str(raw_day.get("label") or f"Day {len(days) + 1}").strip()[:16], "items": items})
    if not days:
        return None
    return {
        "title": str(raw_plan.get("title") or "旅行行程草案").strip()[:60],
        "status": str(raw_plan.get("status") or "草案").strip()[:16],
        "days": days,
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
