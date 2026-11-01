"""Identity of saved itinerary business content, independent of HTML rendering."""
from __future__ import annotations
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, is_dataclass
from hashlib import sha256
from copy import deepcopy
import json
from typing import Any

def _revision(value, label):
    if type(value) is not int or value < 0:
        raise ValueError(f'{label} must be a non-negative integer')
    return value

# 使用确定性 JSON 编码生成稳定的内容摘要。
def _stable_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(encoded.encode("utf-8")).hexdigest()


# 将支持的计划对象转换为普通映射，供后续提取业务基线。
def _object_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return {str(key): deepcopy(item) for key, item in value.items()}
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return {str(key): deepcopy(item) for key, item in vars(value).items() if not key.startswith("_")}
    raise ValueError("plan must be a dataclass, mapping, or object")


# 计划基线模式：记录已持久化业务内容的身份与版本，不纳入异步展示字段。
@dataclass(frozen=True)
class PlanBasis:
    """Identity of committed plan business content, excluding async display data."""

    plan_id: str
    content_hash: str
    business_hash: str
    status: str
    requirements_revision: int | None
    has_content: bool

    # 校验基线字段的协议形态；摘要一致不等于现实行程内容真实有效。
    def __post_init__(self) -> None:
        if not isinstance(self.plan_id, str) or len(self.plan_id) > 200:
            raise ValueError("plan_id is invalid")
        for name in ("content_hash", "business_hash"):
            value = getattr(self, name)
            if value and (not isinstance(value, str) or len(value) != 64):
                raise ValueError(f"{name} must be a sha256 digest")
        if not isinstance(self.status, str) or len(self.status) > 100:
            raise ValueError("plan status is invalid")
        if self.requirements_revision is not None:
            _revision(self.requirements_revision, "plan requirements_revision")
        if type(self.has_content) is not bool:
            raise ValueError("has_content must be boolean")

    # 从已有基线或字段完整的映射构造统一基线对象。
    @classmethod
    def from_value(cls, value: PlanBasis | Mapping[str, Any]) -> PlanBasis:
        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise ValueError("plan_basis must be PlanBasis or a mapping")
        expected = {field.name for field in fields(cls)}
        if set(value) != expected:
            raise ValueError("plan_basis fields are incomplete")
        return cls(**{name: value[name] for name in expected})

    # 导出全部基线字段的普通字典。
    def to_dict(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}


# 展示字段不参与业务冲突判断，避免异步页面生成造成伪冲突。
_DISPLAY_FIELDS = frozenset({"display", "display_source_hash", "display_status", "display_style"})


# 从已提交计划提取稳定业务基线，用于并发冲突判断而非现实世界事实核验。
def plan_basis(plan: Any) -> PlanBasis:
    """Build a stable basis for committed plan content.

    Presentation/display fields are deliberately excluded so an asynchronous
    display job cannot create a false conflict for otherwise unchanged prose.
    """
    raw = _object_mapping(plan)
    if not raw:
        return PlanBasis("", "", "", "", None, False)
    business = {key: value for key, value in raw.items() if key not in _DISPLAY_FIELDS}
    text = business.get("text")
    days = business.get("days")
    content = text if isinstance(text, str) and text.strip() else days if isinstance(days, list) else []
    has_content = bool((isinstance(text, str) and text.strip()) or days)
    requirements_revision = business.get("requirements_revision")
    if requirements_revision is not None and (type(requirements_revision) is not int or requirements_revision < 0):
        requirements_revision = None
    return PlanBasis(
        plan_id=str(business.get("id") or "")[:200],
        content_hash=_stable_hash(content) if has_content else "",
        business_hash=_stable_hash(business),
        status=str(business.get("status") or "")[:100],
        requirements_revision=requirements_revision,
        has_content=has_content,
    )


