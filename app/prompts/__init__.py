from __future__ import annotations

from pathlib import Path

PROMPTS_DIR = Path(__file__).resolve().parent


def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / name).read_text(encoding="utf-8").strip()


CHAT_PROMPT = load_prompt("skills/chat.txt")
BASE_SYSTEM_PROMPT = load_prompt("base_system.txt")
GATHER_TRIP_INFORMATION_PROMPT = load_prompt("skills/gather_trip_information.txt")
FINALIZE_TRIP_PLAN_PROMPT = load_prompt("skills/finalize_trip_plan.txt")
