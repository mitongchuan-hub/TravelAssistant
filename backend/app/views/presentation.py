"""Display helpers for itinerary navigation and the time rail."""
import re
from datetime import date


def day_date_label(value: str) -> str:
    text = (value or "").strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            parsed = date.fromisoformat(text)
            return f"{parsed.month}月{parsed.day}日"
        except ValueError:
            pass
    return text or "日期待定"


def timeline_time_parts(value: str) -> tuple[str, str]:
    text = (value or "").strip()
    match = re.fullmatch(
        r"((?:[01]?\d|2[0-3])[:：][0-5]\d)\s*[-—–~～至]\s*((?:[01]?\d|2[0-3])[:：][0-5]\d)", text
    )
    if match:
        return match.group(1), match.group(2)
    return text or "时间待定", ""
