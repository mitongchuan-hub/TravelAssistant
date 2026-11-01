"""Shared user-message rules for HTTP, task submission and request invariants."""

MAX_MESSAGE_LENGTH = 10_000


class MessageValidationError(ValueError):
    """A public message-input error with no user content in its description."""


def validate_message_text(value: object, *, allow_empty: bool = False) -> str:
    """Validate without rewriting the source text or silently truncating it."""
    if not isinstance(value, str):
        raise MessageValidationError('消息必须是文本')
    if len(value) > MAX_MESSAGE_LENGTH:
        raise MessageValidationError(f'消息不能超过 {MAX_MESSAGE_LENGTH} 字，请缩短后发送')
    if not allow_empty and not value.strip():
        raise MessageValidationError('消息不能为空')
    return value


def normalize_message_text(value: object) -> str:
    """Normalize accepted user submissions before identity checks and storage."""
    return validate_message_text(value).strip()
