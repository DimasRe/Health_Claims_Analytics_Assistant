"""Uji grafik: dibuat dari data resmi, konsisten, dan sesuai bentuk data."""

import json
from decimal import Decimal
from pathlib import Path

import pytest
import yaml

from ai.charts import build_charts

BANK = yaml.safe_load(
    (Path(__file__).parent / "questions" / "question_bank.yaml").read_text(encoding="utf-8")
)["questions"]


def _numbers_in(payload):
    found = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, bool) or value is None:
            return
        else:
            try:
                found.add(round(float(Decimal(str(value))), 4))
            except Exception:
                pass

    walk(payload)
    return found


def assert_chart_values_from_payload(charts, payloads):
    allowed = set()
    for _, payload in payloads:
        allowed |= _numbers_in(payload)
    for chart in charts:
        for series in chart["series"]:
            assert len(series["values"]) == len(chart["labels"])
            for value in series["values"]:
                if value is not None:
                    assert round(value, 4) in allowed, (chart["title"], value)


# ---------------- bentuk grafik per jenis data ----------------

def test_single_dimension_bar():
    payload = {"metric": "total_pelayanan", "dimensions": ["divre"], "filters": {},
               "results": [{"divre_code": "01", "divre": "DIVRE 01", "total_pelayanan": 192734},
                           {"divre_code": "02", "divre": "DIVRE 02", "total_pelayanan": 180559}]}
    [chart] = build_charts([("analyze_data", payload)])
    assert chart["kind"] == "bar" and chart["horizontal"] is True
    assert chart["labels"] == ["DIVRE 01", "DIVRE 02"]
    assert chart["series"][0]["values"] == [192734.0, 180559.0]
    assert chart["subtitle"] == "Seluruh data"


def test_year_dimension_becomes_line_with_series():
    rows = [{"tahun_pelayanan": y, "jenis_pelayanan": j, "total_pelayanan": v}
            for y, j, v in [(2024, "RJTL", 10), (2023, "RJTL", 9), (2023, "RITL", 3), (2024, "RITL", 4)]]
    [chart] = build_charts([("analyze_data", {"metric": "total_pelayanan",
                                              "dimensions": ["jenis_pelayanan", "tahun_pelayanan"],
                                              "filters": {}, "results": rows})])
    assert chart["kind"] == "line"
    assert chart["labels"] == ["2023", "2024"]
    assert {s["name"]: s["values"] for s in chart["series"]} == {"RJTL": [9.0, 10.0], "RITL": [3.0, 4.0]}


def test_q48_three_dimensions_grouped():
    rows = []
    for divre in range(1, 13):
        for jenis in ("RJTL", "RITL"):
            for tahun in (2023, 2024):
                rows.append({"divre": f"DIVRE {divre:02d}", "jenis_pelayanan": jenis,
                             "tahun_pelayanan": tahun, "total_pelayanan": divre * 100 + tahun % 10})
    [chart] = build_charts([("compare_metrics", {
        "metrics": ["total_pelayanan"], "labels": {"total_pelayanan": "Total Pelayanan"},
        "dimensions": ["divre", "jenis_pelayanan", "tahun_pelayanan"], "filters": {}, "results": rows,
    })])
    # 4 seri (DIVRE x jenis terlalu banyak -> tukar sumbu): kategori DIVRE/jenis, seri = tahun.
    assert chart["kind"] in ("line", "grouped_bar")
    total_points = sum(len([v for v in s["values"] if v is not None]) for s in chart["series"])
    assert total_points == 48 or chart["notes"]


def test_within_share_is_stacked_percent():
    rows = [{"kelompok_umur": k, "jenis_pelayanan": j, "total_pelayanan": v,
             "persentase": "1", "persentase_dalam_kelompok": p}
            for k, j, v, p in [("0-5", "RJTL", 80, "80.00"), ("0-5", "RITL", 20, "20.00"),
                               ("6-10", "RJTL", 70, "70.00"), ("6-10", "RITL", 30, "30.00")]]
    [chart] = build_charts([("metric_share", {"metric": "total_pelayanan",
                                              "dimensions": ["kelompok_umur", "jenis_pelayanan"],
                                              "within": ["kelompok_umur"], "filters": {"tahun_pelayanan": 2024},
                                              "results": rows, "additive": True})])
    assert chart["kind"] == "stacked_percent" and chart["unit"] == "percent"
    assert chart["labels"] == ["0-5", "6-10"]
    assert {s["name"]: s["values"] for s in chart["series"]} == {"RJTL": [80.0, 70.0], "RITL": [20.0, 30.0]}
    assert chart["subtitle"] == "Filter: tahun_pelayanan = 2024"


def test_q50_top_per_group_has_names():
    rows = [{"kelompok_umur": k, "diagnosis_primer_code": c, "diagnosis_primer": n,
             "total_pelayanan": v, "peringkat": r}
            for k, c, n, v, r in [("0 - 05", "Z098", "Follow-up", 150065, 1), ("0 - 05", "R509", "Fever", 8822, 2),
                                  ("76++", "Z098", "Follow-up", 90844, 1), ("76++", "K30", "Dyspepsia", 4552, 2)]]
    [chart] = build_charts([("top_categories", {"metric": "total_pelayanan", "dimensions": ["diagnosis_primer"],
                                                "group_by": ["kelompok_umur"], "filters": {}, "results": rows})])
    assert chart["kind"] == "grouped_bar"
    assert chart["labels"] == ["0 - 05", "76++"]
    second = [s for s in chart["series"] if s["name"] == "#2"][0]
    assert second["values"] == [8822.0, 4552.0]
    assert second["names"] == ["Fever", "Dyspepsia"]


def test_mixed_units_never_share_an_axis():
    rows = [{"divre": "A", "total_pelayanan": 10, "rata_rata_biaya_verifikasi": "1500.5"},
            {"divre": "B", "total_pelayanan": 20, "rata_rata_biaya_verifikasi": "900.25"}]
    charts = build_charts([("compare_metrics", {
        "metrics": ["total_pelayanan", "rata_rata_biaya_verifikasi"], "labels": {},
        "dimensions": ["divre"], "filters": {}, "results": rows})])
    assert len(charts) == 2
    assert {c["unit"] for c in charts} == {"count", "idr"}
    assert all(len(c["series"]) == 1 for c in charts)


def test_growth_chart():
    [chart] = build_charts([("metric_growth", {
        "metric": "total_biaya_verifikasi", "previous_year": 2023, "current_year": 2024,
        "results": {"previous_value": "100", "current_value": "110", "difference": "10",
                    "growth_percentage": "10"}})])
    assert chart["labels"] == ["2023", "2024"]
    assert chart["series"][0]["values"] == [100.0, 110.0]
    assert chart["unit"] == "idr"
    assert "+10,00%" in chart["notes"][0]


def test_category_cap_and_note(monkeypatch):
    monkeypatch.setenv("CHART_MAX_CATEGORIES", "5")
    rows = [{"cabang": f"C{i}", "total_pelayanan": 100 - i} for i in range(12)]
    [chart] = build_charts([("analyze_data", {"metric": "total_pelayanan", "dimensions": ["cabang"],
                                              "filters": {}, "results": rows})])
    assert len(chart["labels"]) == 5
    assert "5 dari 12" in chart["notes"][0]


def test_no_chart_for_single_number_or_disabled(monkeypatch):
    payload = {"metric": "total_pelayanan", "dimensions": [], "filters": {}, "results": [{"total_pelayanan": 5}]}
    assert build_charts([("analyze_data", payload)]) == []
    assert build_charts([("inspect_data", {"metrics": []})]) == []
    monkeypatch.setenv("ANSWER_CHARTS", "0")
    rows = [{"divre": "A", "total_pelayanan": 1}]
    assert build_charts([("analyze_data", {"metric": "total_pelayanan", "dimensions": ["divre"],
                                           "filters": {}, "results": rows})]) == []


def test_payload_not_mutated():
    rows = [{"kelompok_umur": "0-5", "diagnosis_primer": "A", "total_pelayanan": 1, "peringkat": 1}]
    payload = {"metric": "total_pelayanan", "dimensions": ["diagnosis_primer"],
               "group_by": ["kelompok_umur"], "filters": {}, "results": rows}
    build_charts([("top_categories", payload)])
    assert "_rank" not in rows[0]


# ---------------- semua pertanyaan bank (database) ----------------

@pytest.mark.db
@pytest.mark.parametrize("item", BANK, ids=[f"Q{i['id']:03d}" for i in BANK])
def test_bank_charts_match_official_data(item, database_available):
    from ai.qwen_mcp_client import normalize_tool_arguments
    from mcp_server import server

    payloads = []
    for call in item["reference_calls"]:
        args = normalize_tool_arguments(call["tool"], dict(call.get("arguments") or {}), item["question"])
        payloads.append((call["tool"], json.loads(getattr(server, call["tool"])(**args))))

    charts = build_charts(payloads)
    json.dumps(charts)  # harus bisa dikirim lewat API

    single_number = all(
        call["tool"] == "inspect_data"
        or (call["tool"] == "analyze_data" and not (call.get("arguments") or {}).get("dimensions")
            and not payloads[i][1].get("dimensions"))
        for i, call in enumerate(item["reference_calls"])
    )
    if not single_number:
        assert charts, f"Q{item['id']} tidak menghasilkan grafik"
    assert len(charts) <= 3
    assert_chart_values_from_payload(charts, payloads)


def test_distinct_within_share_is_not_stacked():
    # Peserta unik: P + L dalam satu segmen bisa > 100% -> jangan ditumpuk.
    rows = [{"segmen_peserta": "PBI", "jenis_kelamin": j, "total_peserta_unik": v,
             "persentase": "1", "persentase_dalam_kelompok": p}
            for j, v, p in [("P", 800, "80.00"), ("L", 700, "70.00")]]
    [chart] = build_charts([("metric_share", {"metric": "total_peserta_unik",
                                              "dimensions": ["segmen_peserta", "jenis_kelamin"],
                                              "within": ["segmen_peserta"], "filters": {},
                                              "results": rows, "additive": False})])
    assert chart["kind"] == "grouped_bar"
    assert chart["unit"] == "percent"
    assert any("lebih dari 100%" in note for note in chart["notes"])
