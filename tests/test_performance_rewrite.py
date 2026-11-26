"""
Optimasi performa harus menghasilkan angka yang SAMA dengan cara lama.

- Penulisan ulang COUNT(DISTINCT) -> SELECT DISTINCT + COUNT(*)
- Penggabungan metrik distinct dengan metrik lain di Python
- Cache hasil query
"""

import json

import pytest

from mcp_server import db, server
from mcp_server.semantic_engine import build_query, build_top_n_query


def run(sql_params):
    sql, params = sql_params
    return db._execute_uncached(sql, params)


@pytest.fixture
def no_cache(monkeypatch):
    monkeypatch.setenv("QUERY_CACHE_TTL", "0")


def test_rewrite_sql_shape(monkeypatch):
    sql, _ = build_query("total_peserta_unik", ["jenis_kelamin"])
    assert "SELECT DISTINCT" in sql
    assert "COUNT(DISTINCT" not in sql
    # Identitas peserta tidak pernah ada di SELECT luar.
    assert sql.split("FROM (")[0].count("member_id") == 0

    monkeypatch.setenv("DISTINCT_COUNT_REWRITE", "0")
    sql, _ = build_query("total_peserta_unik", ["jenis_kelamin"])
    assert 'COUNT(DISTINCT t."member_id")' in sql


CASES = [
    dict(metric="total_peserta_unik"),
    dict(metric="total_peserta_unik", dimensions=["jenis_kelamin"]),
    dict(metric="total_peserta_unik", dimensions=["kelompok_umur", "segmen_peserta"], limit=100),
    dict(metric="total_peserta_unik", dimensions=["divre"], filters={"jenis_pelayanan": "RITL"}),
    dict(metric="total_peserta_unik", dimensions=["tahun_pelayanan"], filters={"tahun_pelayanan": [2022, 2023]}),
    dict(metric="total_peserta_unik", dimensions=["segmen_peserta"], order="asc"),
    dict(metric="total_peserta_unik", dimensions=["diagnosis_sekunder"], limit=100),
    dict(metric="total_sjp_unik", dimensions=["kelas_hak"]),
]


@pytest.mark.db
@pytest.mark.parametrize("kwargs", CASES)
def test_rewrite_matches_count_distinct(kwargs, monkeypatch, database_available):
    fast = run(build_query(**kwargs))
    monkeypatch.setenv("DISTINCT_COUNT_REWRITE", "0")
    original = run(build_query(**kwargs))
    assert fast == original


@pytest.mark.db
@pytest.mark.parametrize("kwargs", [
    dict(metric="total_peserta_unik", dimensions=["kelompok_umur"], group_by=["jenis_kelamin"], top_n=2),
    dict(metric="total_peserta_unik", dimensions=["diagnosis_primer"], top_n=3, order="asc"),
])
def test_top_n_rewrite_matches(kwargs, monkeypatch, database_available):
    fast = run(build_top_n_query(**kwargs))
    monkeypatch.setenv("DISTINCT_COUNT_REWRITE", "0")
    original = run(build_top_n_query(**kwargs))
    assert fast == original


@pytest.mark.db
@pytest.mark.parametrize("tool, kwargs", [
    ("compare_metrics", dict(metrics=["total_pelayanan", "total_peserta_unik", "rata_rata_biaya_verifikasi"], dimensions=["segmen_peserta"])),
    ("compare_metrics", dict(metrics=["total_peserta_unik", "total_biaya_verifikasi"], dimensions=["kelompok_umur", "jenis_kelamin"], sort_by="total_peserta_unik", order="asc", limit=5)),
    ("compare_metrics", dict(metrics=["total_peserta_unik", "total_pelayanan"], dimensions=["tahun_pelayanan"])),
    ("profile_summary", dict()),
    ("metric_share", dict(metric="total_peserta_unik", dimensions=["jenis_kelamin", "kelompok_umur"], within=["jenis_kelamin"], limit=100)),
])
def test_tools_same_result_with_and_without_optimisation(tool, kwargs, monkeypatch, no_cache, database_available):
    fast = json.loads(getattr(server, tool)(**kwargs))
    monkeypatch.setenv("DISTINCT_COUNT_REWRITE", "0")
    original = json.loads(getattr(server, tool)(**kwargs))
    assert fast["success"] is True
    assert fast == original


@pytest.mark.db
def test_query_cache(monkeypatch, database_available):
    db.clear_query_cache()
    calls = []
    real = db._execute_uncached
    monkeypatch.setattr(db, "_execute_uncached", lambda *a: calls.append(1) or real(*a))

    sql, params = build_query("total_pelayanan", ["jenis_kelamin"])
    first = db.execute_query(sql, params)
    first[0]["total_pelayanan"] = -1  # mengubah hasil tidak merusak cache
    second = db.execute_query(sql, params)

    assert len(calls) == 1
    assert second[0]["total_pelayanan"] != -1

    monkeypatch.setenv("QUERY_CACHE_TTL", "0")
    db.execute_query(sql, params)
    assert len(calls) == 2


def test_work_mem_config(monkeypatch):
    monkeypatch.setenv("DB_WORK_MEM", "512MB")
    assert db.get_work_mem() == "512MB"
    monkeypatch.setenv("DB_WORK_MEM", "1; DROP")
    assert db.get_work_mem() == "256MB"
