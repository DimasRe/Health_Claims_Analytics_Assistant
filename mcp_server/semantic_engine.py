"""
SEMANTIC QUERY ENGINE
PostgreSQL -> Controlled SQL Builder

Modul ini menerjemahkan permintaan analitik (metrik, dimensi, filter)
menjadi SQL terkontrol. Hanya metrik, dimensi, kolom, dan tabel yang
masuk whitelist yang boleh dipakai, sehingga model AI tidak pernah
menulis SQL sendiri.
"""

from pathlib import Path
import os
import re
import threading

import yaml


# =========================================================
# KONFIGURASI DASAR
# =========================================================

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"

METRICS_FILE = CONFIG_DIR / "metrics.yaml"
DIMENSIONS_FILE = CONFIG_DIR / "dimensions.yaml"

ALLOWED_SOURCE = "public.claims"

MAX_DIMENSIONS = 5
MAX_FILTER_VALUES = 100
MIN_LIMIT = 1
MAX_LIMIT = 100

# Identitas sensitif tidak boleh digunakan sebagai dimensi
SENSITIVE_COLUMNS = {
    "member_id",
    "claim_id",
    "registration_no",
    "attending_doctor_name",
    "doctor_code",
    "doctor_name",
    "user_name",
    "regional_user_name",
}

# Whitelist ekspresi metrik yang diperbolehkan
ALLOWED_METRICS = {
    "total_pelayanan": "COUNT(*)",
    "total_sjp_unik": "COUNT(DISTINCT t.\"claim_id\")",
    "total_peserta_unik": "COUNT(DISTINCT t.\"member_id\")",
    "total_biaya_tagihan": "SUM(t.\"billed_amount\")",
    "total_biaya_verifikasi": "SUM(t.\"verified_amount\")",
    "total_biaya_rs": "SUM(t.\"hospital_cost\")",
    "rata_rata_biaya_verifikasi": "AVG(t.\"verified_amount\")",
    "selisih_tagihan_verifikasi": (
        "SUM(t.\"billed_amount\") - SUM(t.\"verified_amount\")"
    ),
    # Metrik tambahan v1.2
    "rata_rata_biaya_tagihan": "AVG(t.\"billed_amount\")",
    "rata_rata_biaya_rs": "AVG(t.\"hospital_cost\")",
    "median_biaya_verifikasi": (
        "PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY t.\"verified_amount\")"
    ),
    "biaya_verifikasi_per_peserta": (
        "SUM(t.\"verified_amount\") / NULLIF(COUNT(DISTINCT t.\"member_id\"), 0)"
    ),
    "rata_rata_pelayanan_per_peserta": (
        "COUNT(*)::numeric / NULLIF(COUNT(DISTINCT t.\"member_id\"), 0)"
    ),
}

# Ekspresi dimensi yang diperbolehkan
ALLOWED_EXPRESSIONS = {
    "tahun_pelayanan": (
        'EXTRACT(YEAR FROM t."service_date")::integer'
    ),
    "bulan_pelayanan": (
        'EXTRACT(MONTH FROM t."service_date")::integer'
    ),
}

# Dimensi waktu diurutkan kronologis (urutan = prioritas ORDER BY).
CHRONOLOGICAL_DIMENSIONS = ("tahun_pelayanan", "bulan_pelayanan")

# Tabel lookup yang diperbolehkan
ALLOWED_LOOKUP_TABLES = {
    "public.regions",
}

# Dimensi yang memiliki kolom kode dan dapat difilter berdasarkan kode.
CODE_FILTER_DIMENSIONS = {
    "divre", "cabang", "fasilitas_kesehatan",
    "diagnosis_primer", "jenis_kepulangan",
}

# Delimiter yang diizinkan untuk dimensi split (mis. diagnosis sekunder).
ALLOWED_SPLIT_DELIMITERS = {";": ";", ",": ",", "|": "\\|"}

# Dimensi split memecah satu baris menjadi beberapa baris, sehingga hanya
# metrik hitungan yang tetap bermakna (SUM/AVG akan terduplikasi).
SPLIT_SAFE_AGGREGATIONS = {"count", "distinct_count"}

# Metrik COUNT(DISTINCT kolom). Di tabel besar COUNT(DISTINCT) lambat karena
# PostgreSQL selalu mengurutkan tanpa paralelisme. Metrik ini ditulis ulang
# menjadi SELECT DISTINCT ... lalu COUNT(*) (hasil identik, dapat memakai
# hash aggregate & paralelisme). Kolom identitas hanya dipakai di subquery
# dan tidak pernah ikut ditampilkan.
DISTINCT_COUNT_ENTITIES = {
    "total_peserta_unik": 't."member_id"',
    "total_sjp_unik": 't."claim_id"',
}

SORT_BY_OPTIONS = {"metric", "dimension"}
ORDER_OPTIONS = {"asc", "desc"}
MAX_TOP_N = 50

# Alias filter yang boleh digunakan model/pengguna.
# Alias kode dipisahkan dari alias label agar filter tidak ambigu.
# Alias tambahan juga dibaca dari `filter_aliases` pada dimensions.yaml.
FILTER_ALIASES = {
    "tahun": ("tahun_pelayanan", "default"),
    "year": ("tahun_pelayanan", "default"),
    "bulan": ("bulan_pelayanan", "default"),
    "month": ("bulan_pelayanan", "default"),
    "divre_code": ("divre", "code"),
    "kode_divre": ("divre", "code"),
    "kode_divisi_regional": ("divre", "code"),
    "divisi_regional_code": ("divre", "code"),
    "service_region_code": ("divre", "code"),
    "nama_divre": ("divre", "label"),
    "nama_divisi_regional": ("divre", "label"),
    "cabang_code": ("cabang", "code"),
    "kode_cabang": ("cabang", "code"),
    "branch_code": ("cabang", "code"),
    "service_branch_code": ("cabang", "code"),
    "fasilitas_kesehatan_code": ("fasilitas_kesehatan", "code"),
    "kode_faskes": ("fasilitas_kesehatan", "code"),
    "kode_rs": ("fasilitas_kesehatan", "code"),
    "kode_rumah_sakit": ("fasilitas_kesehatan", "code"),
    "hospital_code": ("fasilitas_kesehatan", "code"),
    "facility_code": ("fasilitas_kesehatan", "code"),
    "nama_rs": ("fasilitas_kesehatan", "label"),
    "nama_rumah_sakit": ("fasilitas_kesehatan", "label"),
    "nama_faskes": ("fasilitas_kesehatan", "label"),
    "nama_cabang": ("cabang", "label"),
}


class SemanticQueryError(Exception):
    """Kesalahan validasi semantic query."""
    pass


# =========================================================
# PEMBACAAN KONFIGURASI (DENGAN CACHE)
# =========================================================

_config_cache = {"key": None, "value": None}
_config_lock = threading.Lock()


def _config_cache_key():
    """Kunci cache berdasarkan waktu modifikasi file konfigurasi."""
    try:
        return (
            METRICS_FILE.stat().st_mtime_ns,
            DIMENSIONS_FILE.stat().st_mtime_ns,
        )
    except OSError:
        return None


def _read_config_files():
    with open(METRICS_FILE, encoding="utf-8") as f:
        metrics_config = yaml.safe_load(f)

    with open(DIMENSIONS_FILE, encoding="utf-8") as f:
        dimensions_config = yaml.safe_load(f)

    if not isinstance(metrics_config, dict) or not isinstance(
        dimensions_config, dict
    ):
        raise SemanticQueryError("Format file konfigurasi tidak valid.")

    if (metrics_config.get("source") or {}).get("table") != ALLOWED_SOURCE:
        raise SemanticQueryError("Tabel sumber tidak diizinkan.")

    if (dimensions_config.get("source") or {}).get("table") != ALLOWED_SOURCE:
        raise SemanticQueryError("Tabel dimensi tidak sesuai.")

    return metrics_config, dimensions_config


def load_config():
    """
    Membaca konfigurasi metrik dan dimensi.

    Hasil disimpan di cache dan otomatis dibaca ulang ketika file YAML
    berubah, sehingga perubahan konfigurasi tetap langsung berlaku tanpa
    membaca disk pada setiap query.
    """

    key = _config_cache_key()

    with _config_lock:
        if key is not None and _config_cache["key"] == key:
            return _config_cache["value"]

        value = _read_config_files()
        _config_cache["key"] = key
        _config_cache["value"] = value
        return value


# =========================================================
# NORMALISASI FILTER
# =========================================================

def _normalize_name(name):
    return name.strip().casefold().replace(" ", "_")


def get_filter_aliases(dimensions_config=None):
    """
    Menggabungkan alias bawaan dengan `filter_aliases` dari dimensions.yaml.
    Alias bawaan diprioritaskan agar perilaku lama tidak berubah.
    """

    aliases = {}

    if isinstance(dimensions_config, dict):
        for dim_key, dim in (dimensions_config.get("dimensions") or {}).items():
            yaml_aliases = (dim or {}).get("filter_aliases") or {}
            for mode in ("code", "label"):
                for alias in yaml_aliases.get(mode) or []:
                    if isinstance(alias, str) and alias.strip():
                        aliases[_normalize_name(alias)] = (dim_key, mode)

    aliases.update(FILTER_ALIASES)
    return aliases


def normalize_filter_key(key, dimensions_config=None):
    """Mengubah alias filter menjadi dimensi dan mode filter resmi."""
    if not isinstance(key, str) or not key.strip():
        raise SemanticQueryError("Nama filter tidak valid.")

    normalized = _normalize_name(key)
    aliases = get_filter_aliases(dimensions_config)
    return aliases.get(normalized, (normalized, "default"))


_DIVRE_CODE_PATTERN = re.compile(
    r"\s*(?:(?:DIVRE|DIVISI\s+REGIONAL)\s*)?0*(\d{1,2})\s*",
    re.IGNORECASE,
)

_DIVRE_LABEL_WITH_NUMBER = re.compile(
    r"\s*(?:DIVRE|DIVISI\s+REGIONAL)\s*0*\d{1,2}\s*",
    re.IGNORECASE,
)


def normalize_filter_value(dimension_key, mode, value):
    """Normalisasi ringan nilai filter kode tanpa mengubah kode cabang/faskes."""
    def normalize_one(item):
        if dimension_key == "divre" and mode == "code":
            if isinstance(item, int) and not isinstance(item, bool):
                return f"{item:02d}"
            if isinstance(item, str):
                match = _DIVRE_CODE_PATTERN.fullmatch(item)
                if match:
                    return f"{int(match.group(1)):02d}"
        if mode == "code" and isinstance(item, int) and not isinstance(item, bool):
            return str(item)
        if isinstance(item, str):
            return item.strip()
        return item

    if isinstance(value, (list, tuple, set)):
        return [normalize_one(item) for item in value]
    return normalize_one(value)


def _looks_like_code(dimension_key, item):
    """True jika nilai filter terlihat seperti kode, bukan label."""
    if isinstance(item, bool):
        return False
    if isinstance(item, int):
        return True
    if not isinstance(item, str):
        return False
    if item.strip().isdigit():
        return True
    return dimension_key == "divre" and bool(
        _DIVRE_LABEL_WITH_NUMBER.fullmatch(item)
    )


# =========================================================
# RESOLUSI METRIK DAN DIMENSI
# =========================================================

def quote_identifier(name):
    """Validasi dan mengamankan nama kolom SQL."""

    if not isinstance(name, str):
        raise SemanticQueryError("Nama kolom tidak valid.")

    if not re.fullmatch(r"[a-z_][a-z0-9_]*", name):
        raise SemanticQueryError(
            f"Identifier tidak diizinkan: {name}"
        )

    if name in SENSITIVE_COLUMNS:
        raise SemanticQueryError(
            f"Kolom sensitif tidak boleh diakses: {name}"
        )

    return f'"{name}"'


def get_metric_expression(metric_key, metrics_config):
    """Memeriksa metrik dan mengambil ekspresi dari whitelist."""

    metrics = metrics_config.get("metrics", {})

    if not isinstance(metric_key, str) or metric_key not in metrics:
        raise SemanticQueryError(
            f"Metrik tidak dikenal: {metric_key}"
        )

    metric = metrics[metric_key]

    if metric.get("status") != "validated":
        raise SemanticQueryError(
            f"Metrik belum divalidasi: {metric_key}"
        )

    if metric_key not in ALLOWED_METRICS:
        raise SemanticQueryError(
            f"Metrik belum masuk whitelist: {metric_key}"
        )

    return ALLOWED_METRICS[metric_key]


def _resolve_split_dimension(key, dim):
    """
    Dimensi multi-nilai dalam satu kolom teks, misalnya diagnosis
    sekunder "E78;I25". Setiap nilai dipecah menjadi baris tersendiri
    menggunakan CROSS JOIN LATERAL unnest(...).
    """

    if dim.get("split_strategy") != "split_and_unnest":
        raise SemanticQueryError(
            f"Dimensi {key} membutuhkan pemrosesan khusus "
            "dan belum diaktifkan."
        )

    column = quote_identifier(dim["column"])
    delimiter = dim.get("split_delimiter", ";")

    if delimiter not in ALLOWED_SPLIT_DELIMITERS:
        raise SemanticQueryError(
            f"Delimiter dimensi {key} tidak diizinkan."
        )

    regex_delimiter = ALLOWED_SPLIT_DELIMITERS[delimiter]
    alias = f"sp_{key}"
    item = f'TRIM({alias}."item")'

    resolved = {
        "select": f'{item} AS "{key}"',
        "group": item,
        # Filter tanpa memecah baris: nilai dicari di dalam array.
        "filter_any": (
            f"regexp_split_to_array(TRIM(t.{column}), "
            f"'\\s*{regex_delimiter}\\s*')"
        ),
        "join": (
            f"CROSS JOIN LATERAL unnest(string_to_array(t.{column}, "
            f"'{delimiter}')) AS {alias}(\"item\")"
        ),
        "split": True,
    }

    if dim.get("exclude_empty", True):
        resolved["where"] = f"{item} <> ''"

    return resolved


def resolve_dimension(key, dimensions_config):
    """Mengubah definisi dimensi menjadi ekspresi SQL."""

    dimensions = dimensions_config.get("dimensions", {})

    if not isinstance(key, str) or key not in dimensions:
        raise SemanticQueryError(
            f"Dimensi tidak dikenal: {key}"
        )

    dim = dimensions[key]

    if dim.get("status") != "validated":
        raise SemanticQueryError(
            f"Dimensi belum divalidasi: {key}"
        )

    # Dimensi yang membutuhkan pemecahan nilai (mis. "E78;I25").
    if dim.get("split_strategy"):
        return _resolve_split_dimension(key, dim)

    if key in ALLOWED_EXPRESSIONS:
        expression = ALLOWED_EXPRESSIONS[key]
        return {
            "select": f'{expression} AS "{key}"',
            "group": expression,
            "filter": expression,
        }

    # Dimensi dengan kolom biasa
    if "column" in dim:
        column = quote_identifier(dim["column"])

        return {
            "select": f't.{column} AS "{key}"',
            "group": f"t.{column}",
            "filter": f"t.{column}",
        }

    # Dimensi dengan tabel lookup
    if "lookup_table" in dim:
        table = dim["lookup_table"]

        if table not in ALLOWED_LOOKUP_TABLES:
            raise SemanticQueryError(
                f"Tabel lookup tidak diizinkan: {table}"
            )

        code_column = quote_identifier(dim["code_column"])
        lookup_key = quote_identifier(dim["lookup_key"])
        lookup_label = quote_identifier(dim["lookup_label"])

        join = (
            f'LEFT JOIN {table} AS r '
            f'ON t.{code_column} = r.{lookup_key}'
        )

        code_expr = f"r.{lookup_key}"
        label_expr = f"r.{lookup_label}"

        return {
            "select": (
                f'{code_expr} AS "{key}_code", '
                f'{label_expr} AS "{key}"'
            ),
            "group": [code_expr, label_expr],
            "filter": label_expr,
            # Filter kode memakai kolom sumber agar tetap tepat walaupun
            # suatu kode belum memiliki pasangan pada tabel lookup.
            "filter_code": f"t.{code_column}",
            "join": join,
        }

    # Dimensi dengan kode dan label di tabel utama
    if "code_column" in dim and "label_column" in dim:
        code_column = quote_identifier(dim["code_column"])
        label_column = quote_identifier(dim["label_column"])

        code_expr = f"t.{code_column}"
        label_expr = f"t.{label_column}"

        return {
            "select": (
                f'{code_expr} AS "{key}_code", '
                f'{label_expr} AS "{key}"'
            ),
            "group": [code_expr, label_expr],
            "filter": label_expr,
            "filter_code": code_expr,
        }

    raise SemanticQueryError(
        f"Struktur dimensi belum didukung: {key}"
    )


# =========================================================
# PEMBENTUKAN QUERY
# =========================================================

def _validate_build_arguments(dimensions, filters, limit):
    if not isinstance(dimensions, list):
        raise SemanticQueryError(
            "Dimensions harus berupa list."
        )

    if not isinstance(filters, dict):
        raise SemanticQueryError(
            "Filters harus berupa dictionary."
        )

    if any(not isinstance(item, str) for item in dimensions):
        raise SemanticQueryError(
            "Setiap dimensi harus berupa teks."
        )

    if len(dimensions) > MAX_DIMENSIONS:
        raise SemanticQueryError(
            f"Maksimal {MAX_DIMENSIONS} dimensi per query."
        )

    if len(dimensions) != len(set(dimensions)):
        raise SemanticQueryError(
            "Dimensi duplikat tidak diperbolehkan."
        )

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise SemanticQueryError("Limit harus berupa integer.")

    if not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise SemanticQueryError(
            f"Limit harus antara {MIN_LIMIT} sampai {MAX_LIMIT}."
        )


def _resolve_filter_mode(key, filter_mode, raw_value):
    """
    Kompatibilitas: jika model mengirim kode sebagai nilai pada key
    dimensi biasa, arahkan ke kolom kode secara otomatis.
    """
    if filter_mode != "default" or key not in CODE_FILTER_DIMENSIONS:
        return filter_mode

    candidate_values = (
        list(raw_value)
        if isinstance(raw_value, (list, tuple, set))
        else [raw_value]
    )

    if candidate_values and all(
        _looks_like_code(key, item) for item in candidate_values
    ):
        return "code"

    return filter_mode


def _validate_sort(sort_by, order, allowed_sort_by=SORT_BY_OPTIONS):
    if sort_by is not None and sort_by not in allowed_sort_by:
        raise SemanticQueryError(
            "sort_by harus salah satu dari: "
            + ", ".join(sorted(allowed_sort_by))
        )

    if order is not None and order not in ORDER_OPTIONS:
        raise SemanticQueryError("order harus 'asc' atau 'desc'.")


def _check_split_metric_compatibility(metric_keys, metrics_config):
    for metric_key in metric_keys:
        aggregation = (
            metrics_config["metrics"][metric_key].get("aggregation") or {}
        )
        if aggregation.get("type") not in SPLIT_SAFE_AGGREGATIONS:
            raise SemanticQueryError(
                f"Metrik {metric_key} tidak dapat dikelompokkan berdasarkan "
                "dimensi multi-nilai (mis. diagnosis sekunder) karena nilai "
                "biaya akan terhitung berulang. Gunakan metrik jumlah."
            )


def _collect_query_parts(metric_keys, dimensions, filters):
    """
    Memvalidasi metrik, dimensi, dan filter lalu mengumpulkan
    potongan SQL (SELECT dimensi, GROUP BY, JOIN, WHERE, parameter).
    """

    metrics_config, dimensions_config = load_config()

    metric_expressions = [
        (metric_key, get_metric_expression(metric_key, metrics_config))
        for metric_key in metric_keys
    ]

    select_parts = []
    group_parts = []
    join_parts = []
    where_parts = []
    params = []
    has_split = False

    for key in dimensions:
        resolved = resolve_dimension(
            key,
            dimensions_config
        )

        select_parts.append(resolved["select"])

        groups = resolved["group"]

        if isinstance(groups, str):
            groups = [groups]

        group_parts.extend(groups)

        if "join" in resolved:
            join_parts.append(resolved["join"])

        if resolved.get("where"):
            where_parts.append(resolved["where"])

        if resolved.get("split"):
            has_split = True

    if has_split:
        _check_split_metric_compatibility(metric_keys, metrics_config)

    # Validasi dan normalisasi filter.
    # Filter dapat memakai nama dimensi, alias kode, atau alias label.
    for raw_key, raw_value in filters.items():

        key, filter_mode = normalize_filter_key(raw_key, dimensions_config)
        resolved = resolve_dimension(
            key,
            dimensions_config
        )

        filter_mode = _resolve_filter_mode(key, filter_mode, raw_value)

        value = normalize_filter_value(key, filter_mode, raw_value)

        if isinstance(value, (list, tuple, set)):
            values = list(dict.fromkeys(value))

            if not values:
                raise SemanticQueryError(
                    f"Filter kosong: {key}"
                )

            if len(values) > MAX_FILTER_VALUES:
                raise SemanticQueryError(
                    f"Terlalu banyak nilai filter: {key}"
                )
        else:
            if value is None:
                raise SemanticQueryError(
                    f"Nilai filter kosong: {key}"
                )
            values = None

        # Dimensi multi-nilai: cari nilai di dalam array tanpa join.
        if resolved.get("filter_any"):
            array_expression = resolved["filter_any"]
            items = values if values is not None else [value]
            conditions = [f"%s = ANY({array_expression})" for _ in items]
            where_parts.append(
                conditions[0] if len(conditions) == 1
                else "(" + " OR ".join(conditions) + ")"
            )
            params.extend(str(item) for item in items)
            continue

        if filter_mode == "code":
            filter_expression = resolved.get("filter_code")
            if not filter_expression:
                raise SemanticQueryError(
                    f"Filter kode belum didukung untuk dimensi: {key}"
                )
        else:
            filter_expression = resolved["filter"]

        if values is not None:
            placeholders = ", ".join(
                ["%s"] * len(values)
            )

            where_parts.append(
                f"{filter_expression} IN ({placeholders})"
            )

            params.extend(values)

        else:
            where_parts.append(
                f"{filter_expression} = %s"
            )

            params.append(value)

        if "join" in resolved and filter_mode != "code":
            join_parts.append(resolved["join"])

    return {
        "metric_expressions": metric_expressions,
        "select_parts": select_parts,
        "dimension_aliases": _aliases_of(select_parts),
        "group_parts": list(dict.fromkeys(group_parts)),
        "join_parts": list(dict.fromkeys(join_parts)),
        "where_parts": where_parts,
        "params": params,
        "has_split": has_split,
    }


def _aliases_of(select_parts):
    """Nama alias kolom dimensi, mis. ['divre_code', 'divre']."""
    return re.findall(r'AS "([a-z0-9_]+)"', ", ".join(select_parts))


def distinct_rewrite_enabled():
    """Dapat dimatikan dengan DISTINCT_COUNT_REWRITE=0 di .env."""
    return os.getenv("DISTINCT_COUNT_REWRITE", "1").strip() not in (
        "0", "false", "False", "no",
    )


def _uses_distinct_rewrite(metrics):
    return (
        len(metrics) == 1
        and metrics[0] in DISTINCT_COUNT_ENTITIES
        and distinct_rewrite_enabled()
    )


def _distinct_source(parts, metric):
    """
    Subquery berisi kombinasi unik (dimensi, entitas), mis.
    SELECT DISTINCT t."sex" AS "jenis_kelamin", t."member_id" AS "_entitas".
    COUNT(*) atas subquery ini sama dengan COUNT(DISTINCT entitas).
    """
    entity = DISTINCT_COUNT_ENTITIES[metric]

    select = parts["select_parts"] + [f'{entity} AS "_entitas"']
    where = parts["where_parts"] + [f"{entity} IS NOT NULL"]

    sql = "SELECT DISTINCT " + ", ".join(select)
    sql += f"\nFROM {ALLOWED_SOURCE} AS t"

    if parts["join_parts"]:
        sql += "\n" + "\n".join(parts["join_parts"])

    sql += "\nWHERE " + " AND ".join(where)
    return sql


def _from_where_group(parts):
    sql = f"\nFROM {ALLOWED_SOURCE} AS t"

    if parts["join_parts"]:
        sql += "\n" + "\n".join(parts["join_parts"])

    if parts["where_parts"]:
        sql += "\nWHERE " + " AND ".join(parts["where_parts"])

    if parts["group_parts"]:
        sql += "\nGROUP BY " + ", ".join(parts["group_parts"])

    return sql


def _order_clause(parts, dimensions, sort_metric, sort_by=None, order=None):
    """
    Default (sort_by dan order kosong): dimensi waktu kronologis, selain itu
    metrik menurun. sort_by="metric"/"dimension" dan order="asc"/"desc"
    dapat dipakai untuk "terbanyak", "paling sedikit", atau urutan kategori.
    """

    group_parts = parts["group_parts"]

    if not group_parts:
        return ""

    direction = (order or "desc").upper()

    if sort_by == "dimension":
        dimension_direction = (order or "asc").upper()
        return "\nORDER BY " + ", ".join(
            f"{expression} {dimension_direction}" for expression in group_parts
        )

    if sort_by is None and order is None:
        # Untuk rincian tahunan/bulanan, urutkan kronologis agar mudah
        # dibaca. Pengurutan metrik menurun tetap dipakai untuk dimensi
        # lainnya.
        time_dimensions = [
            f'"{dim}" ASC'
            for dim in CHRONOLOGICAL_DIMENSIONS
            if dim in dimensions
        ]

        if time_dimensions:
            return "\nORDER BY " + ", ".join(time_dimensions)

    # Kolom grup sebagai tie-breaker agar urutan hasil konsisten
    # ketika beberapa kelompok memiliki nilai metrik yang sama.
    return (
        f'\nORDER BY "{sort_metric}" {direction} NULLS LAST, '
        + ", ".join(group_parts)
    )


def build_query(
    metric,
    dimensions=None,
    filters=None,
    limit=20,
    sort_by=None,
    order=None,
):
    """
    Membentuk SQL terkontrol.

    Contoh:
    build_query(
        metric="total_pelayanan",
        dimensions=["jenis_pelayanan"]
    )
    """

    return build_multi_metric_query(
        [metric],
        dimensions=dimensions,
        filters=filters,
        limit=limit,
        sort_by=sort_by,
        order=order,
    )


def build_multi_metric_query(
    metrics,
    dimensions=None,
    filters=None,
    limit=20,
    sort_by=None,
    order=None,
):
    """
    Membentuk SQL dengan beberapa metrik sekaligus pada pengelompokan dan
    filter yang sama, misalnya jumlah pelayanan dan biaya verifikasi per
    DIVRE. sort_by dapat berupa "metric" (metrik pertama), "dimension",
    atau nama salah satu metrik.
    """

    dimensions = dimensions if dimensions is not None else []
    filters = filters if filters is not None else {}

    if not isinstance(metrics, list) or not metrics:
        raise SemanticQueryError("Metrics harus berupa list yang tidak kosong.")

    if any(not isinstance(item, str) for item in metrics):
        raise SemanticQueryError("Setiap metrik harus berupa teks.")

    if len(metrics) != len(set(metrics)):
        raise SemanticQueryError("Metrik duplikat tidak diperbolehkan.")

    if len(metrics) > 6:
        raise SemanticQueryError("Maksimal 6 metrik per query.")

    _validate_build_arguments(dimensions, filters, limit)
    _validate_sort(sort_by, order, SORT_BY_OPTIONS | set(metrics))

    parts = _collect_query_parts(metrics, dimensions, filters)

    select_parts = parts["select_parts"] + [
        f'{expression} AS "{metric_key}"'
        for metric_key, expression in parts["metric_expressions"]
    ]

    sort_metric = metrics[0]
    if sort_by in metrics:
        sort_metric = sort_by
        sort_by = "metric"

    if _uses_distinct_rewrite(metrics):
        metric = metrics[0]
        aliases = [f'd."{alias}"' for alias in parts["dimension_aliases"]]

        sql = "SELECT " + ", ".join(aliases + [f'COUNT(*) AS "{metric}"'])
        sql += "\nFROM (\n" + _distinct_source(parts, metric) + "\n) AS d"

        if aliases:
            sql += "\nGROUP BY " + ", ".join(aliases)

        sql += _order_clause(
            {"group_parts": aliases}, dimensions, sort_metric, sort_by, order
        )
    else:
        sql = "SELECT " + ", ".join(select_parts) + _from_where_group(parts)
        sql += _order_clause(parts, dimensions, sort_metric, sort_by, order)

    sql += "\nLIMIT %s"
    params = parts["params"] + [limit]

    return sql, params


def build_top_n_query(
    metric,
    dimensions,
    group_by=None,
    filters=None,
    top_n=5,
    order="desc",
):
    """
    Mengambil N kategori teratas (atau terbawah) suatu metrik, opsional
    per kelompok. Contoh: 5 diagnosis primer terbanyak untuk setiap
    jenis pelayanan.
    """

    group_by = group_by if group_by is not None else []
    filters = filters if filters is not None else {}
    order = order or "desc"

    if not isinstance(dimensions, list) or not dimensions:
        raise SemanticQueryError("Dimensions minimal berisi satu dimensi.")

    if not isinstance(group_by, list):
        raise SemanticQueryError("group_by harus berupa list.")

    if set(dimensions) & set(group_by):
        raise SemanticQueryError(
            "Dimensi yang sama tidak boleh menjadi dimensions dan group_by."
        )

    if isinstance(top_n, bool) or not isinstance(top_n, int):
        raise SemanticQueryError("top_n harus berupa integer.")

    if not 1 <= top_n <= MAX_TOP_N:
        raise SemanticQueryError(f"top_n harus antara 1 sampai {MAX_TOP_N}.")

    _validate_sort(None, order)

    all_dimensions = group_by + dimensions
    _validate_build_arguments(all_dimensions, filters, MAX_LIMIT)

    parts = _collect_query_parts([metric], all_dimensions, filters)
    metric_expression = parts["metric_expressions"][0][1]

    metrics_config, dimensions_config = load_config()

    partition_expressions = []
    for key in group_by:
        groups = resolve_dimension(key, dimensions_config)["group"]
        partition_expressions.extend(
            [groups] if isinstance(groups, str) else groups
        )

    if _uses_distinct_rewrite([metric]):
        # Versi cepat untuk metrik distinct count (lihat DISTINCT_COUNT_ENTITIES).
        group_aliases = []
        for key in group_by:
            group_aliases.extend(
                _aliases_of([resolve_dimension(key, dimensions_config)["select"]])
            )

        all_aliases = parts["dimension_aliases"]
        partition = [f'd."{alias}"' for alias in group_aliases]
        others = [f'd."{alias}"' for alias in all_aliases if alias not in group_aliases]

        window = "ROW_NUMBER() OVER ("
        if partition:
            window += "PARTITION BY " + ", ".join(partition) + " "
        window += "ORDER BY " + ", ".join([f"COUNT(*) {order.upper()}"] + others) + ")"

        quoted = [f'd."{alias}"' for alias in all_aliases]
        inner_sql = (
            "SELECT " + ", ".join(
                quoted + [f'COUNT(*) AS "{metric}"', f'{window} AS "peringkat"']
            )
            + "\nFROM (\n" + _distinct_source(parts, metric) + "\n) AS d"
            + "\nGROUP BY " + ", ".join(quoted)
        )
    else:
        rank_order = [f"{metric_expression} {order.upper()}"]
        rank_order.extend(
            expression for expression in parts["group_parts"]
            if expression not in partition_expressions
        )

        window = "ROW_NUMBER() OVER ("
        if partition_expressions:
            window += "PARTITION BY " + ", ".join(partition_expressions) + " "
        window += "ORDER BY " + ", ".join(rank_order) + ")"

        inner_select = parts["select_parts"] + [
            f'{metric_expression} AS "{metric}"',
            f'{window} AS "peringkat"',
        ]

        inner_sql = "SELECT " + ", ".join(inner_select) + _from_where_group(parts)

    outer_order = [f's."{key}"' for key in group_by] + ['s."peringkat"']

    sql = (
        "SELECT *\nFROM (\n" + inner_sql + "\n) AS s"
        + '\nWHERE s."peringkat" <= %s'
        + "\nORDER BY " + ", ".join(outer_order)
        + "\nLIMIT %s"
    )

    params = parts["params"] + [top_n, MAX_LIMIT]

    return sql, params


def uses_split_dimension(dimensions):
    """True jika salah satu dimensi memecah baris (mis. diagnosis sekunder)."""
    _, dimensions_config = load_config()
    configured = dimensions_config.get("dimensions", {})
    return any(
        bool((configured.get(key) or {}).get("split_strategy"))
        for key in dimensions or []
        if isinstance(key, str)
    )


if __name__ == "__main__":

    sql, params = build_query(
        metric="total_pelayanan",
        dimensions=["jenis_pelayanan"],
        limit=20,
    )

    print("\n=== GENERATED SQL ===")
    print(sql)

    print("\n=== PARAMETERS ===")
    print(params)