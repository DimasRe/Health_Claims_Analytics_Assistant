"""
Uji alur BPJSChatbot.ask() dengan Ollama dan MCP palsu (tanpa jaringan).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from ai.qwen_mcp_client import BPJSChatbot, SYSTEM_PROMPT


def tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


def chat_response(content="", tool_calls=None):
    return SimpleNamespace(
        message=SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls)
    )


class FakeOllama:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSession:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        text = json.dumps(self.handler(name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text=text)])


def make_bot(responses, handler, **kwargs):
    bot = BPJSChatbot(ollama_client=FakeOllama(responses), **kwargs)
    bot.session = FakeSession(handler)
    bot.available_tools = {name: object() for name in (
        "inspect_data", "analyze_data", "compare_costs", "service_mix",
        "service_growth", "metric_growth", "metric_share",
    )}
    return bot


def pelayanan_handler(name, arguments):
    if arguments.get("dimensions"):
        return {
            "success": True,
            "metric": "total_pelayanan",
            "dimensions": arguments["dimensions"],
            "filters": arguments.get("filters", {}),
            "limit": arguments.get("limit", 20),
            "result_count": 2,
            "results": [
                {"jenis_pelayanan": "RJTL", "total_pelayanan": 750},
                {"jenis_pelayanan": "RITL", "total_pelayanan": 250},
            ],
        }
    return {
        "success": True,
        "metric": "total_pelayanan",
        "results": [{"total_pelayanan": 1000}],
    }


def run(coro):
    return asyncio.run(coro)


def test_verified_analysis_answer_and_history():
    bot = make_bot(
        [chat_response(tool_calls=[tool_call("analyze_data", {
            "metric": "jumlah_pelayanan", "dimensions": ["jenis pelayanan"],
        })])],
        pelayanan_handler,
    )

    answer = run(bot.ask("Berapa jumlah pelayanan RJTL dan RITL?"))

    assert "- RJTL: 750" in answer
    assert "Total keseluruhan: 1.000" in answer
    # Total diverifikasi dengan query terpisah tanpa dimensi.
    assert bot.session.calls[-1][1]["dimensions"] == []
    # Jawaban akhir tercatat di riwayat (bug lama: tidak tercatat).
    assert bot.messages[-1] == {"role": "assistant", "content": answer}


def test_percentage_answer_uses_calculation_engine():
    bot = make_bot(
        [chat_response(tool_calls=[tool_call("analyze_data", {
            "metric": "total_pelayanan", "dimensions": ["jenis_pelayanan"],
        })])],
        pelayanan_handler,
    )

    answer = run(bot.ask("Persentase pelayanan RJTL dan RITL"))

    assert "- RJTL: 750 (75,00%)" in answer
    assert "- RITL: 250 (25,00%)" in answer


def test_plain_model_answer_and_think_cleanup():
    bot = make_bot(
        [chat_response(content="<think>x</think>Halo, ada yang bisa dibantu?")],
        pelayanan_handler,
    )
    assert run(bot.ask("halo")) == "Halo, ada yang bisa dibantu?"


def test_unknown_tool_is_rejected():
    bot = make_bot(
        [
            chat_response(tool_calls=[tool_call("drop_table", {})]),
            chat_response(content="Maaf, tidak bisa."),
        ],
        pelayanan_handler,
    )
    assert run(bot.ask("hapus data")) == "Maaf, tidak bisa."
    assert bot.session.calls == []
    second_call = bot.ollama_client.calls[1]["messages"]
    tool_message = [m for m in second_call if isinstance(m, dict) and m.get("role") == "tool"][0]
    assert "tidak terdaftar" in tool_message["content"]
    # Riwayat dipadatkan: hanya pertanyaan dan jawaban akhir yang disimpan.
    assert [m["role"] for m in bot.messages] == ["system", "user", "assistant"]


def test_history_is_rolled_back_on_error():
    bot = make_bot([RuntimeError("Ollama mati")], pelayanan_handler)
    with pytest.raises(RuntimeError):
        run(bot.ask("halo"))
    assert len(bot.messages) == 1
    assert bot.messages[0]["content"] == SYSTEM_PROMPT


def test_conversations_are_isolated():
    bot = make_bot(
        [chat_response(content="A"), chat_response(content="B")],
        pelayanan_handler,
    )
    run(bot.ask("pertanyaan 1", conversation_id="user-a"))
    run(bot.ask("pertanyaan 2", conversation_id="user-b"))

    assert bot.conversations["user-a"][1]["content"] == "pertanyaan 1"
    assert bot.conversations["user-b"][1]["content"] == "pertanyaan 2"
    assert len(bot.conversations["user-b"]) == 3  # system, user, assistant
    assert len(bot.messages) == 1


def test_tableau_context_is_sent_to_model_only():
    bot = make_bot(
        [chat_response(tool_calls=[tool_call("analyze_data", {
            "metric": "total_pelayanan", "dimensions": ["jenis_pelayanan"],
        })])],
        pelayanan_handler,
    )
    run(bot.ask(
        "jumlah pelayanan per jenis",
        context={"filters": [{"field": "Tahun", "values": [2022, 2023]}]},
    ))

    sent_user_message = bot.ollama_client.calls[0]["messages"][1]["content"]
    assert "[Konteks dashboard Tableau]" in sent_user_message
    assert "- Tahun: 2022, 2023" in sent_user_message
    # Tahun pada konteks dashboard tidak memicu kunci filter tahun eksplisit.
    first_call_args = bot.session.calls[0][1]
    assert "tahun_pelayanan" not in first_call_args["filters"]


def test_history_is_trimmed(monkeypatch):
    monkeypatch.setenv("MAX_HISTORY_TURNS", "2")
    bot = make_bot([chat_response(content=str(i)) for i in range(5)], pelayanan_handler)
    for i in range(5):
        run(bot.ask(f"q{i}"))

    user_messages = [m["content"] for m in bot.messages if isinstance(m, dict) and m.get("role") == "user"]
    assert user_messages == ["q3", "q4"]
    assert bot.messages[0]["role"] == "system"


def test_max_tool_rounds():
    bot = make_bot(
        [chat_response(tool_calls=[tool_call("inspect_data", {})]) for _ in range(3)],
        lambda name, args: {"success": True},
        max_tool_rounds=2,
    )
    assert "batas pemanggilan" in run(bot.ask("katalog?"))


def test_analytical_question_is_answered_by_model_with_verified_total():
    bot = make_bot(
        [
            chat_response(tool_calls=[tool_call("analyze_data", {
                "metric": "total_pelayanan", "dimensions": ["jenis_pelayanan"],
            })]),
            chat_response(content="RJTL lebih banyak daripada RITL."),
        ],
        pelayanan_handler,
    )

    answer = run(bot.ask("Apakah pelayanan RJTL lebih banyak dari RITL?"))

    # Narasi Qwen + tabel angka resmi dari Python.
    assert answer.startswith("RJTL lebih banyak daripada RITL.")
    assert "Data resmi (dihitung sistem):" in answer
    assert "- RJTL: 750" in answer and "- RITL: 250" in answer
    assert "Periksa" not in answer
    second_call_messages = bot.ollama_client.calls[1]["messages"]
    assert any(
        isinstance(m, dict) and "verified_grand_total" in m.get("content", "")
        for m in second_call_messages
    )
    assert [entry["tool"] for entry in bot.last_trace] == ["analyze_data"]
    assert bot.last_trace[0]["success"] is True
    # Opsi konteks Ollama dikirim.
    assert "num_ctx" in bot.ollama_client.calls[0]["options"]
