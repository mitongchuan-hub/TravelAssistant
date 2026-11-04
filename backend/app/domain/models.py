"""业务数据模型：统一定义用户、旅行需求、聊天消息、行程和想法卡片等对象的数据结构。

这里的 model 指业务数据模型，不是 AI 模型；主要描述对象有哪些字段及其默认值，
不负责模型调用、业务流程或数据库读写。dataclass 的类型标注不会自动校验运行时输入。
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    id: str
    name: str
    initials: str


@dataclass
class TripRequirements:
    start_date: str = ""
    end_date: str = ""
    city: str = ""
    day_count: int | None = None
    transport_mode: str = "unspecified"
    lodging_address: str = ""
    text: str = ""
    revision: int = 0


@dataclass
class Trip:
    id: str
    destination: str
    owner: User
    status: str
    last_activity: str
    requirements: TripRequirements = field(default_factory=TripRequirements)
    workflow_state: dict = field(default_factory=dict)
    mode: str = "city"
    lodging: dict = field(default_factory=dict)
    legacy_metadata: dict = field(default_factory=dict, repr=False)

    @property
    def date_range(self):
        from app.domain.summaries import trip_summaries
        return trip_summaries(self.requirements, self.legacy_metadata, self.mode)['date_range']



@dataclass(frozen=True)
class AgentAction:
    label: str
    target: str


@dataclass(frozen=True)
class AgentCard:
    kind: str
    title: str
    summary: str
    bullets: list[str]
    actions: list[AgentAction]


@dataclass
class Message:
    id: str
    sender: User
    sender_type: str
    body: str
    created_at: str
    status: str = "sent"
    agent_card: AgentCard | None = None
    execution_progress: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MealRecommendation:
    meal_type: str
    recommendation: str
    cuisine: str
    budget: str
    reservation: str = "待确认"


@dataclass(frozen=True)
class PlaceDetails:
    poi_id: str
    name: str
    address: str
    longitude: float
    latitude: float
    city: str
    district: str
    source: str
    fetched_at: str
    selected_at: str = ""
    place_type: str = ""
    opening_hours: str = ""


@dataclass(frozen=True)
class RouteDetails:
    origin_poi_id: str = ""
    origin_name: str = ""
    destination_poi_id: str = ""
    destination_name: str = ""
    mode: str = "transit"
    status: str = "unavailable"
    duration_minutes: int | None = None
    distance_meters: int | None = None
    source: str = ""
    fetched_at: str = ""
    reason: str = "交通耗时待核实。"


@dataclass(frozen=True)
class TripItem:
    id: str
    time: str
    title: str
    location: str
    duration: str
    reason: str
    notes: str
    satisfies: list[str] = field(default_factory=list)
    category: str = "activity"
    meal: MealRecommendation | None = None
    place: PlaceDetails | None = None
    place_scope: str = "unknown"
    travel_mode: str = "transit"
    incoming_route: RouteDetails | None = None
    departure_route: RouteDetails | None = None


@dataclass(frozen=True)
class TripDay:
    id: str
    label: str
    items: list[TripItem]
    date: str = ""
    theme: str = ""


@dataclass(frozen=True)
class PreparationItem:
    title: str
    body: str
    status: str = "待确认"


@dataclass
class TravelPreparation:
    clothing: list[PreparationItem] = field(default_factory=list)
    accommodation: list[PreparationItem] = field(default_factory=list)


@dataclass
class TripPlan:
    id: str
    title: str
    status: str
    days: list[TripDay] = field(default_factory=list)
    preparation: TravelPreparation = field(default_factory=TravelPreparation)
    overview: str = ""
    constraints_met: list[str] = field(default_factory=list)
    pending_items: list[str] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    text: str = ""
    evidence: list[dict] = field(default_factory=list)
    requirements_revision: int | None = None
    display: dict | None = None
    display_source_hash: str = ""
    display_status: str = "not_requested"
    display_style: str = ""
    requirements_snapshot: dict = field(default_factory=dict)
    lodging_snapshot: dict = field(default_factory=dict)
    requirements_basis_complete: bool = True
    requirements_legacy_stale: bool = False

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.days)
