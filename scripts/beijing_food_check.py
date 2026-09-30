import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
from app.tools.registry import run_tool

for kw in ["烤鸭", "涮羊肉", "炸酱面", "北京菜"]:
    r = run_tool("search_food", {"city": "北京", "keywords": kw, "max_results": 3})
    print("==", kw, r["status"])
    for x in r.get("recommendations", []):
        print(f"  {x['name']} | {x['rating']}分 | 人均{x['cost_per_person_cny']} | {x['address']} | {x['tel']}")
