"""
Uji integrasi Semantic Engine + PostgreSQL.

Tidak memakai angka acuan yang ditanam di kode: setiap test memeriksa
konsistensi (jumlah rincian per kategori == total keseluruhan), sehingga
berlaku untuk dataset apa pun, termasuk data sintetis dari
`scripts/generate_sample_data.py`.

    RUN_DATASET_TESTS=1 pytest tests/test_semantic_execution.py
"""

import os

import pytest

from mcp_server.semantic_engine import build_query
from mcp_server.db import execute_query


pytestmark = [
    pytest.mark.db,
    pytest.mark.dataset,
    pytest.mark.skipif(
        os.getenv("RUN_DATASET_TESTS") != "1",
        reason="Set RUN_DATASET_TESTS=1 untuk menguji dataset di database.",
    ),
]


def run(metric, dimensions=None, filters=None, limit=100):
    sql, params = build_query(
        metric=metric,
        dimensions=dimensions or [],
        filters=filters or {},
        limit=limit,
    )
    return execute_query(sql, params)


def grand_total(metric, filters=None):
    rows = run(metric, filters=filters)
    assert len(rows) == 1
    return rows[0][metric]


@pytest.mark.parametrize("dimension", ["jenis_pelayanan", "divre", "regional_tarif"])
def test_rincian_pelayanan_sama_dengan_total(database_available, dimension):
    rows = run("total_pelayanan", [dimension])
    assert sum(r["total_pelayanan"] for r in rows) == grand_total("total_pelayanan")


def test_rincian_biaya_verifikasi_sama_dengan_total(database_available):
    rows = run("total_biaya_verifikasi", ["jenis_pelayanan"])
    total = sum(r["total_biaya_verifikasi"] for r in rows)
    assert total == grand_total("total_biaya_verifikasi")


def test_filter_tahun_konsisten(database_available):
    per_year = run("total_pelayanan", ["tahun_pelayanan"])
    assert per_year, "Dataset kosong"
    year = per_year[0]["tahun_pelayanan"]
    expected = per_year[0]["total_pelayanan"]
    rows = run("total_pelayanan", ["jenis_pelayanan"], {"tahun_pelayanan": year})
    assert sum(r["total_pelayanan"] for r in rows) == expected


def test_filter_jenis_pelayanan_satu_baris(database_available):
    rows = run("total_pelayanan", ["jenis_pelayanan"], {"jenis_pelayanan": "RJTL"})
    assert len(rows) == 1
    assert rows[0]["total_pelayanan"] > 0
