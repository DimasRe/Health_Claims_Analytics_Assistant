"""
MCP Server untuk analisis data BPJS.

PENTING: server berjalan melalui transport stdio, sehingga stdout dipakai
untuk protokol MCP. Jangan gunakan print(); gunakan logger (stderr/file).
"""

import functools
import json
import logging
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from mcp_server.semantic_engine import (
    load_config,
    build_query,
    build_multi_metric_query,
    build_top_n_query,
    uses_split_dimension,
    get_metric_expression,
    resolve_dimension,
    SemanticQueryError,
    MAX_FILTER_VALUES,
    MAX_LIMIT,
    CHRONOLOGICAL_DIMENSIONS,
    DISTINCT_COUNT_ENTITIES,
    distinct_rewrite_enabled,
)

from mcp_server.db import execute_query

from mcp_server.calculation_engine import (
    calculate_difference,
    percentage,
    growth,
    to_decimal,
    round_decimal,
    format_indonesian_number,
    CalculationError,
)


# =====================================================
# LOGGING (stderr + file, tidak pernah stdout)
# =====================================================

LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

logger = logging.getLogger("bpjs.mcp_server")

if not logger.handlers:
    logger.setLevel(logging.INFO)
    _formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setLevel(logging.WARNING)
    _stderr_handler.setFormatter(_formatter)
    logger.addHandler(_stderr_handler)

    try:
        LOG_DIR.mkdir(exist_ok=True)
        _file_handler = logging.FileHandler(
            LOG_DIR / "mcp_server.log", encoding="utf-8"
        )
        _file_handler.setFormatter(_formatter)
        logger.addHandler(_file_handler)
    except OSError:
        pass

    logger.propagate = False


# =====================================================
# MCP SERVER
# =====================================================

server = MCPServer(
    name="bpjs-analytics",
    version="0.2.0",
    description="MCP Server untuk analisis data BPJS.",
    instructions=(
        "Gunakan inspect_data untuk melihat kemampuan analisis. "
        "Gunakan analyze_data untuk menjalankan analisis agregat. "
        "Gunakan metric_share untuk persentase/proporsi per kategori. "
        "Jangan meminta atau menampilkan identitas peserta."
    ),
)


def json_response(data):
    """Mengubah hasil menjadi JSON yang aman (Decimal -> string)."""

    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        default=str,
    )


def error_response(error_code, message):
    return json_response({
        "success": False,
        "error": error_code,
        "message": message,
    })


def validate_filters(filters):
    """
    Validasi bentuk filter yang dikirim model.
    Mengembalikan dictionary filter yang sudah aman.
    """

    filters = filters if filters is not None else {}

    if not isinstance(filters, dict):
        raise SemanticQueryError(
            "Format filters harus berupa dictionary."
        )

    for key, value in filters.items():
        values = (
            list(value)
            if isinstance(value, (list, tuple))
            else [value]
        )

        if not values or len(values) > MAX_FILTER_VALUES:
            raise SemanticQueryError(
                f"Jumlah nilai filter tidak valid: {key}"
            )

        for item in values:
            if (
                isinstance(item, bool)
                or not isinstance(item, (str, int, float))
            ):
                raise SemanticQueryError(
                    f"Tipe nilai filter tidak didukung: {key}"
                )

    return filters


def validate_years(current_year, previous_year):
    for year_name, year_value in (
        ("current_year", current_year),
        ("previous_year", previous_year),
    ):
        if isinstance(year_value, bool) or not isinstance(year_value, int):
            raise ValueError(f"{year_name} harus berupa bilangan tahun.")

    if current_year == previous_year:
        raise ValueError("Kedua tahun tidak boleh sama.")


def fetch_single_value(metric, filters, missing_message, empty_message):
    """Menjalankan query satu metrik tanpa dimensi dan mengambil nilainya."""

    sql, params = build_query(
        metric=metric,
        dimensions=[],
        filters=filters,
        limit=1,
    )

    rows = execute_query(sql, params)

    if len(rows) != 1:
        raise CalculationError(missing_message)

    value = rows[0].get(metric)

    if value is None:
        raise CalculationError(empty_message)

    return value


SPLIT_DIMENSION_NOTE = (
    "Dimensi multi-nilai (diagnosis sekunder) dipecah per nilai, sehingga "
    "jumlah dihitung per kemunculan diagnosis, bukan per pelayanan."
)


def get_metric_config(metric, metrics_config=None):
    if metrics_config is None:
        metrics_config, _ = load_config()
    metric_config = (metrics_config.get("metrics") or {}).get(metric)
    if not metric_config:
        raise SemanticQueryError(f"Metrik tidak dikenal: {metric}")
    return metric_config


def aggregation_type(metric, metrics_config=None):
    return (get_metric_config(metric, metrics_config).get("aggregation") or {}).get("type")


def row_label(row, dimensions):
    """Label kategori yang mudah dibaca, mis. 'DIVRE I / RJTL'."""
    return " / ".join(str(row.get(dim)) for dim in dimensions)


def row_key(row, dimensions):
    return tuple(row.get(dim) for dim in dimensions)


def format_metric_value(metric, value, metrics_config=None):
    """Format angka bergaya Indonesia sesuai satuan metrik."""
    if value is None:
        return "-"
    unit = str(get_metric_config(metric, metrics_config).get("unit") or "")
    if unit.startswith("IDR"):
        return "Rp " + format_indonesian_number(value, 2)
    if unit in ("records", "people"):
        return format_indonesian_number(value, 0)
    return format_indonesian_number(value, 2)


def _sort_rows(rows, metrics, dimensions, sort_by=None, order=None):
    """Pengurutan di Python dengan aturan yang sama seperti SQL builder."""
    if not rows:
        return rows

    dimension_columns = [key for key in rows[0] if key not in metrics]

    def dimension_key(row):
        return tuple(
            (row.get(column) is None, str(row.get(column)))
            for column in dimension_columns
        )

    if sort_by == "dimension":
        return sorted(rows, key=dimension_key, reverse=(order == "desc"))

    if sort_by is None and order is None:
        time_columns = [d for d in CHRONOLOGICAL_DIMENSIONS if d in dimensions]
        if time_columns:
            return sorted(
                rows,
                key=lambda row: tuple(
                    (row.get(c) is None, row.get(c) or 0) for c in time_columns
                ),
            )

    sort_metric = sort_by if sort_by in metrics else metrics[0]
    rows = sorted(rows, key=dimension_key)  # tie-breaker yang konsisten
    with_value = [row for row in rows if row.get(sort_metric) is not None]
    without_value = [row for row in rows if row.get(sort_metric) is None]
    with_value.sort(
        key=lambda row: to_decimal(row[sort_metric]),
        reverse=(order or "desc") == "desc",
    )
    return with_value + without_value


def execute_metrics_query(
    metrics, dimensions, filters, limit, sort_by=None, order=None
):
    """
    Menjalankan beberapa metrik sekaligus. Jika ada metrik distinct count
    (peserta/SJP unik) bersama metrik lain, metrik distinct dihitung dengan
    query terpisah versi cepat lalu digabung di Python per kategori.
    """

    distinct_metrics = [
        metric for metric in metrics if metric in DISTINCT_COUNT_ENTITIES
    ]

    if (
        len(metrics) == 1
        or not distinct_metrics
        or not distinct_rewrite_enabled()
    ):
        sql, params = build_multi_metric_query(
            metrics, dimensions=dimensions, filters=filters, limit=limit,
            sort_by=sort_by, order=order,
        )
        return execute_query(sql, params)

    other_metrics = [m for m in metrics if m not in distinct_metrics]
    metric_groups = ([other_metrics] if other_metrics else []) + [
        [metric] for metric in distinct_metrics
    ]

    # Validasi sort sesuai aturan query gabungan.
    build_multi_metric_query(
        metrics, dimensions=dimensions, filters=filters, limit=limit,
        sort_by=sort_by, order=order,
    )

    part_limit = MAX_LIMIT if dimensions else 1
    merged = {}

    for group in metric_groups:
        sql, params = build_multi_metric_query(
            group, dimensions=dimensions, filters=filters, limit=part_limit,
        )
        for row in execute_query(sql, params):
            dimension_values = {k: v for k, v in row.items() if k not in group}
            key = tuple(dimension_values.items())
            if key not in merged:
                merged[key] = {**dimension_values, **{m: None for m in metrics}}
            for metric in group:
                merged[key][metric] = row.get(metric)

    rows = _sort_rows(list(merged.values()), metrics, dimensions, sort_by, order)
    return rows[:limit]


def fetch_totals(metrics, filters):
    """Total keseluruhan beberapa metrik (query terpisah tanpa dimensi)."""
    rows = execute_metrics_query(metrics, dimensions=[], filters=filters, limit=1)
    if len(rows) != 1:
        raise CalculationError("Total keseluruhan tidak berhasil diperoleh.")
    return rows[0]


def fetch_group_totals(metric, group_dimensions, filters):
    """Total metrik per kelompok induk, untuk persentase di dalam kelompok."""
    sql, params = build_query(
        metric, dimensions=group_dimensions, filters=filters, limit=100,
        sort_by="dimension",
    )
    return {
        row_key(row, group_dimensions): row.get(metric)
        for row in execute_query(sql, params)
    }


def safe_percentage(part, whole):
    """Persentase 2 desimal; None jika tidak dapat dihitung."""
    if part is None or whole is None:
        return None
    try:
        return round_decimal(percentage(part, whole), 2)
    except CalculationError:
        return None


def add_ranks(rows, metrics):
    """Peringkat per metrik (1 = nilai tertinggi) di antara baris hasil."""
    for metric in metrics:
        ordered = sorted(
            (row for row in rows if row.get(metric) is not None),
            key=lambda row: to_decimal(row[metric]),
            reverse=True,
        )
        for position, row in enumerate(ordered, start=1):
            row[f"peringkat_{metric}"] = position


def extremes(rows, metric, dimensions):
    """Kategori dengan nilai tertinggi dan terendah untuk satu metrik."""
    valid = [row for row in rows if row.get(metric) is not None]
    if not valid:
        return None
    highest = max(valid, key=lambda row: to_decimal(row[metric]))
    lowest = min(valid, key=lambda row: to_decimal(row[metric]))
    return {
        "tertinggi": {"kategori": row_label(highest, dimensions), "nilai": highest[metric]},
        "terendah": {"kategori": row_label(lowest, dimensions), "nilai": lowest[metric]},
        "semua_kategori_bernilai_sama": (
            to_decimal(highest[metric]) == to_decimal(lowest[metric])
        ),
    }


def pct_text(value):
    """Persentase bergaya Indonesia, mis. 66,67%."""
    return "-" if value is None else format_indonesian_number(value, 2) + "%"


def tool_error_handler(
    operation_failed_message,
    semantic_error_code="SEMANTIC_VALIDATION_ERROR",
):
    """
    Dekorator penanganan error yang seragam untuk seluruh tool.
    Detail internal database tidak dikirim ke pengguna, tetapi dicatat
    pada log server.
    """

    def decorator(func):

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)

            except SemanticQueryError as exc:
                return error_response(semantic_error_code, str(exc))

            except CalculationError as exc:
                return error_response("CALCULATION_ERROR", str(exc))

            except (ValueError, TypeError) as exc:
                return error_response("INVALID_REQUEST", str(exc))

            except Exception:
                logger.exception("Tool %s gagal dijalankan.", func.__name__)
                return error_response(
                    "QUERY_EXECUTION_ERROR",
                    operation_failed_message,
                )

        return wrapper

    return decorator


# =====================================================
# TOOL 1: INSPECT DATA
# =====================================================

@server.tool(
    name="inspect_data",
    description=(
        "Melihat katalog metrik dan dimensi analisis BPJS "
        "yang tersedia dan telah divalidasi."
    ),
    structured_output=False,
)
def inspect_data() -> str:

    metrics_config, dimensions_config = load_config()

    available_metrics = []

    for key, metric in metrics_config["metrics"].items():

        try:
            get_metric_expression(key, metrics_config)

            available_metrics.append({
                "key": key,
                "label": metric["label"],
                "description": metric["description"],
                "unit": metric["unit"],
                "privacy": metric.get("privacy", "aggregate_only"),
            })

        except SemanticQueryError:
            continue

    available_dimensions = []

    for key, dimension in dimensions_config["dimensions"].items():

        try:
            resolve_dimension(key, dimensions_config)
            available = True

        except SemanticQueryError:
            available = False

        available_dimensions.append({
            "key": key,
            "label": dimension["label"],
            "available_for_query": available,
            "status": dimension.get("status"),
            "special_processing": bool(
                dimension.get("split_strategy")
            ),
        })

    return json_response({
        "dataset": "BPJS Dummy Analytics",
        "source": metrics_config["source"]["table"],
        "row_grain": metrics_config["source"]["row_grain"],
        "metrics": available_metrics,
        "dimensions": available_dimensions,
        "security": {
            "mode": "aggregate_only",
            "participant_identifiers_exposed": False,
            "raw_sql_allowed": False,
        },
    })


# =====================================================
# TOOL 2: ANALYZE DATA
# =====================================================

@server.tool(
    name="analyze_data",
    description=(
        "Melakukan analisis agregat data BPJS menggunakan "
        "metrik, dimensi, dan filter yang telah diizinkan. "
        "Opsional: sort_by ('metric' atau 'dimension') dan order "
        "('desc' untuk terbanyak/tertinggi, 'asc' untuk paling "
        "sedikit/terendah)."
    ),
    structured_output=False,
)
@tool_error_handler(
    "Query gagal dijalankan. "
    "Periksa konfigurasi dan koneksi database."
)
def analyze_data(
    metric: str,
    dimensions: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
    sort_by: str | None = None,
    order: str | None = None,
) -> str:

    dimensions = dimensions or []
    filters = validate_filters(filters)

    # Membentuk SQL dari konfigurasi yang diizinkan
    sql, params = build_query(
        metric=metric,
        dimensions=dimensions,
        filters=filters,
        limit=limit,
        sort_by=sort_by,
        order=order,
    )

    # Menjalankan query melalui koneksi read-only
    results = execute_query(sql, params)

    response = {
        "success": True,
        "metric": metric,
        "dimensions": dimensions,
        "filters": filters,
        "limit": limit,
        "result_count": len(results),
        "results": results,
    }

    if sort_by or order:
        response["sort"] = {"sort_by": sort_by or "metric", "order": order or "desc"}

    if dimensions and len(results) >= limit:
        response["possibly_truncated"] = True

    if uses_split_dimension(dimensions):
        response["note"] = SPLIT_DIMENSION_NOTE

    return json_response(response)


# =====================================================
# TOOL 3: COMPARE COSTS
# =====================================================

@server.tool(
    name="compare_costs",
    description=(
        "Membandingkan total biaya tagihan dan biaya verifikasi "
        "dengan filter yang sama. Menghitung selisih dan "
        "persentase selisih terhadap biaya tagihan menggunakan "
        "Calculation Engine. Gunakan untuk pertanyaan perbandingan biaya. "
        "Opsional: dimensions (mis. ['divre']) untuk rincian per kategori."
    ),
    structured_output=False,
)
@tool_error_handler(
    "Perbandingan biaya gagal dijalankan. "
    "Periksa konfigurasi dan koneksi database."
)
def compare_costs(
    filters: dict[str, Any] | None = None,
    dimensions: list[str] | None = None,
    limit: int = 20,
) -> str:

    filters = validate_filters(filters)
    dimensions = dimensions or []

    # Ambil kedua metrik dengan filter yang identik.
    component_values = {}

    for metric_key in (
        "total_biaya_tagihan",
        "total_biaya_verifikasi",
    ):
        sql, params = build_query(
            metric=metric_key,
            dimensions=[],
            filters=filters,
            limit=1,
        )

        rows = execute_query(sql, params)

        # SUM tanpa data bernilai NULL: anggap tidak tersedia.
        if (
            len(rows) != 1
            or metric_key not in rows[0]
            or rows[0][metric_key] is None
        ):
            raise CalculationError(
                f"Hasil metrik tidak tersedia: {metric_key}"
            )

        component_values[metric_key] = rows[0][metric_key]

    tagihan = component_values["total_biaya_tagihan"]
    verifikasi = component_values["total_biaya_verifikasi"]

    # Semua perhitungan dilakukan oleh Python.
    difference = calculate_difference(
        "selisih_tagihan_verifikasi",
        component_values,
    )

    difference_percentage = percentage(
        difference,
        tagihan,
    )

    breakdown = None

    # Rincian per dimensi (mis. per DIVRE) dengan filter yang sama.
    if dimensions:
        sql, params = build_multi_metric_query(
            ["total_biaya_tagihan", "total_biaya_verifikasi"],
            dimensions=dimensions,
            filters=filters,
            limit=limit,
        )

        breakdown = []

        for row in execute_query(sql, params):
            row_tagihan = row.get("total_biaya_tagihan")
            row_verifikasi = row.get("total_biaya_verifikasi")

            if row_tagihan is None or row_verifikasi is None:
                continue

            row_difference = calculate_difference(
                "selisih_tagihan_verifikasi",
                {
                    "total_biaya_tagihan": row_tagihan,
                    "total_biaya_verifikasi": row_verifikasi,
                },
            )

            breakdown.append({
                **{key: value for key, value in row.items()
                   if key not in ("total_biaya_tagihan", "total_biaya_verifikasi")},
                "total_biaya_tagihan": row_tagihan,
                "total_biaya_verifikasi": row_verifikasi,
                "selisih_tagihan_verifikasi": row_difference,
                "persentase_selisih_dari_tagihan": safe_percentage(
                    row_difference, row_tagihan
                ),
                "persentase_verifikasi_dari_total": safe_percentage(
                    row_verifikasi, verifikasi
                ),
            })

        add_ranks(breakdown, ["total_biaya_verifikasi", "selisih_tagihan_verifikasi"])

    response = {
        "success": True,
        "operation": "compare_costs",
        "filters": filters,
        "results": {
            "total_biaya_tagihan": tagihan,
            "total_biaya_verifikasi": verifikasi,
            "selisih_tagihan_verifikasi": difference,
            "persentase_selisih_dari_tagihan": difference_percentage,
        },
        "calculation": {
            "difference": (
                "biaya_tagihan - biaya_verifikasi"
            ),
            "percentage": (
                "selisih / biaya_tagihan * 100"
            ),
            "calculated_by": "Python Calculation Engine",
        },
    }

    if breakdown is not None:
        response["dimensions"] = dimensions
        response["breakdown"] = breakdown

    return json_response(response)


# =====================================================
# TOOL 4: SERVICE MIX
# =====================================================

@server.tool(
    name="service_mix",
    description=(
        "Menghitung jumlah dan persentase pelayanan berdasarkan "
        "jenis pelayanan, misalnya RJTL dan RITL. "
        "Menggunakan total keseluruhan dari query PostgreSQL "
        "terpisah dan Calculation Engine untuk persentase."
    ),
    structured_output=False,
)
@tool_error_handler("Analisis komposisi pelayanan gagal dijalankan.")
def service_mix(
    filters: dict[str, Any] | None = None,
) -> str:

    filters = validate_filters(filters)

    # Query pertama: jumlah berdasarkan jenis pelayanan.
    sql, params = build_query(
        metric="total_pelayanan",
        dimensions=["jenis_pelayanan"],
        filters=filters,
        limit=20,
    )

    grouped_results = execute_query(sql, params)

    # Query kedua: total keseluruhan tanpa dimensi.
    grand_total = fetch_single_value(
        "total_pelayanan",
        filters,
        "Total keseluruhan tidak berhasil diperoleh.",
        "Nilai total pelayanan tidak tersedia.",
    )

    # Hitung persentase menggunakan Calculation Engine.
    results = []

    for row in grouped_results:
        count = row.get("total_pelayanan")
        category = row.get("jenis_pelayanan")

        if count is None:
            raise CalculationError(
                "Jumlah pelayanan per kelompok tidak tersedia."
            )

        share = percentage(count, grand_total)

        results.append({
            "jenis_pelayanan": category,
            "total_pelayanan": count,
            "persentase": share,
        })

    return json_response({
        "success": True,
        "operation": "service_mix",
        "filters": filters,
        "grand_total": grand_total,
        "results": results,
        "calculated_by": "Python Calculation Engine",
    })


# =====================================================
# TOOL 5: SERVICE GROWTH
# =====================================================

@server.tool(
    name="service_growth",
    description=(
        "Menghitung pertumbuhan jumlah pelayanan antara dua tahun. "
        "Gunakan current_year untuk tahun terbaru dan previous_year "
        "untuk tahun pembanding. Perhitungan dilakukan oleh "
        "Python Calculation Engine."
    ),
    structured_output=False,
)
@tool_error_handler(
    "Analisis pertumbuhan pelayanan gagal dijalankan.",
    semantic_error_code="CALCULATION_ERROR",
)
def service_growth(
    current_year: int,
    previous_year: int,
) -> str:

    validate_years(current_year, previous_year)

    yearly_totals = {}

    for year in (previous_year, current_year):
        yearly_totals[year] = fetch_single_value(
            "total_pelayanan",
            {"tahun_pelayanan": year},
            f"Total pelayanan tahun {year} tidak tersedia.",
            f"Nilai pelayanan tahun {year} tidak tersedia.",
        )

    previous_total = yearly_totals[previous_year]
    current_total = yearly_totals[current_year]

    growth_percentage = growth(current_total, previous_total)
    difference = to_decimal(current_total) - to_decimal(previous_total)

    return json_response({
        "success": True,
        "operation": "service_growth",
        "current_year": current_year,
        "previous_year": previous_year,
        "results": {
            "previous_total": previous_total,
            "current_total": current_total,
            "difference": difference,
            "growth_percentage": growth_percentage,
        },
        "calculated_by": "Python Calculation Engine",
    })


# =====================================================
# TOOL 6: METRIC GROWTH
# =====================================================

@server.tool(
    name="metric_growth",
    description=(
        "Menghitung pertumbuhan metrik tervalidasi antara dua tahun. "
        "Dapat digunakan untuk jumlah pelayanan, jumlah SJP unik, "
        "jumlah peserta unik, total biaya tagihan, biaya verifikasi, "
        "biaya rumah sakit, dan rata-rata biaya verifikasi. "
        "Tidak digunakan untuk metrik bertipe derived_difference. "
        "Parameter: metric, current_year, previous_year. "
        "Selisih nominal dan persentase dihitung oleh Python Calculation Engine."
    ),
    structured_output=False,
)
@tool_error_handler(
    "Analisis pertumbuhan metrik gagal dijalankan.",
    semantic_error_code="CALCULATION_ERROR",
)
def metric_growth(
    metric: str,
    current_year: int,
    previous_year: int,
) -> str:

    if not isinstance(metric, str) or not metric.strip():
        raise ValueError("Nama metrik harus berupa teks.")

    validate_years(current_year, previous_year)

    metrics_config, _ = load_config()
    metric_config = metrics_config["metrics"].get(metric)

    if not metric_config:
        raise ValueError(f"Metrik tidak tersedia: {metric}")

    if metric_config.get("status") != "validated":
        raise ValueError(f"Metrik belum tervalidasi: {metric}")

    aggregation = metric_config.get("aggregation", {})

    if aggregation.get("type") == "derived_difference":
        raise ValueError(
            "Pertumbuhan untuk metrik derived_difference "
            "belum didukung."
        )

    yearly_values = {}

    for year in (previous_year, current_year):
        value = fetch_single_value(
            metric,
            {"tahun_pelayanan": year},
            f"Nilai metrik {metric} tahun {year} tidak tersedia.",
            f"Nilai metrik {metric} tahun {year} kosong.",
        )
        yearly_values[year] = Decimal(str(value))

    previous_value = yearly_values[previous_year]
    current_value = yearly_values[current_year]

    difference = current_value - previous_value
    growth_percentage = growth(current_value, previous_value)

    return json_response({
        "success": True,
        "operation": "metric_growth",
        "metric": metric,
        "label": metric_config.get("label", metric),
        "unit": metric_config.get("unit"),
        "current_year": current_year,
        "previous_year": previous_year,
        "results": {
            "previous_value": previous_value,
            "current_value": current_value,
            "difference": difference,
            "growth_percentage": growth_percentage,
        },
        "calculated_by": "Python Calculation Engine",
    })


# =====================================================
# TOOL 7: METRIC SHARE
# =====================================================

SHARE_SUPPORTED_TYPES = {"count", "distinct_count", "sum"}


@server.tool(
    name="metric_share",
    description=(
        "Menghitung nilai dan persentase (proporsi) sebuah metrik per "
        "kategori dimensi apa pun, misalnya peserta unik per segmen, "
        "biaya tagihan per DIVRE, atau pelayanan per kelompok umur. "
        "Total keseluruhan diambil dari query PostgreSQL terpisah tanpa "
        "dimensi dan persentase dihitung oleh Python Calculation Engine. "
        "Parameter: metric, dimensions (list), filters (dictionary), limit. "
        "Opsional within (list): hitung persentase di dalam setiap kelompok, "
        "mis. dimensions=['jenis_kelamin','kelompok_umur'], "
        "within=['jenis_kelamin'] untuk distribusi umur pada tiap jenis kelamin."
    ),
    structured_output=False,
)
@tool_error_handler("Analisis persentase metrik gagal dijalankan.")
def metric_share(
    metric: str,
    dimensions: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
    within: list[str] | None = None,
    sort_by: str | None = None,
    order: str | None = None,
) -> str:

    dimensions = dimensions or []
    within = within or []
    filters = validate_filters(filters)

    if not dimensions:
        raise ValueError(
            "metric_share membutuhkan minimal satu dimensi."
        )

    if not isinstance(within, list) or not set(within) <= set(dimensions):
        raise ValueError(
            "Parameter within harus berupa list dan bagian dari dimensions."
        )

    if within and set(within) == set(dimensions):
        raise ValueError(
            "within tidak boleh sama dengan seluruh dimensions."
        )

    metrics_config, _ = load_config()
    metric_config = get_metric_config(metric, metrics_config)

    aggregation = metric_config.get("aggregation") or {}
    metric_type = aggregation.get("type")

    if metric_type not in SHARE_SUPPORTED_TYPES:
        raise ValueError(
            "Persentase per kategori tidak bermakna untuk metrik "
            f"bertipe {metric_type}: {metric}"
        )

    # Persentase di dalam kelompok: urutkan per kelompok agar mudah dibaca.
    if within and sort_by is None and order is None:
        ordered_dimensions = within + [d for d in dimensions if d not in within]
    else:
        ordered_dimensions = dimensions

    sql, params = build_query(
        metric=metric,
        dimensions=ordered_dimensions,
        filters=filters,
        limit=limit,
        sort_by=sort_by,
        order=order,
    )

    grouped_results = execute_query(sql, params)

    if within and sort_by is None and order is None:
        # Kelompok induk berurutan, di dalamnya nilai terbesar dulu.
        grouped_results.sort(
            key=lambda row: (
                tuple(str(row.get(dim)) for dim in within),
                -to_decimal(row.get(metric) or 0),
            )
        )

    grand_total = fetch_single_value(
        metric,
        filters,
        "Total keseluruhan tidak berhasil diperoleh.",
        f"Nilai total {metric} tidak tersedia.",
    )

    group_totals = (
        fetch_group_totals(metric, within, filters) if within else {}
    )

    results = []

    for row in grouped_results:
        value = row.get(metric)

        if value is None:
            raise CalculationError(
                f"Nilai {metric} per kelompok tidak tersedia."
            )

        item = {
            **row,
            "persentase": round_decimal(
                percentage(value, grand_total), 2
            ),
        }

        if within:
            group_total = group_totals.get(row_key(row, within))
            item["total_kelompok"] = group_total
            item["persentase_dalam_kelompok"] = safe_percentage(
                value, group_total
            )

        results.append(item)

    additive = aggregation.get("additive") is True

    response = {
        "success": True,
        "operation": "metric_share",
        "metric": metric,
        "label": metric_config.get("label", metric),
        "unit": metric_config.get("unit"),
        "dimensions": dimensions,
        "filters": filters,
        "grand_total": grand_total,
        "result_count": len(results),
        "results": results,
        "additive": additive,
        "calculated_by": "Python Calculation Engine",
    }

    if within:
        response["within"] = within

    if len(results) >= limit:
        response["possibly_truncated"] = True

    notes = []
    if not additive:
        notes.append(
            "Metrik distinct count: satu entitas dapat muncul pada lebih "
            "dari satu kategori, sehingga jumlah persentase kategori "
            "dapat melebihi 100%. Total keseluruhan dihitung terpisah."
        )
    if uses_split_dimension(dimensions):
        notes.append(SPLIT_DIMENSION_NOTE)
    if notes:
        response["note"] = " ".join(notes)

    return json_response(response)


# =====================================================
# TOOL 8: COMPARE METRICS (BARU)
# =====================================================

@server.tool(
    name="compare_metrics",
    description=(
        "Membandingkan beberapa metrik sekaligus (2-6) pada kategori yang "
        "sama, misalnya jumlah pelayanan, biaya verifikasi, dan rata-rata "
        "biaya verifikasi per DIVRE/segmen/kelompok umur. Menghasilkan "
        "nilai per kategori, total keseluruhan, persentase kontribusi, "
        "peringkat per metrik, serta kategori tertinggi/terendah. Gunakan "
        "untuk pertanyaan 'apakah wilayah dengan pelayanan tertinggi juga "
        "biayanya tertinggi', 'jumlah pelayanan dan biaya per segmen', atau "
        "'kategori dengan pelayanan tinggi tetapi rata-rata biaya rendah'."
    ),
    structured_output=False,
)
@tool_error_handler("Perbandingan metrik gagal dijalankan.")
def compare_metrics(
    metrics: list[str],
    dimensions: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    limit: int = 20,
    sort_by: str | None = None,
    order: str | None = None,
) -> str:

    dimensions = dimensions or []
    filters = validate_filters(filters)

    if not isinstance(metrics, list) or len(metrics) < 1:
        raise ValueError("Parameter metrics harus berupa list metrik.")

    metrics_config, _ = load_config()

    totals = fetch_totals(metrics, filters)

    response = {
        "success": True,
        "operation": "compare_metrics",
        "metrics": metrics,
        "labels": {
            metric: get_metric_config(metric, metrics_config).get("label", metric)
            for metric in metrics
        },
        "units": {
            metric: get_metric_config(metric, metrics_config).get("unit")
            for metric in metrics
        },
        "dimensions": dimensions,
        "filters": filters,
        "totals": totals,
        "calculated_by": "Python Calculation Engine",
    }

    if not dimensions:
        return json_response(response)

    rows = execute_metrics_query(
        metrics,
        dimensions=dimensions,
        filters=filters,
        limit=limit,
        sort_by=sort_by,
        order=order,
    )

    share_metrics = [
        metric for metric in metrics
        if aggregation_type(metric, metrics_config) in SHARE_SUPPORTED_TYPES
    ]

    for row in rows:
        for metric in share_metrics:
            row[f"persen_{metric}"] = safe_percentage(
                row.get(metric), totals.get(metric)
            )

    add_ranks(rows, metrics)

    summary = {}
    for metric in metrics:
        result = extremes(rows, metric, dimensions)
        if result:
            summary[metric] = result

    top_categories = {
        metric: summary[metric]["tertinggi"]["kategori"]
        for metric in summary
    }

    response.update({
        "result_count": len(rows),
        "results": rows,
        "summary": summary,
        "kategori_tertinggi_sama_untuk_semua_metrik": (
            len(set(top_categories.values())) == 1 if top_categories else None
        ),
    })

    if len(rows) >= limit:
        response["possibly_truncated"] = True
        response["note"] = (
            "Hasil terpotong oleh limit; peringkat hanya berlaku untuk "
            "kategori yang ditampilkan."
        )

    distinct_metrics = [
        metric for metric in metrics
        if aggregation_type(metric, metrics_config) == "distinct_count"
    ]
    if distinct_metrics:
        response["distinct_count_note"] = (
            "Total metrik distinct count dihitung terpisah dan tidak sama "
            "dengan penjumlahan antar-kategori."
        )

    return json_response(response)


# =====================================================
# TOOL 9: TOP CATEGORIES (BARU)
# =====================================================

@server.tool(
    name="top_categories",
    description=(
        "Menampilkan N kategori teratas atau terbawah untuk sebuah metrik, "
        "opsional untuk setiap kelompok. Contoh: 10 diagnosis primer "
        "terbanyak (dimensions=['diagnosis_primer']), 5 diagnosis terbanyak "
        "per jenis pelayanan (group_by=['jenis_pelayanan']), atau 5 "
        "kombinasi segmen-umur-jenis kelamin dengan biaya verifikasi "
        "terbesar. order='desc' untuk terbanyak/tertinggi, 'asc' untuk "
        "paling sedikit/terendah."
    ),
    structured_output=False,
)
@tool_error_handler("Analisis peringkat kategori gagal dijalankan.")
def top_categories(
    metric: str,
    dimensions: list[str],
    group_by: list[str] | None = None,
    filters: dict[str, Any] | None = None,
    top_n: int = 5,
    order: str = "desc",
) -> str:

    group_by = group_by or []
    filters = validate_filters(filters)

    metrics_config, _ = load_config()
    metric_config = get_metric_config(metric, metrics_config)

    sql, params = build_top_n_query(
        metric,
        dimensions=dimensions,
        group_by=group_by,
        filters=filters,
        top_n=top_n,
        order=order,
    )

    rows = execute_query(sql, params)

    metric_type = aggregation_type(metric, metrics_config)

    if metric_type in SHARE_SUPPORTED_TYPES:
        if group_by:
            group_totals = fetch_group_totals(metric, group_by, filters)
        else:
            group_totals = {
                (): fetch_single_value(
                    metric, filters,
                    "Total keseluruhan tidak berhasil diperoleh.",
                    f"Nilai total {metric} tidak tersedia.",
                )
            }

        for row in rows:
            total = group_totals.get(row_key(row, group_by))
            row["total_kelompok"] = total
            row["persentase"] = safe_percentage(row.get(metric), total)

    response = {
        "success": True,
        "operation": "top_categories",
        "metric": metric,
        "label": metric_config.get("label", metric),
        "unit": metric_config.get("unit"),
        "dimensions": dimensions,
        "group_by": group_by,
        "filters": filters,
        "top_n": top_n,
        "order": order,
        "result_count": len(rows),
        "results": rows,
        "calculated_by": "Python Calculation Engine",
    }

    if uses_split_dimension(dimensions + group_by):
        response["note"] = SPLIT_DIMENSION_NOTE

    return json_response(response)


# =====================================================
# TOOL 10: PROFILE SUMMARY (BARU)
# =====================================================

DEFAULT_PROFILE_DIMENSIONS = [
    "jenis_kelamin",
    "kelompok_umur",
    "segmen_peserta",
    "jenis_pelayanan",
    "kelas_hak",
]

PROFILE_METRICS = [
    "total_peserta_unik",
    "total_pelayanan",
    "total_biaya_verifikasi",
    "rata_rata_biaya_verifikasi",
]


def _profile_insights(dimension, label, rows, totals, metrics_config):
    insights = []

    if not rows:
        return insights

    def fmt(metric, value):
        return format_metric_value(metric, value, metrics_config)

    most_services = max(rows, key=lambda row: to_decimal(row["total_pelayanan"] or 0))
    insights.append(
        f"{label}: kategori dengan pelayanan terbanyak adalah "
        f"'{most_services[dimension]}' ({fmt('total_pelayanan', most_services['total_pelayanan'])} "
        f"pelayanan, {pct_text(most_services.get('persen_pelayanan'))} dari total)."
    )

    most_people = max(rows, key=lambda row: to_decimal(row["total_peserta_unik"] or 0))
    insights.append(
        f"{label}: peserta unik terbanyak pada '{most_people[dimension]}' "
        f"({fmt('total_peserta_unik', most_people['total_peserta_unik'])} peserta)."
    )

    valid_avg = [row for row in rows if row.get("rata_rata_biaya_verifikasi") is not None]
    if len(valid_avg) >= 2 and len({
        to_decimal(row["rata_rata_biaya_verifikasi"]) for row in valid_avg
    }) == 1:
        insights.append(
            f"{label}: rata-rata biaya verifikasi sama pada semua kategori "
            f"({fmt('rata_rata_biaya_verifikasi', valid_avg[0]['rata_rata_biaya_verifikasi'])})."
        )
    elif len(valid_avg) >= 2:
        highest = max(valid_avg, key=lambda row: to_decimal(row["rata_rata_biaya_verifikasi"]))
        lowest = min(valid_avg, key=lambda row: to_decimal(row["rata_rata_biaya_verifikasi"]))
        ratio_text = ""
        try:
            ratio_value = round_decimal(
                to_decimal(highest["rata_rata_biaya_verifikasi"])
                / to_decimal(lowest["rata_rata_biaya_verifikasi"]),
                2,
            )
            ratio_text = f", {format_indonesian_number(ratio_value, 2)} kali lipat"
        except Exception:
            pass
        insights.append(
            f"{label}: rata-rata biaya verifikasi tertinggi pada "
            f"'{highest[dimension]}' ({fmt('rata_rata_biaya_verifikasi', highest['rata_rata_biaya_verifikasi'])}) "
            f"dan terendah pada '{lowest[dimension]}' "
            f"({fmt('rata_rata_biaya_verifikasi', lowest['rata_rata_biaya_verifikasi'])}{ratio_text})."
        )

    # Kategori yang porsi biayanya jauh berbeda dari porsi pelayanannya.
    for row in rows:
        service_share = row.get("persen_pelayanan")
        cost_share = row.get("persen_biaya_verifikasi")
        if service_share is None or cost_share is None:
            continue
        if cost_share - service_share >= 5:
            insights.append(
                f"{label}: '{row[dimension]}' menyerap {pct_text(cost_share)} biaya "
                f"verifikasi padahal hanya {pct_text(service_share)} dari pelayanan."
            )
        elif service_share - cost_share >= 5:
            insights.append(
                f"{label}: '{row[dimension]}' mencakup {pct_text(service_share)} "
                f"pelayanan tetapi hanya {pct_text(cost_share)} biaya verifikasi."
            )

    return insights


@server.tool(
    name="profile_summary",
    description=(
        "Ringkasan karakteristik peserta dan pelayanan untuk beberapa "
        "dimensi sekaligus (default: jenis kelamin, kelompok umur, segmen "
        "peserta, jenis pelayanan, kelas hak). Untuk setiap kategori "
        "berisi peserta unik, jumlah pelayanan, biaya verifikasi, rata-rata "
        "biaya, persentase, serta daftar temuan (insights) yang sudah "
        "dihitung Python. Gunakan untuk pertanyaan ringkasan, profil, "
        "karakteristik, atau 'temuan menarik' dari dataset."
    ),
    structured_output=False,
)
@tool_error_handler("Ringkasan profil gagal dijalankan.")
def profile_summary(
    dimensions: list[str] | None = None,
    filters: dict[str, Any] | None = None,
) -> str:

    dimensions = dimensions or list(DEFAULT_PROFILE_DIMENSIONS)
    filters = validate_filters(filters)

    if not isinstance(dimensions, list) or len(dimensions) > 6:
        raise ValueError("Parameter dimensions berupa list, maksimal 6 dimensi.")

    if len(dimensions) != len(set(dimensions)):
        raise ValueError("Dimensi duplikat tidak diperbolehkan.")

    if uses_split_dimension(dimensions):
        raise ValueError(
            "profile_summary tidak mendukung dimensi multi-nilai "
            "(diagnosis sekunder)."
        )

    metrics_config, dimensions_config = load_config()
    totals = fetch_totals(PROFILE_METRICS, filters)

    profiles = {}
    insights = []

    for dimension in dimensions:
        rows = execute_metrics_query(
            PROFILE_METRICS,
            dimensions=[dimension],
            filters=filters,
            limit=20,
            sort_by="total_pelayanan",
        )

        for row in rows:
            row["persen_pelayanan"] = safe_percentage(
                row.get("total_pelayanan"), totals.get("total_pelayanan")
            )
            row["persen_biaya_verifikasi"] = safe_percentage(
                row.get("total_biaya_verifikasi"),
                totals.get("total_biaya_verifikasi"),
            )
            row["persen_peserta"] = safe_percentage(
                row.get("total_peserta_unik"), totals.get("total_peserta_unik")
            )

        label = (
            (dimensions_config.get("dimensions") or {}).get(dimension) or {}
        ).get("label", dimension)

        profiles[dimension] = {
            "label": label,
            "categories": rows,
        }

        insights.extend(
            _profile_insights(dimension, label, rows, totals, metrics_config)
        )

    return json_response({
        "success": True,
        "operation": "profile_summary",
        "filters": filters,
        "totals": totals,
        "totals_formatted": {
            metric: format_metric_value(metric, totals.get(metric), metrics_config)
            for metric in PROFILE_METRICS
        },
        "profiles": profiles,
        "insights": insights,
        "notes": [
            "persen_peserta memakai distinct count: satu peserta dapat "
            "masuk lebih dari satu kategori sehingga jumlahnya dapat "
            "melebihi 100%.",
            "Rata-rata biaya verifikasi dihitung per baris pelayanan.",
        ],
        "calculated_by": "Python Calculation Engine",
    })


# =====================================================
# START SERVER
# =====================================================

if __name__ == "__main__":

    server.run(transport="stdio")
