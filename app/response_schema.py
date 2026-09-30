from __future__ import annotations


TRAVEL_RESULT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "body": {"type": "string"},
        "card": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "enum": [
                    "requirement-summary", "missing-info", "conflict",
                    "itinerary-draft", "revision",
                ]},
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "bullets": {"type": "array", "items": {"type": "string"}},
                "action_target": {"type": "string", "enum": ["board", "itinerary"]},
                "next_action": {"type": "string", "enum": ["continue_chat", "ask_plan_confirmation", "generate_plan", "show_plan"]},
            },
            "required": ["kind", "title", "summary", "bullets", "action_target", "next_action"],
        },
        "idea_cards": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["地点", "预算", "节奏", "禁忌", "待归类"]},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["kind", "title", "body", "status"],
            },
        },
        "plan": {
            "type": ["object", "null"],
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "status": {"type": "string", "enum": ["草案", "已修改", "待确认"]},
                "overview": {"type": "string"},
                "constraints_met": {"type": "array", "items": {"type": "string"}},
                "pending_items": {"type": "array", "items": {"type": "string"}},
                "risks": {"type": "array", "items": {"type": "string"}},
                "preparation": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "clothing": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": "string"},
                                    "body": {"type": "string"},
                                    "status": {"type": "string", "enum": ["建议", "待确认", "已确认", "已准备"]},
                                },
                                "required": ["title", "body", "status"],
                            },
                        },
                        "accommodation": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "title": {"type": "string"},
                                    "body": {"type": "string"},
                                    "status": {"type": "string", "enum": ["建议", "待确认", "已确认", "已预订"]},
                                },
                                "required": ["title", "body", "status"],
                            },
                        },
                    },
                    "required": ["clothing", "accommodation"],
                },
                "days": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "label": {"type": "string"},
                            "date": {"type": "string"},
                            "theme": {"type": "string"},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "category": {"type": "string", "enum": ["activity", "meal", "transport", "accommodation", "rest"]},
                                        "meal": {
                                            "type": ["object", "null"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "meal_type": {"type": "string"},
                                                "recommendation": {"type": "string"},
                                                "cuisine": {"type": "string"},
                                                "budget": {"type": "string"},
                                                "reservation": {"type": "string", "enum": ["无需预约", "建议预约", "待确认", "已预约"]},
                                            },
                                            "required": ["meal_type", "recommendation", "cuisine", "budget", "reservation"],
                                        },
                                        "time": {"type": "string"},
                                        "title": {"type": "string"},
                                        "location": {"type": "string"},
                                        "duration": {"type": "string"},
                                        "reason": {"type": "string", "description": "安排原因，以及对应的地点、饮食、节奏或预算需求"},
                                        "notes": {"type": "string", "description": "用餐、温度、穿衣、雨具和待确认事项"},
                                        "satisfies": {"type": "array", "items": {"type": "string"}},
                                    },
                                    "required": ["category", "meal", "time", "title", "location", "duration", "reason", "notes", "satisfies"],
                                },
                            },
                        },
                        "required": ["label", "date", "theme", "items"],
                    },
                },
            },
            "required": ["title", "status", "overview", "constraints_met", "pending_items", "risks", "preparation", "days"],
        },
    },
    "required": ["body", "card", "idea_cards", "plan"],
}

TRAVEL_RESULT_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "travel_result",
        "strict": True,
        "schema": TRAVEL_RESULT_SCHEMA,
    },
}
