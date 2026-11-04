"""演示数据纯工厂：每次返回新的业务对象，不创建 Store、不读写数据库。"""
from collections.abc import Mapping, Sequence

from app.domain.models import AgentAction, AgentCard, Message, TripDay, TripItem, TripPlan, User


def seed_messages(users: Mapping[str, User]) -> list[Message]:
    return [
        Message(
            id="m1",
            sender=users["u1"],
            sender_type="user",
            body="我 8 月底想去杭州，安排两晚，节奏别太赶。",
            created_at="19:10",
        ),
        Message(
            id="m2",
            sender=users["agent"],
            sender_type="agent",
            body="收到，我先按轻松的 3 天 2 晚来整理。",
            created_at="19:11",
            agent_card=AgentCard(
                kind="requirement-summary",
                title="旅行框架已经记下",
                summary="杭州、两晚、轻松节奏已经进入计划约束。",
                bullets=["8 月底出发", "安排两晚", "避免行程太赶", "还需要预算和必去地点"],
                actions=[AgentAction(label="查看旅行想法", target="board")],
            ),
        ),
        Message(
            id="m3",
            sender=users["u1"],
            sender_type="user",
            body="我想去西湖和茶园，预算控制在人均 1500 左右。",
            created_at="19:12",
        ),
        Message(
            id="m4",
            sender=users["agent"],
            sender_type="agent",
            body="信息够用了，我生成了第一版行程。",
            created_at="19:16",
            agent_card=AgentCard(
                kind="itinerary-draft",
                title="杭州 3 天 2 晚初版行程",
                summary="西湖、龙井茶园、运河街区，整体偏轻松。",
                bullets=["每天 2-3 个主要安排", "下午保留休息时间", "预算按人均 1500 控制"],
                actions=[AgentAction(label="查看行程", target="itinerary")],
            ),
        ),
    ]



def seed_plan() -> TripPlan:
    return TripPlan(
        id="plan-v1",
        title="杭州 3 天 2 晚轻松行程",
        status="草案",
        days=[
            TripDay(
                id="day-1",
                label="Day 1",
                items=[
                    TripItem(
                        id="item-1",
                        time="14:00",
                        title="抵达杭州并入住",
                        location="湖滨商圈附近",
                        duration="1.5 小时",
                        reason="方便晚间步行到西湖，也便于第二天出发。",
                        notes="住宿预算还需要继续确认。",
                        satisfies=["交通便利", "轻松节奏"],
                    ),
                    TripItem(
                        id="item-2",
                        time="17:00",
                        title="西湖傍晚散步",
                        location="断桥 - 白堤",
                        duration="2 小时",
                        reason="符合轻松节奏，也满足西湖需求。",
                        notes="避开中午高温和人流。",
                        satisfies=["西湖", "轻松节奏"],
                    ),
                ],
            ),
            TripDay(
                id="day-2",
                label="Day 2",
                items=[
                    TripItem(
                        id="item-3",
                        time="10:00",
                        title="龙井茶园",
                        location="龙井村",
                        duration="3 小时",
                        reason="满足茶园偏好，适合慢节奏体验。",
                        notes="建议提前确认是否需要预约体验。",
                        satisfies=["茶园体验", "预算可控"],
                    )
                ],
            ),
        ],
    )
