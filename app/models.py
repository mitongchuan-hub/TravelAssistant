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
    arrival_time: str = ""
    departure_time: str = ""
    city: str = ""
    arrival_location: str = ""
    departure_location: str = ""
    day_count: int | None = None
    travelers: int | None = None
    budget_amount: str = ""
    budget_scope: str = "unspecified"
    budget_coverage: str = ""
    must_visit: list[str] = field(default_factory=list)
    avoid: list[str] = field(default_factory=list)
    dietary: list[str] = field(default_factory=list)
    pace: str = ""
    lodging_address: str = ""
    outing_start_time: str = ""
    outing_end_time: str = ""
    revision: int = 0


@dataclass
class Trip:
    id: str
    destination: str
    date_range: str
    budget: str
    style: str
    note: str
    owner: User
    status: str
    last_activity: str
    requirements: TripRequirements = field(default_factory=TripRequirements)
    workflow_state: dict = field(default_factory=dict)
    mode: str = "travel"
    lodging: dict = field(default_factory=dict)


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
    requirements_snapshot: dict = field(default_factory=dict)

    @property
    def has_content(self) -> bool:
        return bool(self.text.strip() or self.days)


@dataclass(frozen=True)
class IdeaCard:
    id: str
    kind: str
    title: str
    body: str
    author: str
    status: str
    rotation: str = "0deg"
