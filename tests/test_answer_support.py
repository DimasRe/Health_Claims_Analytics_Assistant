"""Uji patch B: pemilihan tool, prompt modular, tabel resmi, cek angka."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
from decimal import Decimal
import pytest
import yaml

from ai.answer_support import (
    compact_tool_output,
    find_unverified_numbers,
    format_data_tables,
    select_tools,
)
from ai.prompts import TOOL_GUIDES, build_system_prompt
from ai.qwen_mcp_client import BPJSChatbot, SYSTEM_PROMPT

BANK = yaml.safe_load(
    (Path(__file__).parent / "questions" / "question_bank.yaml").read_text(encoding="utf-8")
)["questions"]


# ---------------- pemilihan tool ----------------

@pytest.mark.parametrize("item", BANK, ids=[f"Q{i['id']:03d}" for i in BANK])
def test_reference_tool_is_selected(item):
    selected = select_tools(item["question"])
    for call in item["reference_calls"]:
        assert call["tool"] in selected, (item["question"], selected)
    assert len(selected) <= 5


def test_inspect_data_only_when_asked():
    assert "inspect_data" not in select_tools("Berapa jumlah pelayanan RJTL dan RITL?")
    assert "inspect_data" in select_tools("Metrik dan dimensi apa saja yang tersedia?")


def test_follow_up_reuses_previous_tools():
    selected = select_tools("bagaimana dengan tahun 2023?", previous_tools=["compare_costs"])
    assert "compare_costs" in selected


def test_tool_selection_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TOOL_SELECTION", "0")
    assert len(select_tools("halo")) == 10


# ---------------- prompt modular ----------------

def test_prompt_contains_only_selected_guides():
    prompt = build_system_prompt(["analyze_data", "metric_share"])
    assert "metric_share:" in prompt and "analyze_data:" in prompt
    assert "profile_summary:" not in prompt
    assert len(prompt) < len(SYSTEM_PROMPT)


def test_full_prompt_keeps_all_rules():
    for name in TOOL_GUIDES:
        assert f"{name}:" in SYSTEM_PROMPT
    for rule in ("Jangan menghitung sendiri", "distinct count", "divre_code",
                 "Konteks dashboard Tableau".upper()[:7], "Rawat inap"):
        assert rule.lower() in SYSTEM_PROMPT.lower()


# ---------------- pemadatan hasil tool ----------------

def test_compact_tool_output():
    output = json.dumps({"success": True, "results": [{"a": i} for i in range(60)]}, indent=2)
    compact = json.loads(compact_tool_output(output, max_rows=40))
    assert len(compact["results"]) == 41
    assert "20 baris lain" in compact["results"][-1]["catatan"]
    assert compact_tool_output("bukan json") == "bukan json"


# ---------------- pemeriksaan angka ----------------

SHARE_PAYLOAD = ("metric_share", {
    "success": True, "metric": "total_peserta_unik", "dimensions": ["jenis_kelamin"],
    "grand_total": 1234567,
    "results": [
        {"jenis_kelamin": "P", "total_peserta_unik": 700000, "persentase": "56.70"},
        {"jenis_kelamin": "L", "total_peserta_unik": 534567, "persentase": "43.30"},
    ],
})
COST_PAYLOAD = ("compare_costs", {
    "success": True,
    "results": {"total_biaya_tagihan": "4123000000.50", "total_biaya_verifikasi": "3700000000.00",
                "selisih_tagihan_verifikasi": "423000000.50", "persentase_selisih_dari_tagihan": "10.2595"},
})


@pytest.mark.parametrize("text", [
    "Peserta perempuan 700.000 orang (56,70%) dan laki-laki 534.567 (43,3%).",
    "Total peserta 1.234.567 orang.",
    "Selisih Rp 423.000.000,50 atau 10,26% dari tagihan.",
    "Tagihan sekitar Rp 4,12 miliar dan verifikasi Rp 3,7 miliar.",
    "Ada 2 kategori pada tahun 2024.",
])
def test_correct_numbers_are_not_flagged(text):
    assert find_unverified_numbers(text, [SHARE_PAYLOAD, COST_PAYLOAD], "tahun 2024") == []


@pytest.mark.parametrize("text, wrong", [
    ("Peserta perempuan 700.500 orang.", "700.500"),
    ("Persentase perempuan 65,2%.", "65,2%"),
    ("Tagihan sekitar Rp 5,4 miliar.", "5,4 miliar"),
])
def test_wrong_numbers_are_flagged(text, wrong):
    assert wrong in find_unverified_numbers(text, [SHARE_PAYLOAD, COST_PAYLOAD])


# ---------------- tabel angka resmi ----------------

def test_data_table_formatting():
    table = format_data_tables([SHARE_PAYLOAD, COST_PAYLOAD, SHARE_PAYLOAD])
    assert table.startswith("Data resmi (dihitung sistem):")
    assert "- P: 700.000 (56,70%)" in table
    assert "- Total keseluruhan: 1.234.567" in table
    assert "Rp 4.123.000.000,50" in table
    assert table.count("Persentase Total Peserta Unik") == 1  # duplikat dihapus


@pytest.mark.db
@pytest.mark.parametrize("item", BANK, ids=[f"Q{i['id']:03d}" for i in BANK])
def test_every_bank_tool_result_can_be_tabulated(item, database_available):
    from ai.qwen_mcp_client import normalize_tool_arguments
    from mcp_server import server

    payloads = []
    for call in item["reference_calls"]:
        args = normalize_tool_arguments(call["tool"], dict(call.get("arguments") or {}), item["question"])
        payloads.append((call["tool"], json.loads(getattr(server, call["tool"])(**args))))

    table = format_data_tables(payloads)
    if item["reference_calls"][0]["tool"] != "inspect_data":
        assert "Data resmi" in table
        # Angka di tabel resmi tentu harus lolos pemeriksaan angka.
        assert find_unverified_numbers(table, payloads, item["question"]) == []


# ---------------- integrasi chatbot ----------------

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


def _message(content="", tool_calls=None):
    return SimpleNamespace(message=SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls))


def _tool_call(name, arguments):
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=arguments))


class FakeSession:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(SHARE_PAYLOAD[1]))])


def _bot(responses):
    bot = BPJSChatbot(ollama_client=FakeOllama(responses))
    bot.session = FakeSession()
    names = ["inspect_data", "analyze_data", "compare_costs", "service_mix", "service_growth",
             "metric_growth", "metric_share", "compare_metrics", "top_categories", "profile_summary"]
    bot.available_tools = {name: object() for name in names}
    bot.ollama_tools = [{"type": "function", "function": {"name": n, "description": "", "parameters": {}}} for n in names]
    return bot


def test_only_selected_tools_are_sent_and_numbers_checked(monkeypatch):
    monkeypatch.setenv("ANSWER_NUMBER_MODE", "correct")
    bot = _bot([
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message(content="Perempuan 700.000 peserta (56,70%), laki-laki 999.999 peserta."),
        _message(content="Perempuan 700.000 peserta (56,70%), laki-laki 534.567 peserta."),
    ])
    answer = asyncio.run(bot.ask("Bagaimana distribusi peserta berdasarkan jenis kelamin?"))

    sent_tools = [tool["function"]["name"] for tool in bot.ollama_client.calls[0]["tools"]]
    assert "metric_share" in sent_tools and "inspect_data" not in sent_tools
    assert len(sent_tools) <= 5
    assert bot.ollama_client.calls[0]["options"]["num_ctx"] == 8192
    # Angka salah diperbaiki Qwen, tidak ada peringatan, narasi = data resmi.
    assert "999.999" not in answer and "Periksa" not in answer
    assert answer.startswith("Perempuan 700.000 peserta (56,70%), laki-laki 534.567 peserta.")
    assert "700.000 (56,70%)" in answer
    # Riwayat hanya berisi pertanyaan dan jawaban akhir.
    assert [m["role"] for m in bot.messages] == ["system", "user", "assistant"]


def test_timeout_gives_clear_message():
    bot = _bot([httpx.ReadTimeout("lambat")])
    with pytest.raises(TimeoutError, match="qwen3:4b"):
        asyncio.run(bot.ask("halo"))
    assert len(bot.messages) == 1


# ---------------- model "thinking" ----------------

from ai.qwen_mcp_client import TRUNCATED_ANSWER_MESSAGE, clean_model_answer  # noqa: E402


@pytest.mark.parametrize("raw, expected", [
    ("<think>pikir</think>\nJawaban", "Jawaban"),
    ("Okay, let's see... reasoning</think>\n\nJawaban akhir.", "Jawaban akhir."),
    ("Jawaban <think>pikiran terpotong", "Jawaban"),
    ("<think>pikiran terpotong tanpa akhir", ""),
])
def test_clean_model_answer_variants(raw, expected):
    assert clean_model_answer(raw) == expected


def _message_with_reason(content, done_reason):
    response = _message(content=content)
    response.done_reason = done_reason
    return response


def test_truncated_reasoning_is_not_shown():
    bot = _bot([
        _message_with_reason("Okay, let's see. The user is asking", "length"),
        _message_with_reason("Okay, still thinking about it", "length"),
    ])
    answer = asyncio.run(bot.ask("berapa jumlah pelayanan RJTL dan RITL?"))
    assert answer == TRUNCATED_ANSWER_MESSAGE
    assert "Okay" not in answer
    # Sebelum menyerah, Qwen diminta memanggil tool.
    assert bot.ollama_client.calls[1]["messages"][-1]["content"] == NO_TOOL_NUDGE


def test_q49_truncated_without_tool_gets_second_chance():
    # Kasus nyata Q49: Qwen menulis panjang tanpa tool lalu terpotong.
    bot = _bot([
        _message_with_reason("Berikut persentase per kelompok umur ... 0-4 RJTL", "length"),
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message(content="Perempuan 700.000 peserta."),
    ])
    answer = asyncio.run(bot.ask(
        "Berapa persentase pelayanan RITL dibandingkan RJTL di setiap kelompok umur pada tahun 2024?"
    ))
    assert answer.startswith("Perempuan 700.000 peserta.")
    assert "Data resmi (dihitung sistem):" in answer


def test_warn_mode_is_default_and_keeps_narrative(monkeypatch):
    monkeypatch.delenv("ANSWER_NUMBER_MODE", raising=False)
    bot = _q48_bot([
        _message(tool_calls=[Q48_TOOL]),
        _message(content="Tahun 2023 RITL DIVRE 01 hanya 6.705."),
    ])
    answer = asyncio.run(bot.ask(Q48))
    assert answer.startswith("Tahun 2023 RITL DIVRE 01 hanya 6.705.")
    assert "⚠️ Periksa" in answer and "6.705" in answer
    assert len(bot.ollama_client.calls) == 2  # tanpa panggilan tulis ulang


def test_truncated_narrative_keeps_official_table():
    bot = _bot([
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message_with_reason("Perempuan mendominasi dengan", "length"),
    ])
    answer = asyncio.run(bot.ask("Bagaimana distribusi peserta berdasarkan jenis kelamin?"))
    assert "terpotong" in answer
    assert "Data resmi (dihitung sistem):" in answer


def test_think_option(monkeypatch):
    monkeypatch.setenv("OLLAMA_THINK", "1")
    bot = _bot([_message(content="Halo")])
    asyncio.run(bot.ask("halo"))
    assert bot.ollama_client.calls[0]["think"] is True
    monkeypatch.setenv("OLLAMA_THINK", "0")
    bot = _bot([_message(content="Halo")])
    asyncio.run(bot.ask("halo"))
    assert bot.ollama_client.calls[0]["think"] is False


# ---------------- pengaman angka karangan ----------------

from ai.qwen_mcp_client import NO_DATA_ANSWER, NO_TOOL_NUDGE  # noqa: E402

HALLUCINATION = (
    "Berikut distribusi total pelayanan berdasarkan divisi regional:\n"
    "- Divre 10 (Jawa Timur): 1.200.000\n- Divre 20 (Sumatera): 1.100.000\n"
    "Total keseluruhan pelayanan: 4.800.000"
)


def test_hallucinated_numbers_trigger_tool_call():
    bot = _bot([
        _message(content=HALLUCINATION),
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message(content="Perempuan sedikit lebih banyak."),
    ])
    answer = asyncio.run(bot.ask("Berapa pelayanan berdasarkan divisi regional?"))

    assert "1.200.000" not in answer and "4.800.000" not in answer
    assert "Data resmi (dihitung sistem):" in answer
    second_call = bot.ollama_client.calls[1]["messages"]
    assert second_call[-1]["content"] == NO_TOOL_NUDGE
    # Jawaban karangan tidak ikut dikirim ulang ke Qwen.
    assert all("1.200.000" not in str(getattr(m, "content", m)) for m in second_call)


def test_persistent_hallucination_is_blocked():
    bot = _bot([_message(content=HALLUCINATION), _message(content=HALLUCINATION)])
    answer = asyncio.run(bot.ask("Berapa pelayanan berdasarkan divisi regional?"))
    assert answer == NO_DATA_ANSWER
    assert [m["role"] for m in bot.messages] == ["system", "user", "assistant"]
    assert bot.messages[-1]["content"] == NO_DATA_ANSWER


@pytest.mark.parametrize("reply", [
    "Halo! Ada yang bisa saya bantu terkait data BPJS?",
    "Saya bisa menganalisis data tahun 2024 untuk DIVRE 10.",
])
def test_answers_without_data_numbers_pass(reply):
    bot = _bot([_message(content=reply)])
    assert asyncio.run(bot.ask("halo, bisa bantu data tahun 2024 DIVRE 10?")) == reply


def test_follow_up_may_reuse_numbers_from_history():
    bot = _bot([
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message(content="Perempuan 700.000 peserta."),
        _message(content="Tadi jumlah peserta perempuan adalah 700.000."),
    ])
    asyncio.run(bot.ask("Bagaimana distribusi peserta berdasarkan jenis kelamin?"))
    answer = asyncio.run(bot.ask("berapa tadi jumlah perempuan?"))
    assert answer == "Tadi jumlah peserta perempuan adalah 700.000."


def test_direct_format_hides_duplicate_code_label():
    payload = {
        "metric": "total_pelayanan",
        "results": [{"divre_code": "01", "divre": "DIVRE 01", "total_pelayanan": 192734}],
    }
    text = BPJSChatbot.format_verified_analysis(payload, Decimal(2500000))
    assert "- DIVRE 01: 192.734" in text
    assert "01, DIVRE 01" not in text


def test_data_table_shows_applied_filters():
    payload = dict(SHARE_PAYLOAD[1], filters={"jenis_pelayanan": "RITL", "tahun_pelayanan": 2024})
    table = format_data_tables([("metric_share", payload)])
    assert "Filter: jenis_pelayanan = RITL; tahun_pelayanan = 2024" in table
    assert "Filter: tidak ada (seluruh data)" in format_data_tables([SHARE_PAYLOAD])


def test_truncated_result_is_announced_in_table():
    payload = dict(SHARE_PAYLOAD[1], possibly_truncated=True, result_count=2)
    table = format_data_tables([("metric_share", payload)])
    assert "hasil dibatasi 2 baris" in table


def test_table_and_tool_output_show_48_rows_by_default():
    rows = [{"divre": f"DIVRE {i:02d}", "total_pelayanan": 1000 + i} for i in range(48)]
    payload = {"success": True, "metric": "total_pelayanan", "dimensions": ["divre"], "results": rows}
    table = format_data_tables([("analyze_data", payload)])
    assert "DIVRE 47" in table and "baris lainnya" not in table
    compact = json.loads(compact_tool_output(json.dumps(payload)))
    assert len(compact["results"]) == 48


def test_table_limit_configurable(monkeypatch):
    monkeypatch.setenv("ANSWER_TABLE_MAX_LINES", "10")
    rows = [{"divre": f"D{i}", "total_pelayanan": i + 100} for i in range(48)]
    payload = {"success": True, "metric": "total_pelayanan", "dimensions": ["divre"], "results": rows}
    assert "38 baris lainnya" in format_data_tables([("analyze_data", payload)])


# ---------------- v1.2.9: narasi harus sama dengan data resmi ----------------

from ai.qwen_mcp_client import HIDDEN_NARRATIVE_MESSAGE  # noqa: E402

Q48_PAYLOAD = {
    "success": True, "operation": "compare_metrics", "metrics": ["total_pelayanan"],
    "labels": {"total_pelayanan": "Total Pelayanan"}, "dimensions": ["divre", "jenis_pelayanan", "tahun_pelayanan"],
    "filters": {"tahun_pelayanan": [2023, 2024]}, "totals": {"total_pelayanan": 991999},
    "results": [
        {"divre": "DIVRE 01", "jenis_pelayanan": "RITL", "tahun_pelayanan": 2023, "total_pelayanan": 7305},
        {"divre": "DIVRE 01", "jenis_pelayanan": "RJTL", "tahun_pelayanan": 2023, "total_pelayanan": 40734},
    ],
}


class Q48Session:
    async def call_tool(self, name, arguments):
        return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(Q48_PAYLOAD))])


def _q48_bot(responses):
    bot = _bot(responses)
    bot.session = Q48Session()
    return bot


Q48_TOOL = _tool_call("compare_metrics", {"metrics": ["total_pelayanan"], "dimensions": ["divre", "jenis_pelayanan"]})
Q48 = "Bandingkan jumlah pelayanan RJTL dan RITL per DIVRE pada tahun 2023 dan 2024."


def test_q48_wrong_number_is_corrected_by_model(monkeypatch):
    monkeypatch.setenv("ANSWER_NUMBER_MODE", "correct")
    bot = _q48_bot([
        _message(tool_calls=[Q48_TOOL]),
        _message(content="Tahun 2023 RJTL DIVRE 01 40.734, RITL hanya 6.705."),
        _message(content="Tahun 2023 RJTL DIVRE 01 40.734, RITL 7.305."),
    ])
    answer = asyncio.run(bot.ask(Q48))

    correction = bot.ollama_client.calls[2]["messages"][-1]["content"]
    assert "6.705" in correction
    assert answer.startswith("Tahun 2023 RJTL DIVRE 01 40.734, RITL 7.305.")
    assert "6.705" not in answer and "Periksa" not in answer
    # Riwayat tetap ringkas: koreksi tidak ikut tersimpan.
    assert [m["role"] for m in bot.messages] == ["system", "user", "assistant"]


def test_q48_still_wrong_narrative_is_hidden(monkeypatch):
    monkeypatch.setenv("ANSWER_NUMBER_MODE", "correct")
    bot = _q48_bot([
        _message(tool_calls=[Q48_TOOL]),
        _message(content="RITL 2023 hanya 6.705."),
        _message(content="RITL 2023 sebesar 6.750."),
    ])
    answer = asyncio.run(bot.ask(Q48))

    assert answer.startswith(HIDDEN_NARRATIVE_MESSAGE)
    assert "6.705" not in answer and "6.750" not in answer
    assert "DIVRE 01 / RITL / 2023: Total Pelayanan 7.305" in answer


def test_correct_narrative_needs_no_extra_call():
    bot = _q48_bot([
        _message(tool_calls=[Q48_TOOL]),
        _message(content="RJTL DIVRE 01 tahun 2023 sebesar 40.734."),
    ])
    answer = asyncio.run(bot.ask(Q48))
    assert len(bot.ollama_client.calls) == 2
    assert answer.startswith("RJTL DIVRE 01 tahun 2023 sebesar 40.734.")


def test_q49_service_mix_call_is_rerouted_end_to_end():
    calls = []

    class RecordingSession:
        async def call_tool(self, name, arguments):
            calls.append((name, arguments))
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(SHARE_PAYLOAD[1]))])

    bot = _bot([
        _message(tool_calls=[_tool_call("service_mix", {"filters": {"kelompok_umur": ["0-14", "65+"], "tahun_pelayanan": 2024}})]),
        _message(content="Persentase per kelompok umur tersedia di data resmi."),
    ])
    bot.session = RecordingSession()
    asyncio.run(bot.ask("Berapa persentase pelayanan RITL dibandingkan RJTL di setiap kelompok umur pada tahun 2024?"))

    assert calls[0][0] == "metric_share"
    assert calls[0][1]["within"] == ["kelompok_umur"]
    assert bot.last_trace[0]["tool"] == "metric_share"


def test_chatbot_exposes_charts_from_official_data():
    bot = _bot([
        _message(tool_calls=[_tool_call("metric_share", {"metric": "peserta", "dimensions": ["jenis_kelamin"]})]),
        _message(content="Perempuan 700.000 peserta."),
    ])
    asyncio.run(bot.ask("Bagaimana distribusi peserta berdasarkan jenis kelamin?"))
    assert len(bot.last_charts) == 1
    assert bot.last_charts[0]["series"][0]["values"] == [700000.0, 534567.0]


def test_no_charts_when_no_data():
    bot = _bot([_message(content="Halo!")])
    asyncio.run(bot.ask("halo"))
    assert bot.last_charts == []


# ---------------- v1.3.2: format angka campuran & ringkas ----------------

COST_DIVRE = ("compare_costs", {
    "success": True, "dimensions": ["divre"],
    "results": {"total_biaya_tagihan": "4000000000000.00", "total_biaya_verifikasi": "3900000000000.00",
                "selisih_tagihan_verifikasi": "100000000000.00", "persentase_selisih_dari_tagihan": "2.5"},
    "breakdown": [{"divre": "DIVRE 03", "total_biaya_tagihan": "360208780419.67",
                   "total_biaya_verifikasi": "355199999921.53",
                   "selisih_tagihan_verifikasi": "5008780498.14",
                   "persentase_selisih_dari_tagihan": "1.39"}],
})


@pytest.mark.parametrize("text", [
    "Biaya verifikasi Rp 355.199.999.921.53",      # campuran (kasus nyata)
    "Selisih Rp 5.008.780.498.14 (1.39% dari tagihan)",
    "Biaya verifikasi Rp 355.199.999.921,53",      # Indonesia
    "Biaya verifikasi Rp 355,199,999,921.53",      # Inggris
    "Biaya verifikasi Rp 355,20 miliar",           # ringkas
    "Selisih sekitar Rp 5,01 miliar",
])
def test_mixed_and_compact_formats_are_recognised(text):
    assert find_unverified_numbers(text, [COST_DIVRE]) == []


def test_wrong_value_in_mixed_format_still_flagged():
    assert find_unverified_numbers("Rp 355.199.999.999.53", [COST_DIVRE]) != []


def test_prompt_asks_for_compact_rupiah():
    from ai.prompts import CORE_PROMPT
    assert "miliar" in CORE_PROMPT


# ---------------- v1.3.3: angka siap-salin untuk Qwen ----------------

from ai.answer_support import format_payload_for_model  # noqa: E402

REAL_COSTS = {
    "success": True, "operation": "compare_costs", "filters": {"tahun_pelayanan": 2024},
    "results": {"total_biaya_tagihan": "3231380226911.95", "total_biaya_verifikasi": "3187264059312.40",
                "selisih_tagihan_verifikasi": "44116167599.55", "persentase_selisih_dari_tagihan": "1.3652"},
    "dimensions": ["divre"],
    "breakdown": [{"divre_code": "01", "divre": "DIVRE 01", "total_biaya_tagihan": "256297513482.10",
                   "total_biaya_verifikasi": "251903448126.37", "selisih_tagihan_verifikasi": "4394065355.73",
                   "persentase_selisih_dari_tagihan": "1.71", "peringkat_total_biaya_verifikasi": 1}],
}


def test_model_sees_ready_to_copy_rupiah():
    formatted = format_payload_for_model(REAL_COSTS)
    results = formatted["results"]
    assert results["total_biaya_verifikasi"] == "Rp 3.187.264.059.312,40 (≈ Rp 3,19 triliun)"
    assert results["total_biaya_tagihan"] == "Rp 3.231.380.226.911,95 (≈ Rp 3,23 triliun)"
    assert results["selisih_tagihan_verifikasi"] == "Rp 44.116.167.599,55 (≈ Rp 44,12 miliar)"
    assert results["persentase_selisih_dari_tagihan"] == "1,37%"
    row = formatted["breakdown"][0]
    assert row["divre_code"] == "01" and row["peringkat_total_biaya_verifikasi"] == 1
    assert formatted["filters"] == {"tahun_pelayanan": 2024}
    # Payload asli tidak berubah (dipakai tabel resmi, grafik, pemeriksa).
    assert REAL_COSTS["results"]["total_biaya_verifikasi"] == "3187264059312.40"


def test_copied_formatted_numbers_pass_check_and_100x_error_is_caught():
    payloads = [("compare_costs", REAL_COSTS)]
    good = ("Total verifikasi Rp 3.187.264.059.312,40 (≈ Rp 3,19 triliun), "
            "selisih Rp 44,12 miliar atau 1,37%.")
    assert find_unverified_numbers(good, payloads) == []
    bad = "Total verifikasi Rp 318.726.405.931.240,00"  # kesalahan nyata (100x)
    assert find_unverified_numbers(bad, payloads) == ["Rp 318.726.405.931.240,00"] or \
        find_unverified_numbers(bad, payloads)


def test_counts_percentages_and_growth_formatting():
    share = format_payload_for_model({
        "metric": "total_peserta_unik", "grand_total": 1227146,
        "results": [{"segmen_peserta": "PBI APBD", "total_peserta_unik": 953216,
                     "persentase": "77.68", "persentase_dalam_kelompok": "40.69", "total_kelompok": 72534}],
    })
    assert share["grand_total"] == "1.227.146"
    assert share["results"][0] == {"segmen_peserta": "PBI APBD", "total_peserta_unik": "953.216",
                                   "persentase": "77,68%", "persentase_dalam_kelompok": "40,69%",
                                   "total_kelompok": "72.534"}
    growth = format_payload_for_model({
        "operation": "metric_growth", "metric": "rata_rata_biaya_verifikasi",
        "current_year": 2024, "previous_year": 2023,
        "results": {"previous_value": "800.1234", "current_value": "812.5",
                    "difference": "12.3766", "growth_percentage": "1.5468"}})
    assert growth["current_year"] == 2024
    assert growth["results"]["previous_value"] == "Rp 800,12"
    assert growth["results"]["growth_percentage"] == "1,55%"
    service = format_payload_for_model({
        "operation": "service_growth", "current_year": 2024, "previous_year": 2023,
        "results": {"previous_total": 409658, "current_total": 409781, "difference": "123",
                    "growth_percentage": "0.03"}})
    assert service["results"]["previous_total"] == "409.658"
    assert service["results"]["difference"] == "123"


def test_compare_metrics_summary_and_labels():
    formatted = format_payload_for_model({
        "metrics": ["total_pelayanan", "total_biaya_verifikasi"],
        "labels": {"total_pelayanan": "Total Pelayanan", "total_biaya_verifikasi": "Total Biaya Verifikasi"},
        "totals": {"total_pelayanan": 2500000, "total_biaya_verifikasi": "3187264059312.40"},
        "summary": {"total_biaya_verifikasi": {"tertinggi": {"kategori": "DIVRE 01", "nilai": "251903448126.37"}}},
    })
    assert formatted["labels"]["total_pelayanan"] == "Total Pelayanan"
    assert formatted["totals"]["total_pelayanan"] == "2.500.000"
    assert formatted["summary"]["total_biaya_verifikasi"]["tertinggi"]["nilai"].startswith("Rp 251.903.448.126,37")


def test_qwen_receives_formatted_tool_output():
    class CostSession:
        async def call_tool(self, name, arguments):
            return SimpleNamespace(content=[SimpleNamespace(text=json.dumps(REAL_COSTS))])

    bot = _bot([
        _message(tool_calls=[_tool_call("compare_costs", {"dimensions": ["divre"]})]),
        _message(content="Total biaya verifikasi Rp 3,19 triliun."),
    ])
    bot.session = CostSession()
    answer = asyncio.run(bot.ask("Bagaimana perbandingan biaya tagihan dan biaya verifikasi berdasarkan DIVRE?"))

    tool_message = [m for m in bot.ollama_client.calls[1]["messages"]
                    if isinstance(m, dict) and m.get("role") == "tool"][0]
    assert "Rp 3.187.264.059.312,40" in tool_message["content"]
    assert "3187264059312.40" not in tool_message["content"]
    assert "Periksa" not in answer
    # Tabel resmi tetap dari angka asli.
    assert "Rp 3.187.264.059.312,40" in answer


def test_model_number_format_can_be_disabled(monkeypatch):
    monkeypatch.setenv("TOOL_OUTPUT_FORMAT_NUMBERS", "0")
    compact = json.loads(compact_tool_output(json.dumps(REAL_COSTS)))
    assert compact["results"]["total_biaya_verifikasi"] == "3187264059312.40"
