"""类型化请求和公开响应 DTO；不接受客户端坐标或内部工作流状态。"""
from typing import Literal, Required, TypedDict

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, field_validator

from app.core.message_input import MAX_MESSAGE_LENGTH, normalize_message_text


class UserDTO(TypedDict):
    id: str
    name: str
    initials: str


class SessionDTO(TypedDict):
    user: UserDTO | None
    csrf_token: str


class RequirementsDTO(TypedDict):
    start_date: str
    end_date: str
    arrival_time: str
    departure_time: str
    city: str
    arrival_location: str
    departure_location: str
    day_count: int | None
    travelers: int | None
    budget_amount: str
    budget_scope: str
    budget_coverage: str
    transport_mode: str
    must_visit: list[str]
    avoid: list[str]
    dietary: list[str]
    pace: str
    lodging_address: str
    outing_start_time: str
    outing_end_time: str
    revision: int


class PlaceDTO(TypedDict, total=False):
    poi_id: str
    name: str
    address: str
    longitude: float
    latitude: float
    city: str
    district: str
    source: str
    fetched_at: str
    selected_at: str
    place_type: str
    opening_hours: str


class AgentActionDTO(TypedDict):
    label: str
    target: str


class AgentCardDTO(TypedDict):
    kind: str
    title: str
    summary: str
    bullets: list[str]
    actions: list[AgentActionDTO]


class ProgressStepDTO(TypedDict, total=False):
    id: int
    kind: str
    label: str
    detail: str
    status: str
    started_at: int
    duration_ms: int


class ProgressDTO(TypedDict, total=False):
    state: str
    started_at: int
    updated_at: int
    duration_ms: int
    revision: int
    omitted: int
    steps: list[ProgressStepDTO]


class MealDTO(TypedDict):
    meal_type: str
    recommendation: str
    cuisine: str
    budget: str
    reservation: str


class RouteDTO(TypedDict):
    origin_poi_id: str
    origin_name: str
    destination_poi_id: str
    destination_name: str
    mode: str
    status: str
    duration_minutes: int | None
    distance_meters: int | None
    source: str
    fetched_at: str
    reason: str


class PlanItemDTO(TypedDict):
    id: str
    time: str
    title: str
    location: str
    duration: str
    reason: str
    notes: str
    satisfies: list[str]
    category: str
    meal: MealDTO | None
    place: PlaceDTO | None
    place_scope: str
    travel_mode: str
    incoming_route: RouteDTO | None
    departure_route: RouteDTO | None


class PlanDayDTO(TypedDict):
    id: str
    label: str
    items: list[PlanItemDTO]
    date: str
    theme: str


class PreparationItemDTO(TypedDict):
    title: str
    body: str
    status: str


class PreparationDTO(TypedDict):
    clothing: list[PreparationItemDTO]
    accommodation: list[PreparationItemDTO]


class PlanDTO(TypedDict):
    id: str
    title: str
    status: str
    text: str
    overview: str
    constraints_met: list[str]
    pending_items: list[str]
    risks: list[str]
    requirements_revision: int | None
    display_status: str
    display_source_hash: str
    days: list[PlanDayDTO]
    preparation: PreparationDTO
    has_content: bool


class TaskDTO(TypedDict, total=False):
    id: str
    trip_id: str
    kind: str
    request_id: str
    retry_of: str
    status: str
    created_at: str
    updated_at: str
    message_id: str
    thinking_id: str
    plan_id: str
    requirements_revision: int
    message: Required[str]


class IdeaDTO(TypedDict):
    id: str
    kind: str
    title: str
    body: str
    author: str
    status: str
    rotation: str


class MetricsDTO(TypedDict):
    message_count: int
    idea_count: int
    constraint_count: int
    reminder_count: int
    plan_day_count: int
    plan_date_range: str
    plan_item_count: int
    date_coverage_note: str


class DisplayStateDTO(TypedDict):
    plan_id: str
    source_hash: str
    display_status: str
    version: str
    enabled: bool
    has_text: bool
    requirements_revision: int
    basis_status: str
    basis_message: str
    guide_available: bool
    guide_version: str
    busy: bool


class TripDTO(TypedDict):
    id: str
    destination: str
    date_range: str
    budget: str
    style: str
    note: str
    status: str
    last_activity: str
    mode: str
    owner: UserDTO
    requirements: RequirementsDTO
    lodging: PlaceDTO


class MessageDTO(TypedDict):
    id: str
    sender: UserDTO
    sender_type: str
    body: str
    created_at: str
    status: str
    agent_card: AgentCardDTO | None
    execution_progress: ProgressDTO


class WorkspaceDTO(TypedDict):
    trip: TripDTO
    plan: PlanDTO
    messages: list[MessageDTO]
    tasks: list[TaskDTO]
    idea_cards: list[IdeaDTO]
    wall_cards: list[IdeaDTO]
    metrics: MetricsDTO
    display_state: DisplayStateDTO


class InputDTO(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Credentials(InputDTO):
    email: StrictStr = Field(max_length=254)
    password: StrictStr = Field(max_length=1024)


class EmptyInput(InputDTO):
    pass


class CityInput(InputDTO):
    lodging_address: StrictStr = ''
    city_hint: StrictStr = ''
    start_date: StrictStr = ''
    day_count: StrictStr = ''
    outing_start_time: StrictStr = ''
    outing_end_time: StrictStr = ''
    travelers: StrictStr = ''
    budget_amount: StrictStr = ''
    budget_scope: StrictStr = ''
    pace: StrictStr = ''
    must_visit: StrictStr = ''
    avoid: StrictStr = ''
    dietary: StrictStr = ''
    note: StrictStr = ''
    revision: StrictStr = ''
    transport_mode: StrictStr = ''
    choice_token: StrictStr = Field(default='', max_length=100)
    poi_id: StrictStr = Field(default='', max_length=200)


class MessageInput(InputDTO):
    body: StrictStr = Field(min_length=1, max_length=MAX_MESSAGE_LENGTH)
    request_id: StrictStr = Field(min_length=1, max_length=100)

    @field_validator('body', mode='before')
    @classmethod
    def validate_body(cls, value):
        return normalize_message_text(value)


class RetryInput(InputDTO):
    request_id: StrictStr = Field(min_length=1, max_length=100)


class TaskInput(InputDTO):
    kind: Literal['chat', 'plan', 'revision']
    request_id: StrictStr = Field(min_length=1, max_length=100)
    payload: dict = Field(default_factory=dict)


class IdeaInput(InputDTO):
    body: StrictStr = Field(min_length=1, max_length=2000)


class DisplayInput(InputDTO):
    plan_id: StrictStr = Field(min_length=1, max_length=200)
    source_hash: StrictStr = Field(min_length=1, max_length=200)


class BasisInput(InputDTO):
    expected_plan_id: StrictStr = Field(min_length=1, max_length=200)
    source_hash: StrictStr = Field(min_length=1, max_length=200)
    expected_requirements_revision: StrictInt = Field(ge=0)
