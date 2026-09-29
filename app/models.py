from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    id: str
    name: str
    initials: str


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
class TripItem:
    id: str
    time: str
    title: str
    location: str
    duration: str
    reason: str
    notes: str
    satisfies: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TripDay:
    id: str
    label: str
    items: list[TripItem]


@dataclass
class TripPlan:
    id: str
    title: str
    status: str
    days: list[TripDay] = field(default_factory=list)


@dataclass(frozen=True)
class PlanVersion:
    id: str
    label: str
    title: str
    status: str
    created_at: str
    change_summary: str
    days: list[TripDay] = field(default_factory=list)


@dataclass(frozen=True)
class IdeaCard:
    id: str
    kind: str
    title: str
    body: str
    author: str
    status: str
    rotation: str = "0deg"
