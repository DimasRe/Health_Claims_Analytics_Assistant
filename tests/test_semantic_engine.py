"""
Uji Semantic Engine (tanpa database).

Dapat dijalankan dengan pytest, atau sebagai skrip seperti versi lama:
    python -m tests.test_semantic_engine
"""

import pytest

from mcp_server.semantic_engine import (
    SemanticQueryError,
    build_query,
    normalize_filter_key,
    normalize_filter_value,
    load_config,
)


# Kasus uji asli (dipertahankan)
CASES = [
    (
        "TEST 1 - PELAYANAN TAHUN 2024",
        "total_pelayanan",
        ["jenis_pelayanan"],
        {"tahun_pelayanan": 2024},
    ),
    (
        "TEST 2 - BIAYA VERIFIKASI",
        "total_biaya_verifikasi",
        ["jenis_pelayanan"],
        None,
    ),
    (
        "TEST 3 - DISTRIBUSI DIVRE",
        "total_pelayanan",
        ["divre"],
        None,
    ),
    (
        "TEST 4 - BIAYA PER REGIONAL TARIF",
        "total_biaya_verifikasi",
        ["regional_tarif"],
        None,
    ),
    (
        "TEST 5 - PELAYANAN RJTL/RITL TAHUN 2024",
        "total_pelayanan",
        ["jenis_pelayanan"],
        {
            "tahun_pelayanan": 2024,
            "jenis_pelayanan": "RJTL",
        },
    ),
]


def show_query(title, metric, dimensions=None, filters=None):
    print("\n" + "=" * 60)
    print(title)
    print("=" * 60)

    sql, params = build_query(
        metric=metric,
        dimensions=dimensions or [],
        filters=filters or {},
        limit=20,
    )

    print("\nSQL:")
    print(sql)

    print("\nPARAMETERS:")
    print(params)

    return sql, params


@pytest.mark.parametrize("title, metric, dimensions, filters", CASES)
def test_original_cases_build_valid_sql(title, metric, dimensions, filters):
    sql, params = build_query(metric, dimensions, filters or {}, 20)

    assert sql.startswith("SELECT ")
    assert "FROM public.claims AS t" in sql
    assert f'AS "{metric}"' in sql
    assert params[-1] == 20
    for dim in dimensions:
        assert f'"{dim}"' in sql


def test_year_filter_uses_expression_and_param():
    sql, params = build_query(
        "total_pelayanan", ["jenis_pelayanan"], {"tahun_pelayanan": 2024}
    )
    assert 'EXTRACT(YEAR FROM t."service_date")::integer = %s' in sql
    assert params == [2024, 20]


def test_divre_dimension_uses_lookup_join():
    sql, _ = build_query("total_pelayanan", ["divre"])
    assert "LEFT JOIN public.regions AS r" in sql
    assert 'r."region_code" AS "divre_code"' in sql


def test_divre_code_filter_uses_source_column_without_join():
    sql, params = build_query("total_pelayanan", [], {"divre_code": 10})
    assert 't."service_region_code" = %s' in sql
    assert "LEFT JOIN" not in sql
    assert params[0] == "10"


@pytest.mark.parametrize("value", ["DIVRE 10", "divisi regional 10", "10", 10])
def test_divre_code_auto_detection(value):
    sql, params = build_query("total_pelayanan", [], {"divre": value})
    assert 't."service_region_code" = %s' in sql
    assert params[0] == "10"


def test_divre_label_filter_uses_lookup_label():
    sql, params = build_query("total_pelayanan", [], {"divre": "DIVRE X"})
    assert 'r."region_name" = %s' in sql
    assert "LEFT JOIN public.regions" in sql
    assert params[0] == "DIVRE X"


@pytest.mark.parametrize("alias", ["nama_divre", "nama_divisi_regional"])
def test_yaml_label_aliases_are_supported(alias):
    # Sebelumnya alias dari dimensions.yaml tidak terbaca.
    sql, _ = build_query("total_pelayanan", [], {alias: "DIVRE I"})
    assert 'r."region_name" = %s' in sql


def test_filter_key_aliases():
    assert normalize_filter_key("kode_rs") == ("fasilitas_kesehatan", "code")
    assert normalize_filter_key("Tahun") == ("tahun_pelayanan", "default")
    assert normalize_filter_key("jenis pelayanan") == ("jenis_pelayanan", "default")


def test_filter_value_normalization():
    assert normalize_filter_value("divre", "code", 1) == "01"
    assert normalize_filter_value("divre", "code", ["DIVRE 2", "3"]) == ["02", "03"]
    assert normalize_filter_value("cabang", "code", 121) == "121"
    assert normalize_filter_value("cabang", "code", "0121") == "0121"


def test_multi_year_breakdown_is_chronological():
    sql, params = build_query(
        "total_pelayanan",
        ["tahun_pelayanan"],
        {"tahun_pelayanan": [2023, 2022, 2023]},
        100,
    )
    assert "IN (%s, %s)" in sql  # duplikat dihapus
    assert 'ORDER BY "tahun_pelayanan" ASC' in sql
    assert params == [2023, 2022, 100]


def test_month_breakdown_is_chronological():
    sql, _ = build_query(
        "total_pelayanan", ["tahun_pelayanan", "bulan_pelayanan"]
    )
    assert 'ORDER BY "tahun_pelayanan" ASC, "bulan_pelayanan" ASC' in sql


def test_non_time_order_has_tie_breaker():
    sql, _ = build_query("total_pelayanan", ["segmen_peserta"])
    assert 'ORDER BY "total_pelayanan" DESC NULLS LAST, t."member_segment"' in sql


@pytest.mark.parametrize(
    "kwargs, message",
    [
        (dict(metric="tidak_ada"), "Metrik tidak dikenal"),
        (dict(metric="total_pelayanan", dimensions=["tidak_ada"]), "Dimensi tidak dikenal"),
        (dict(metric="total_biaya_tagihan", dimensions=["diagnosis_sekunder"]), "multi-nilai"),
        (dict(metric="total_pelayanan", sort_by="random"), "sort_by"),
        (dict(metric="total_pelayanan", order="naik"), "order"),
        (dict(metric="total_pelayanan", dimensions="segmen_peserta"), "list"),
        (dict(metric="total_pelayanan", filters=[]), "dictionary"),
        (dict(metric="total_pelayanan", limit=0), "Limit"),
        (dict(metric="total_pelayanan", limit=101), "Limit"),
        (dict(metric="total_pelayanan", limit=True), "integer"),
        (dict(metric="total_pelayanan", dimensions=["divre", "divre"]), "duplikat"),
        (dict(metric="total_pelayanan", dimensions=[1]), "teks"),
        (dict(metric="total_pelayanan", filters={"jenis_pelayanan": []}), "kosong"),
        (dict(metric="total_pelayanan", filters={"jenis_pelayanan": None}), "kosong"),
        (dict(metric="total_pelayanan", filters={"jenis_kelamin": [f"L{i}" for i in range(101)]}), "Terlalu banyak"),
        (dict(metric="total_pelayanan", filters={"segmen_peserta": "X", "kode_x": 1}), "Dimensi tidak dikenal"),
    ],
)
def test_invalid_requests_are_rejected(kwargs, message):
    with pytest.raises(SemanticQueryError, match=message):
        build_query(**kwargs)


def test_sensitive_columns_cannot_be_used():
    from mcp_server.semantic_engine import quote_identifier

    for column in ("member_id", "claim_id", "doctor_name"):
        with pytest.raises(SemanticQueryError, match="sensitif"):
            quote_identifier(column)


def test_sql_injection_in_values_is_parameterized():
    malicious = "RJTL'; DROP TABLE public.claims; --"
    sql, params = build_query("total_pelayanan", [], {"jenis_pelayanan": malicious})
    assert "DROP" not in sql
    assert params[0] == malicious


def test_config_is_cached():
    assert load_config() is load_config()


if __name__ == "__main__":
    for case in CASES:
        show_query(*case)
