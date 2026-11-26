"""
Uji tool MCP Server.
Uji validasi berjalan tanpa database; uji query berjalan jika
PostgreSQL tersedia (lihat .env) dan dilewati jika tidak.
"""

import json
from decimal import Decimal

import pytest

from mcp_server import server


def call(tool, **kwargs):
    return json.loads(getattr(server, tool)(**kwargs))


# ------------------------- tanpa database -------------------------

def test_inspect_data_catalog():
    data = call("inspect_data")
    keys = {metric["key"] for metric in data["metrics"]}
    assert "total_pelayanan" in keys and "total_peserta_unik" in keys
    assert data["security"]["raw_sql_allowed"] is False


@pytest.mark.parametrize("tool, kwargs, error", [
    ("analyze_data", dict(metric="bogus"), "SEMANTIC_VALIDATION_ERROR"),
    ("analyze_data", dict(metric="total_pelayanan", filters="x"), "SEMANTIC_VALIDATION_ERROR"),
    ("analyze_data", dict(metric="total_pelayanan", filters={"x": [True]}), "SEMANTIC_VALIDATION_ERROR"),
    ("analyze_data", dict(metric="total_pelayanan", limit=1000), "SEMANTIC_VALIDATION_ERROR"),
    ("compare_costs", dict(filters={"jenis_pelayanan": {"a": 1}}), "SEMANTIC_VALIDATION_ERROR"),
    ("service_mix", dict(filters=[1]), "SEMANTIC_VALIDATION_ERROR"),
    ("service_growth", dict(current_year=2023, previous_year=2023), "INVALID_REQUEST"),
    ("service_growth", dict(current_year="2023", previous_year=2022), "INVALID_REQUEST"),
    ("metric_growth", dict(metric="selisih_tagihan_verifikasi", current_year=2023, previous_year=2022), "INVALID_REQUEST"),
    ("metric_growth", dict(metric="nope", current_year=2023, previous_year=2022), "INVALID_REQUEST"),
    ("metric_share", dict(metric="total_pelayanan"), "INVALID_REQUEST"),
    ("metric_share", dict(metric="rata_rata_biaya_verifikasi", dimensions=["segmen_peserta"]), "INVALID_REQUEST"),
    ("metric_share", dict(metric="bogus", dimensions=["segmen_peserta"]), "SEMANTIC_VALIDATION_ERROR"),
])
def test_validation_errors(tool, kwargs, error):
    data = call(tool, **kwargs)
    assert data["success"] is False
    assert data["error"] == error


def test_database_errors_are_hidden(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("password authentication failed for user secret")

    monkeypatch.setattr(server, "execute_query", broken)
    data = call("analyze_data", metric="total_pelayanan")
    assert data["error"] == "QUERY_EXECUTION_ERROR"
    assert "secret" not in data["message"]


def test_tool_schemas_are_preserved():
    import asyncio

    tools = {tool.name: tool for tool in asyncio.run(server.server.list_tools())}
    assert set(tools) == {
        "inspect_data", "analyze_data", "compare_costs", "service_mix",
        "service_growth", "metric_growth", "metric_share",
        "compare_metrics", "top_categories", "profile_summary",
    }
    schema = tools["analyze_data"].input_schema
    assert schema["required"] == ["metric"]
    # Parameter lama tetap ada; parameter baru bersifat opsional.
    assert {"metric", "dimensions", "filters", "limit"} <= set(schema["properties"])
    assert tools["compare_costs"].input_schema.get("required", []) == []
    assert tools["service_mix"].input_schema.get("required", []) == []
    assert set(tools["metric_growth"].input_schema["required"]) == {
        "metric", "current_year", "previous_year",
    }


# ------------------------- dengan database -------------------------

@pytest.mark.db
def test_service_mix_percentages_sum_to_100(database_available):
    data = call("service_mix")
    assert data["success"] is True
    total = sum(Decimal(row["persentase"]) for row in data["results"])
    assert abs(total - 100) < Decimal("0.0001")


@pytest.mark.db
def test_compare_costs_difference(database_available):
    data = call("compare_costs")["results"]
    assert Decimal(data["selisih_tagihan_verifikasi"]) == (
        Decimal(data["total_biaya_tagihan"]) - Decimal(data["total_biaya_verifikasi"])
    )


@pytest.mark.db
def test_metric_share_matches_grand_total(database_available):
    share = call("metric_share", metric="total_pelayanan", dimensions=["jenis_pelayanan"])
    total = call("analyze_data", metric="total_pelayanan")
    assert share["grand_total"] == total["results"][0]["total_pelayanan"]
    assert share["additive"] is True
    distinct = call("metric_share", metric="total_peserta_unik", dimensions=["segmen_peserta"])
    assert "note" in distinct


@pytest.mark.db
def test_divre_code_and_label_filters_match(database_available):
    by_dim = call("analyze_data", metric="total_pelayanan", dimensions=["divre"], limit=100)
    row = by_dim["results"][0]
    by_code = call("analyze_data", metric="total_pelayanan", filters={"divre_code": row["divre_code"]})
    by_label = call("analyze_data", metric="total_pelayanan", filters={"nama_divre": row["divre"]})
    assert by_code["results"][0]["total_pelayanan"] == row["total_pelayanan"]
    assert by_label["results"][0]["total_pelayanan"] == row["total_pelayanan"]


@pytest.mark.db
def test_growth_tools_agree(database_available):
    years = call("analyze_data", metric="total_pelayanan", dimensions=["tahun_pelayanan"], limit=100)
    available = [row["tahun_pelayanan"] for row in years["results"]]
    if len(available) < 2:
        pytest.skip("Data kurang dari dua tahun.")
    previous_year, current_year = available[-2], available[-1]
    service = call("service_growth", current_year=current_year, previous_year=previous_year)
    metric = call("metric_growth", metric="total_pelayanan", current_year=current_year, previous_year=previous_year)
    if not service["success"]:
        assert service["error"] == "CALCULATION_ERROR"
        return
    assert Decimal(service["results"]["growth_percentage"]) == Decimal(metric["results"]["growth_percentage"])


@pytest.mark.parametrize("tool, kwargs, error", [
    ("metric_share", dict(metric="total_pelayanan", dimensions=["jenis_kelamin"], within=["segmen_peserta"]), "INVALID_REQUEST"),
    ("metric_share", dict(metric="total_pelayanan", dimensions=["jenis_kelamin"], within=["jenis_kelamin"]), "INVALID_REQUEST"),
    ("compare_metrics", dict(metrics=[]), "INVALID_REQUEST"),
    ("compare_metrics", dict(metrics=["total_pelayanan", "total_pelayanan"]), "SEMANTIC_VALIDATION_ERROR"),
    ("top_categories", dict(metric="total_pelayanan", dimensions=[]), "SEMANTIC_VALIDATION_ERROR"),
    ("top_categories", dict(metric="total_pelayanan", dimensions=["divre"], top_n=500), "SEMANTIC_VALIDATION_ERROR"),
    ("top_categories", dict(metric="total_biaya_tagihan", dimensions=["diagnosis_sekunder"]), "SEMANTIC_VALIDATION_ERROR"),
    ("top_categories", dict(metric="total_pelayanan", dimensions=["divre"], group_by=["divre"]), "SEMANTIC_VALIDATION_ERROR"),
    ("profile_summary", dict(dimensions=["diagnosis_sekunder"]), "INVALID_REQUEST"),
    ("analyze_data", dict(metric="total_pelayanan", dimensions=["divre"], order="naik"), "SEMANTIC_VALIDATION_ERROR"),
])
def test_new_tool_validation_errors(tool, kwargs, error):
    data = call(tool, **kwargs)
    assert data["success"] is False
    assert data["error"] == error


@pytest.mark.db
def test_within_percentages_sum_to_100_per_group(database_available):
    data = call(
        "metric_share", metric="total_pelayanan",
        dimensions=["kelompok_umur", "jenis_pelayanan"], within=["kelompok_umur"], limit=100,
    )
    sums = {}
    for row in data["results"]:
        sums.setdefault(row["kelompok_umur"], Decimal(0))
        sums[row["kelompok_umur"]] += Decimal(row["persentase_dalam_kelompok"])
    assert all(abs(total - 100) <= Decimal("0.05") for total in sums.values())


@pytest.mark.db
def test_compare_costs_breakdown_matches_overall(database_available):
    data = call("compare_costs", dimensions=["divre"], limit=100)
    total = sum(Decimal(row["total_biaya_verifikasi"]) for row in data["breakdown"])
    assert total == Decimal(data["results"]["total_biaya_verifikasi"])
