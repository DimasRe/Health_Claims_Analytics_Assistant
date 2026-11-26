"""
Membuktikan setiap pertanyaan di bank pertanyaan DAPAT dijawab sistem:
panggilan tool acuan dinormalisasi seperti dari Qwen, lalu dijalankan ke
MCP Server dan PostgreSQL.
"""

import json
from pathlib import Path

import pytest
import yaml

from ai.qwen_mcp_client import normalize_tool_arguments
from mcp_server import server

BANK_FILE = Path(__file__).resolve().parent / "questions" / "question_bank.yaml"
QUESTIONS = yaml.safe_load(BANK_FILE.read_text(encoding="utf-8"))["questions"]
TOOL_NAMES = {
    "inspect_data", "analyze_data", "compare_costs", "service_mix",
    "service_growth", "metric_growth", "metric_share", "compare_metrics",
    "top_categories", "profile_summary",
}


def test_bank_is_well_formed():
    ids = [item["id"] for item in QUESTIONS]
    assert len(ids) == len(set(ids)), "ID pertanyaan duplikat"
    for item in QUESTIONS:
        assert item["question"].strip()
        assert set(item["acceptable_tools"]) <= TOOL_NAMES
        assert item["reference_calls"]
        for call in item["reference_calls"]:
            assert call["tool"] in item["acceptable_tools"]


@pytest.mark.db
@pytest.mark.parametrize(
    "item", QUESTIONS, ids=[f"Q{item['id']:03d}" for item in QUESTIONS]
)
def test_reference_calls_answer_question(item, database_available):
    for call in item["reference_calls"]:
        arguments = normalize_tool_arguments(
            call["tool"], dict(call.get("arguments") or {}), item["question"]
        )
        payload = json.loads(getattr(server, call["tool"])(**arguments))

        assert payload.get("success") is not False, (
            f"Q{item['id']} {call['tool']} gagal: {payload.get('message')}"
        )

        if call["tool"] != "inspect_data":
            has_data = any(
                payload.get(key)
                for key in ("results", "breakdown", "profiles", "totals")
            )
            assert has_data, f"Q{item['id']} tidak menghasilkan data"
