"""
BPJS Analytics Chatbot: Qwen (Ollama) + MCP Client.

Alur:
1. Qwen menerima pertanyaan pengguna dan memilih MCP tool.
2. Argumen tool dinormalisasi (alias metrik/dimensi/filter) dan dikunci
   terhadap kode/tahun yang disebut eksplisit pada pertanyaan.
3. MCP Server menjalankan query terkontrol ke PostgreSQL.
4. Hasil jumlah pelayanan diverifikasi dengan query total terpisah dan
   diformat oleh Python; jawaban lain disusun Qwen dari hasil MCP.
"""

import asyncio
import json
import logging
import os
import re
import sys
from contextlib import AsyncExitStack
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import httpx
import ollama
from dotenv import load_dotenv

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters
from mcp_server.calculation_engine import percentage

from ai.answer_support import (
    compact_tool_output,
    data_table_enabled,
    find_unverified_numbers,
    format_data_tables,
    format_number_for_model,
    number_check_enabled,
    numbers_without_source,
    select_tools,
    unverified_warning,
)
from ai.charts import build_charts
from ai.prompts import build_system_prompt


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Baca .env (OLLAMA_MODEL, OLLAMA_NUM_CTX, dll.) untuk proses chatbot.
# Variabel yang sudah di-set di terminal tetap diutamakan.
load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger("bpjs.chatbot")

DEFAULT_CONVERSATION_ID = "default"


def _env_int(name, default, minimum=None, maximum=None):
    try:
        value = int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def _env_float(name, default):
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# System prompt lengkap (semua tool). Per pertanyaan, prompt yang dikirim ke
# Qwen hanya memuat panduan tool yang relevan (lihat ai/prompts.py).
SYSTEM_PROMPT = build_system_prompt()


# =========================================================
# ALIAS & KONSTANTA NORMALISASI ARGUMEN
# =========================================================

# Alias metrik: semua alias diarahkan ke nama metrik resmi.
METRIC_ALIASES = {
    "jumlah_pelayanan": "total_pelayanan",
    "banyak_pelayanan": "total_pelayanan",
    "jumlah_sjp": "total_sjp_unik",
    "total_sjp": "total_sjp_unik",
    "jumlah_peserta": "total_peserta_unik",
    "total_peserta": "total_peserta_unik",
    "peserta": "total_peserta_unik",
    "jumlah_peserta_unik": "total_peserta_unik",
    "rata_rata_biaya": "rata_rata_biaya_verifikasi",
    "rata-rata_biaya_verifikasi": "rata_rata_biaya_verifikasi",
    "biaya_verifikasi": "total_biaya_verifikasi",
    "biaya_tagihan": "total_biaya_tagihan",
    "biaya_rs": "total_biaya_rs",
    "biaya_rumah_sakit": "total_biaya_rs",
    "total_biaya_rumah_sakit": "total_biaya_rs",
    "selisih_biaya": "selisih_tagihan_verifikasi",
    "selisih": "selisih_tagihan_verifikasi",
    "median_biaya": "median_biaya_verifikasi",
    "rata_rata_biaya_rumah_sakit": "rata_rata_biaya_rs",
    "biaya_per_peserta": "biaya_verifikasi_per_peserta",
    "kunjungan_per_peserta": "rata_rata_pelayanan_per_peserta",
    "pelayanan_per_peserta": "rata_rata_pelayanan_per_peserta",
}

# Alias dimensi: diarahkan ke nama dimensi dalam katalog.
DIMENSION_ALIASES = {
    "segmen": "segmen_peserta",
    "segmen peserta": "segmen_peserta",
    "segment": "segmen_peserta",
    "jenis kelamin": "jenis_kelamin",
    "jenis pelayanan": "jenis_pelayanan",
    "divisi regional": "divre",
    "regional divisi": "divre",
    "divre": "divre",
    "nama divre": "divre",
    "regional": "regional_tarif",
    "regional tarif": "regional_tarif",
    "faskes": "fasilitas_kesehatan",
    "fasilitas kesehatan": "fasilitas_kesehatan",
    "rumah sakit": "fasilitas_kesehatan",
    "rs": "fasilitas_kesehatan",
    "cabang": "cabang",
    "kelompok umur": "kelompok_umur",
    "kelas hak": "kelas_hak",
    "tanggal pelayanan": "tanggal_pelayanan",
    "tahun pelayanan": "tahun_pelayanan",
    "bulan pelayanan": "bulan_pelayanan",
    # Variasi umum lain dari Qwen.
    "tahun": "tahun_pelayanan",
    "bulan": "bulan_pelayanan",
    "umur": "kelompok_umur",
    "usia": "kelompok_umur",
    "gender": "jenis_kelamin",
    "wilayah": "divre",
    "region": "divre",
    "diagnosis": "diagnosis_primer",
    "diagnosa": "diagnosis_primer",
    "diagnosis utama": "diagnosis_primer",
    "diagnosa primer": "diagnosis_primer",
    "diagnosa sekunder": "diagnosis_sekunder",
    "keparahan": "severity_level",
    "tingkat keparahan": "severity_level",
    "severity": "severity_level",
    "kronis": "kategori_kronis",
    "penyakit kronis": "kategori_kronis",
    "katastrofik": "kategori_katastrofik",
    "penyakit katastrofik": "kategori_katastrofik",
    "kepulangan": "jenis_kepulangan",
    "jenis pulang": "jenis_kepulangan",
    "cara pulang": "jenis_kepulangan",
    "kelas rawat": "kelas_rawat",
    "segmen kepesertaan": "segmen_peserta",
    "kepesertaan": "segmen_peserta",
    "jenis faskes": "jenis_fasilitas",
    "tipe faskes": "tipe_fasilitas",
    "tipe rs": "tipe_fasilitas",
    "kelas rs": "tipe_fasilitas",
    "kepemilikan": "kepemilikan_fasilitas",
    "kepemilikan rs": "kepemilikan_fasilitas",
    "umur": "kelompok_umur",
}

# Alias kode wilayah/cabang/rumah sakit.
CLIENT_FILTER_ALIASES = {
    "kode_divre": "divre_code",
    "kode_divisi_regional": "divre_code",
    "divisi_regional_code": "divre_code",
    "kode_cabang": "cabang_code",
    "branch_code": "cabang_code",
    "service_branch_code": "cabang_code",
    "kode_faskes": "fasilitas_kesehatan_code",
    "kode_rs": "fasilitas_kesehatan_code",
    "kode_rumah_sakit": "fasilitas_kesehatan_code",
    "hospital_code": "fasilitas_kesehatan_code",
    "facility_code": "fasilitas_kesehatan_code",
    "nama_rs": "nama_rumah_sakit",
    "nama_faskes": "nama_rumah_sakit",
}

# Qwen kadang mengirim segment, dimension, atau group_by
# sebagai parameter terpisah. Nilainya digabung ke dimensions.
DIMENSION_ARGUMENT_ALIASES = ("segment", "dimension", "group_by", "groupby")

# Tool yang menerima parameter filters.
FILTER_TOOLS = (
    "analyze_data", "compare_costs", "service_mix", "metric_share",
    "compare_metrics", "top_categories", "profile_summary",
)

# Tool yang menerima metric + dimensions.
DIMENSION_TOOLS = ("analyze_data", "metric_share")

GROWTH_TOOLS = ("service_growth", "metric_growth")

# Argumen yang dikenali masing-masing tool di MCP Server.
TOOL_ALLOWED_ARGUMENTS = {
    "analyze_data": ("metric", "dimensions", "filters", "limit", "sort_by", "order"),
    "metric_share": (
        "metric", "dimensions", "filters", "limit", "within", "sort_by", "order",
    ),
    "compare_metrics": (
        "metrics", "dimensions", "filters", "limit", "sort_by", "order",
    ),
    "top_categories": (
        "metric", "dimensions", "group_by", "filters", "top_n", "order",
    ),
    "profile_summary": ("dimensions", "filters"),
    "compare_costs": ("filters", "dimensions", "limit"),
}

PERCENTAGE_KEYWORDS = ("persentase", "persen", "proporsi", "%")

# Variasi nilai order dari Qwen.
ORDER_ALIASES = {
    "desc": "desc", "descending": "desc", "menurun": "desc", "turun": "desc",
    "terbanyak": "desc", "tertinggi": "desc", "terbesar": "desc", "top": "desc",
    "highest": "desc", "most": "desc",
    "asc": "asc", "ascending": "asc", "menaik": "asc", "naik": "asc",
    "terendah": "asc", "tersedikit": "asc", "paling sedikit": "asc",
    "terkecil": "asc", "lowest": "asc", "least": "asc", "bottom": "asc",
}

SORT_BY_ALIASES = {
    "metric": "metric", "metrik": "metric", "nilai": "metric", "value": "metric",
    "jumlah": "metric",
    "dimension": "dimension", "dimensi": "dimension", "kategori": "dimension",
    "category": "dimension", "label": "dimension", "nama": "dimension",
}

# Pertanyaan analitis yang memerlukan penjelasan Qwen (bukan hanya daftar
# angka), sehingga hasil jumlah pelayanan tidak langsung diformat Python.
ANALYTICAL_QUESTION_PATTERN = re.compile(
    r"\b(?:paling|ter(?:banyak|tinggi|rendah|sedikit|besar|kecil)|apakah|"
    r"mana|bandingkan|perbandingan|dibandingkan|beda|perbedaan|temuan|"
    r"ringkas(?:an)?|insight|mengapa|kenapa|biaya|peserta|rata|diagnos|"
    r"karakteristik|tren|pola)\w*",
    re.IGNORECASE,
)

_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.IGNORECASE | re.DOTALL)

# Kode (cabang/RS) wajib memuat setidaknya satu digit agar kata biasa
# seperti "tersebut" atau "dengan" tidak dianggap sebagai kode.
_CODE_TOKEN = r"(?=[A-Z0-9_-]*\d)([A-Z0-9_-]{%d,})"

_DIVRE_PATTERN = re.compile(
    r"\b(?:divre|divisi\s+regional)\s*(?:nomor|no\.?|ke-)?\s*0*(\d{1,2})\b",
    re.IGNORECASE,
)

_BRANCH_PATTERN = re.compile(
    r"\bkode\s+cabang\s*(?:adalah|:|nomor|no\.?|=)?\s*" + _CODE_TOKEN % 2 + r"\b",
    re.IGNORECASE,
)

_HOSPITAL_PATTERNS = (
    re.compile(
        r"\bkode\s+(?:rumah\s+sakit|rs|faskes|fasilitas\s+kesehatan)"
        r"\s*(?:adalah|:|nomor|no\.?|=)?\s*" + _CODE_TOKEN % 4 + r"\b",
        re.IGNORECASE,
    ),
    # Terima juga susunan bahasa: "rumah sakit dengan kode 10015851".
    re.compile(
        r"\b(?:rumah\s+sakit|rs|faskes|fasilitas\s+kesehatan)"
        r"\s+(?:dengan\s+)?kode\s*(?:adalah|:|nomor|no\.?|=)?"
        r"\s*" + _CODE_TOKEN % 4 + r"\b",
        re.IGNORECASE,
    ),
)

_YEAR_PATTERN = re.compile(r"\b(?:19|20)\d{2}\b")

# Deteksi permintaan rincian tahunan dengan variasi bahasa alami,
# termasuk bentuk tanpa spasi seperti "pertahunnya".
_YEAR_BREAKDOWN_PATTERN = re.compile(
    r"(?:"
    r"\b(setiap|tiap)\s+tahun(?:nya)?\b|"
    r"\bper\s*tahun(?:nya)?\b|"
    r"\bberdasarkan\s+tahun\b|"
    r"\btahunan\b|"
    r"\bmasing[- ]masing\s+tahun\b|"
    r"\bdari\s+tahun\s+ke\s+tahun\b|"
    r"\brincian(?:nya)?\s+(?:per\s*)?tahun\b"
    r")",
    re.IGNORECASE,
)


# =========================================================
# FUNGSI NORMALISASI (MURNI, MUDAH DIUJI)
# =========================================================

def normalize_metric(value):
    if not isinstance(value, str):
        raise ValueError("Parameter metric harus berupa string.")

    metric_key = value.strip().casefold()
    if not metric_key:
        raise ValueError("Parameter metric tidak boleh kosong.")

    metric_key = metric_key.replace(" ", "_")
    return METRIC_ALIASES.get(metric_key, metric_key)


def normalize_year(value, parameter_name):
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())

    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(
            f"Parameter {parameter_name} harus berupa tahun integer."
        )

    return value


def _to_int_if_digit(item):
    if isinstance(item, str) and item.strip().isdigit():
        return int(item.strip())
    return item


def normalize_filters(filters):
    """Normalisasi key filter (alias) dan nilai tahun/bulan."""

    filters = filters or {}

    if not isinstance(filters, dict):
        raise ValueError(
            "Format filters harus berupa dictionary."
        )

    filters = dict(filters)

    # Alias filter tahun.
    if "tahun" in filters:
        if (
            "tahun_pelayanan" in filters
            and filters["tahun_pelayanan"] != filters["tahun"]
        ):
            raise ValueError(
                "Filter tahun memiliki nilai yang bertentangan."
            )
        filters["tahun_pelayanan"] = filters.pop("tahun")

    normalized_filters = {}
    for key, value in filters.items():
        normalized_key = str(key).strip().casefold().replace(" ", "_")
        normalized_key = CLIENT_FILTER_ALIASES.get(
            normalized_key, normalized_key
        )
        if (
            normalized_key in normalized_filters
            and normalized_filters[normalized_key] != value
        ):
            raise ValueError(
                f"Filter memiliki nilai bertentangan: {normalized_key}"
            )
        normalized_filters[normalized_key] = value
    filters = normalized_filters

    # Tahun/bulan numerik menjadi integer; list tahun tetap list
    # agar dapat menghasilkan rincian per tahun.
    for key in ("tahun_pelayanan", "bulan_pelayanan"):
        value = filters.get(key)
        if isinstance(value, list):
            filters[key] = [_to_int_if_digit(item) for item in value]
        elif isinstance(value, str) and value.strip().isdigit():
            filters[key] = int(value.strip())

    return filters


def normalize_dimensions(arguments):
    """
    Menerima beberapa variasi nama parameter dimensi dari Qwen dan
    mengembalikan list nama dimensi resmi. Dapat memindahkan tahun ke
    arguments["filters"] jika dikirim dalam bentuk dictionary.
    """

    dimensions = arguments.get("dimensions")

    extra_dimensions = []

    for alias_key in DIMENSION_ARGUMENT_ALIASES:
        alias_value = arguments.pop(alias_key, None)

        if alias_value is None:
            continue

        if isinstance(alias_value, str):
            if alias_value.strip():
                extra_dimensions.append(alias_value.strip())
        elif isinstance(alias_value, list):
            for item in alias_value:
                if not isinstance(item, str) or not item.strip():
                    raise ValueError(
                        f"Setiap nilai {alias_key} harus berupa string."
                    )
                extra_dimensions.append(item.strip())
        else:
            raise ValueError(
                f"Parameter {alias_key} harus berupa string atau list."
            )

    # Normalisasi bentuk dimensions.
    if dimensions is None:
        dimensions = []
    elif isinstance(dimensions, str):
        dimensions = [dimensions] if dimensions.strip() else []
    elif isinstance(dimensions, dict):
        normalized_from_dict = []
        filters = arguments.setdefault("filters", {})

        for key, value in dimensions.items():
            key_normalized = str(key).strip().casefold()

            # Tahun adalah filter, bukan dimensi pengelompokan.
            if key_normalized in ("tahun", "tahun_pelayanan"):
                if isinstance(value, list):
                    filters["tahun_pelayanan"] = [
                        int(str(item).strip())
                        if str(item).strip().isdigit() else item
                        for item in value
                    ]
                elif str(value).strip().isdigit():
                    filters["tahun_pelayanan"] = int(
                        str(value).strip()
                    )
                continue

            if isinstance(value, str) and value.strip():
                normalized_from_dict.append(value.strip())
            elif isinstance(key, str) and key.strip():
                # Mendukung bentuk {nama_dimensi: true}.
                if value is True:
                    normalized_from_dict.append(key.strip())

        dimensions = normalized_from_dict

    if not isinstance(dimensions, list):
        raise ValueError(
            "Dimensions harus berupa list, string, atau dictionary."
        )

    normalized_dimensions = []

    for dim in dimensions + extra_dimensions:
        if not isinstance(dim, str) or not dim.strip():
            raise ValueError(
                "Setiap dimension harus berupa string yang tidak kosong."
            )

        dim_key = dim.strip().casefold()

        # Tahun boleh menjadi dimensi grouping. Jika pengguna
        # meminta beberapa tahun, hasil harus dipecah per tahun.
        normalized_dim = DIMENSION_ALIASES.get(
            dim_key,
            DIMENSION_ALIASES.get(dim_key.replace("_", " "), dim.strip()),
        )

        if normalized_dim not in normalized_dimensions:
            normalized_dimensions.append(normalized_dim)

    return normalized_dimensions


def normalize_dimension_names(values, parameter_name="dimensions"):
    """Normalisasi list nama dimensi (alias -> nama resmi), tanpa duplikat."""
    if values is None:
        return []
    if isinstance(values, str):
        values = [item for item in re.split(r"\s*,\s*", values) if item.strip()]
    if not isinstance(values, list):
        raise ValueError(f"Parameter {parameter_name} harus berupa list.")

    normalized = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Setiap nilai {parameter_name} harus berupa string."
            )
        key = item.strip().casefold()
        name = DIMENSION_ALIASES.get(
            key, DIMENSION_ALIASES.get(key.replace("_", " "), item.strip())
        )
        if name not in normalized:
            normalized.append(name)
    return normalized


def normalize_order(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Parameter order harus berupa teks.")
    key = value.strip().casefold().replace("_", " ")
    if not key:
        return None
    if key not in ORDER_ALIASES:
        raise ValueError("Parameter order harus 'asc' atau 'desc'.")
    return ORDER_ALIASES[key]


def normalize_sort_by(value, metrics=()):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("Parameter sort_by harus berupa teks.")
    key = value.strip().casefold()
    if not key:
        return None
    if key in SORT_BY_ALIASES:
        return SORT_BY_ALIASES[key]
    metric_key = normalize_metric(key)
    if metric_key in metrics:
        return metric_key
    raise ValueError("Parameter sort_by harus 'metric', 'dimension', atau nama metrik.")


def normalize_metric_list(value):
    if isinstance(value, str):
        value = [item for item in re.split(r"\s*,\s*", value) if item.strip()]
    if not isinstance(value, list) or not value:
        raise ValueError("Parameter metrics harus berupa list metrik.")
    metrics = []
    for item in value:
        metric = normalize_metric(item)
        if metric not in metrics:
            metrics.append(metric)
    return metrics


def normalize_top_n(value, default=5):
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Parameter top_n harus berupa integer.")
    return max(1, min(50, value))


def normalize_limit(value, default=20):
    if value is None:
        return default
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Parameter limit harus berupa integer.")
    # Batasi ke rentang yang diizinkan Semantic Engine (1-100).
    return max(1, min(100, value))


# Tool yang dapat diberi rincian per tahun bila pertanyaan menyebut
# beberapa tahun (analyze_data total_pelayanan ditangani terpisah).
YEAR_BREAKDOWN_TOOLS = ("analyze_data", "compare_metrics", "compare_costs", "metric_share")

_RITL_PATTERN = re.compile(r"\b(?:ritl|rawat[\s-]+inap)\b", re.IGNORECASE)
_RJTL_PATTERN = re.compile(r"\b(?:rjtl|rawat[\s-]+jalan)\b", re.IGNORECASE)

# Kata yang menandakan rentang tahun, bukan satu tahun tertentu.
_YEAR_RANGE_PATTERN = re.compile(
    r"\b(?:sejak|setelah|sebelum|hingga|sampai|antara|s\.?\s?d\.?|"
    r"selain|kecuali)\b",
    re.IGNORECASE,
)

# Alias filter jenis pelayanan yang mungkin dikirim Qwen.
_SERVICE_FILTER_KEYS = ("jenis_pelayanan", "jenis pelayanan", "service_type", "pelayanan")


def _grouped_dimensions(arguments):
    """Dimensi yang dipakai untuk pengelompokan (dimensions + group_by)."""
    grouped = []
    for key in ("dimensions", "group_by"):
        value = arguments.get(key) or []
        if isinstance(value, str):
            value = [value]
        if isinstance(value, list):
            grouped.extend(item for item in value if isinstance(item, str))
    return grouped


def apply_explicit_question_constraints(
    question,
    arguments,
    include_year_breakdown=True,
    tool_name=None,
):
    """Pastikan kode/tahun eksplisit dari pertanyaan tidak diganti Qwen."""
    if not isinstance(question, str) or not isinstance(arguments, dict):
        return arguments

    filters = arguments.get("filters")
    if not isinstance(filters, dict):
        filters = {}

    # Contoh: "DIVRE 10 tahun 2024" -> divre_code = "10".
    # Beberapa DIVRE ("DIVRE 1 dan DIVRE 2") menjadi list kode.
    divre_codes = list(dict.fromkeys(
        f"{int(code):02d}" for code in _DIVRE_PATTERN.findall(question)
    ))
    if divre_codes:
        # Pertanyaan eksplisit menjadi sumber kebenaran; hapus alias
        # label yang mungkin berisi kode keliru dari hasil interpretasi Qwen.
        filters.pop("divre", None)
        filters.pop("nama_divre", None)
        filters.pop("kode_divre", None)
        filters["divre_code"] = (
            divre_codes[0] if len(divre_codes) == 1 else divre_codes
        )

    # Kode cabang eksplisit, misalnya "kode cabang 0121".
    branch_match = _BRANCH_PATTERN.search(question)
    if branch_match:
        filters.pop("cabang", None)
        filters.pop("nama_cabang", None)
        filters.pop("kode_cabang", None)
        filters["cabang_code"] = branch_match.group(1).upper()

    # Kode rumah sakit/faskes eksplisit.
    hospital_match = None
    for pattern in _HOSPITAL_PATTERNS:
        hospital_match = pattern.search(question)
        if hospital_match:
            break
    if hospital_match:
        filters.pop("fasilitas_kesehatan", None)
        filters.pop("nama_rumah_sakit", None)
        filters.pop("nama_rs", None)
        filters.pop("kode_rs", None)
        filters["fasilitas_kesehatan_code"] = hospital_match.group(1).upper()

    grouped = _grouped_dimensions(arguments)

    # Jenis pelayanan eksplisit: "rawat inap"/RITL atau "rawat jalan"/RJTL.
    # Hanya dikunci jika tepat satu jenis disebut dan jenis pelayanan tidak
    # sedang dipakai sebagai pengelompokan/perbandingan.
    mentions_ritl = bool(_RITL_PATTERN.search(question))
    mentions_rjtl = bool(_RJTL_PATTERN.search(question))
    if (
        tool_name != "service_mix"
        and mentions_ritl != mentions_rjtl
        and "jenis_pelayanan" not in grouped
    ):
        for key in _SERVICE_FILTER_KEYS:
            filters.pop(key, None)
        filters["jenis_pelayanan"] = "RITL" if mentions_ritl else "RJTL"

    # Satu tahun eksplisit ("tahun 2024") menjadi filter tahun.
    explicit_years = sorted({int(value) for value in _YEAR_PATTERN.findall(question)})
    if (
        len(explicit_years) == 1
        and "tahun_pelayanan" not in grouped
        and not _YEAR_RANGE_PATTERN.search(question)
    ):
        filters.pop("tahun", None)
        filters["tahun_pelayanan"] = explicit_years[0]

    if include_year_breakdown:
        # Ambil beberapa tahun eksplisit untuk membuat rincian per tahun.
        years = sorted({int(value) for value in _YEAR_PATTERN.findall(question)})
        asks_year_breakdown = bool(_YEAR_BREAKDOWN_PATTERN.search(question))

        if arguments.get("metric") == "total_pelayanan" and (
            len(years) >= 2 or asks_year_breakdown
        ):
            if len(years) >= 2:
                filters.pop("tahun", None)
                filters["tahun_pelayanan"] = years

            dimensions = arguments.get("dimensions") or []
            if isinstance(dimensions, str):
                dimensions = [dimensions]
            else:
                dimensions = list(dimensions)

            if "tahun_pelayanan" not in dimensions:
                dimensions.append("tahun_pelayanan")

            arguments["dimensions"] = dimensions

            # Dataset dapat memiliki lebih dari 20 tahun/kategori.
            # Naikkan limit agar rincian tahunan tidak terpotong.
            try:
                current_limit = int(arguments.get("limit") or 20)
            except (TypeError, ValueError):
                current_limit = 20
            arguments["limit"] = max(current_limit, 100)

    # Beberapa tahun eksplisit ("tahun 2023 dan 2024") untuk tool lain:
    # tampilkan rincian per tahun, bukan angka gabungan.
    if tool_name in YEAR_BREAKDOWN_TOOLS and not _YEAR_RANGE_PATTERN.search(question):
        years = sorted({int(value) for value in _YEAR_PATTERN.findall(question)})
        dimensions = arguments.get("dimensions") or []
        dimensions = [dimensions] if isinstance(dimensions, str) else list(dimensions)

        if (
            len(years) >= 2
            and "tahun_pelayanan" not in dimensions
            and len(dimensions) < 5
        ):
            filters.pop("tahun", None)
            filters["tahun_pelayanan"] = years

            if tool_name == "metric_share":
                # Persentase dihitung di dalam setiap tahun.
                arguments["dimensions"] = ["tahun_pelayanan"] + dimensions
                within = arguments.get("within") or []
                within = [within] if isinstance(within, str) else list(within)
                arguments["within"] = ["tahun_pelayanan"] + [
                    item for item in within if item != "tahun_pelayanan"
                ]
            else:
                arguments["dimensions"] = dimensions + ["tahun_pelayanan"]

    arguments["filters"] = filters
    return arguments


def _raise_limit_for_crosstab(arguments):
    """
    Tabulasi silang (>= 2 dimensi) mudah melebihi 20 baris. Jika Qwen tidak
    menentukan limit (atau memakai default 20), naikkan ke 100 agar tidak
    ada kategori yang hilang.
    """
    dimensions = arguments.get("dimensions") or []
    if not isinstance(dimensions, list) or len(dimensions) < 2:
        return arguments
    limit = arguments.get("limit")
    if limit is None or limit == 20:
        arguments["limit"] = 100
    return arguments


def _normalize_sort_arguments(arguments, metrics=()):
    if "order" in arguments:
        arguments["order"] = normalize_order(arguments.get("order"))
        if arguments["order"] is None:
            arguments.pop("order")
    if "sort_by" in arguments:
        arguments["sort_by"] = normalize_sort_by(arguments.get("sort_by"), metrics)
        if arguments["sort_by"] is None:
            arguments.pop("sort_by")


def _keep_allowed_arguments(name, arguments):
    allowed = TOOL_ALLOWED_ARGUMENTS[name]
    return {
        key: value for key, value in arguments.items()
        if key in allowed
    }


def should_format_directly(question):
    """
    Jumlah pelayanan diformat langsung oleh Python (tanpa narasi Qwen) hanya
    untuk pertanyaan hitungan sederhana. Pertanyaan analitis tetap
    dijawab Qwen agar dapat membandingkan atau memanggil tool lanjutan.
    """
    return not (
        isinstance(question, str)
        and ANALYTICAL_QUESTION_PATTERN.search(question)
    )


_BREAKDOWN_WORDS = r"(?:di\s+)?(?:setiap|tiap|per|masing[- ]masing|berdasarkan|menurut)"


def _breakdown_alias_pattern():
    aliases = set(DIMENSION_ALIASES)
    aliases.update(name.replace("_", " ") for name in DIMENSION_ALIASES.values())
    ordered = sorted(aliases, key=len, reverse=True)
    return re.compile(
        _BREAKDOWN_WORDS + r"\s+(" + "|".join(re.escape(a) for a in ordered) + r")\b",
        re.IGNORECASE,
    )


_BREAKDOWN_PATTERN = None


def breakdown_dimensions(question):
    """
    Dimensi yang diminta sebagai rincian, mis. "di setiap kelompok umur" ->
    ["kelompok_umur"], "per DIVRE dan per segmen" -> ["divre", "segmen_peserta"].
    """
    global _BREAKDOWN_PATTERN
    if not isinstance(question, str):
        return []
    if _BREAKDOWN_PATTERN is None:
        _BREAKDOWN_PATTERN = _breakdown_alias_pattern()

    found = []
    for match in _BREAKDOWN_PATTERN.finditer(question):
        alias = match.group(1).casefold()
        name = DIMENSION_ALIASES.get(alias, alias.replace(" ", "_"))
        if name not in found:
            found.append(name)
    return found


def _filter_keys_for_dimension(filters, dimension):
    """Key filter yang merujuk ke dimensi (nama resmi atau alias label)."""
    keys = []
    for key in filters:
        normalized = str(key).strip().casefold().replace(" ", "_")
        if normalized == dimension or DIMENSION_ALIASES.get(
            normalized.replace("_", " ")
        ) == dimension:
            keys.append(key)
    return keys


def _apply_breakdown_dimensions(name, arguments, question):
    """
    Dimensi yang diminta dirinci ("di setiap kelompok umur") harus menjadi
    pengelompokan, bukan filter. Nilai filter tebakan Qwen untuk dimensi itu
    dibuang (mis. kelompok_umur = "0-14", "15-24", ... yang tidak ada di data).
    """
    if name == "top_categories":
        return _apply_breakdown_to_top_categories(arguments, question)

    if name not in ("analyze_data", "metric_share", "compare_metrics", "compare_costs"):
        return arguments

    wanted = [
        dim for dim in breakdown_dimensions(question)
        if dim not in ("tahun_pelayanan", "bulan_pelayanan")
    ]
    if not wanted:
        return arguments

    filters = arguments.get("filters") or {}
    dimensions = list(arguments.get("dimensions") or [])

    for dim in wanted:
        for key in _filter_keys_for_dimension(filters, dim):
            # Jenis pelayanan yang dikunci dari pertanyaan tetap dipakai.
            if dim == "jenis_pelayanan":
                continue
            logger.info("[Guard] Filter %s dipindah menjadi pengelompokan.", key)
            filters.pop(key, None)
        if dim not in dimensions and dim != "jenis_pelayanan" and len(dimensions) < 5:
            dimensions.append(dim)

    arguments["filters"] = filters
    if dimensions:
        arguments["dimensions"] = dimensions

    # "Persentase/distribusi A di setiap B" -> persentase di dalam kelompok B.
    if name == "metric_share" and not arguments.get("within"):
        within = [dim for dim in wanted if dim in dimensions]
        if within and len(within) < len(dimensions):
            arguments["within"] = within

    return arguments


def _apply_breakdown_to_top_categories(arguments, question):
    """
    "3 diagnosis terbanyak pada setiap kelompok umur": kelompok umur adalah
    group_by (peringkat dihitung di dalam tiap kelompok), bukan dimensi yang
    diperingkat bersama diagnosis.
    """
    wanted = [
        dim for dim in breakdown_dimensions(question)
        if dim not in ("tahun_pelayanan", "bulan_pelayanan")
    ]
    if not wanted:
        return arguments

    dimensions = list(arguments.get("dimensions") or [])
    group_by = list(arguments.get("group_by") or [])
    filters = arguments.get("filters") or {}

    ranked = [dim for dim in dimensions if dim not in wanted]
    if not ranked:
        # Semua dimensi adalah rincian -> tidak ada yang diperingkat; biarkan.
        return arguments

    for dim in wanted:
        if dim in dimensions:
            logger.info("[Guard] %s dipindah ke group_by (peringkat per kelompok).", dim)
        if dim not in group_by and len(group_by) + len(ranked) < 5:
            group_by.append(dim)
        for key in _filter_keys_for_dimension(filters, dim):
            if dim != "jenis_pelayanan":
                filters.pop(key, None)

    arguments["dimensions"] = ranked
    arguments["group_by"] = group_by
    arguments["filters"] = filters
    return arguments


def reroute_tool_call(name, arguments, question):
    """
    Mengalihkan pilihan tool yang keliru sebelum dijalankan:
    service_mix (komposisi RJTL/RITL keseluruhan) tetapi pertanyaan meminta
    rincian per dimensi lain -> metric_share dengan persentase di dalam
    kelompok.
    """
    if name != "service_mix":
        return name, arguments

    wanted = [
        dim for dim in breakdown_dimensions(question)
        if dim != "jenis_pelayanan"
    ]
    if not wanted:
        return name, arguments

    filters = dict((arguments or {}).get("filters") or {})
    for dim in wanted:
        for key in _filter_keys_for_dimension(filters, dim):
            filters.pop(key, None)

    logger.info("[Guard] service_mix dialihkan ke metric_share per %s.", ", ".join(wanted))
    return "metric_share", {
        "metric": "total_pelayanan",
        "dimensions": wanted + ["jenis_pelayanan"],
        "within": list(wanted),
        "filters": filters,
    }


def normalize_tool_arguments(name, arguments, question=None):
    """
    Normalisasi argumen tool dari Qwen sebelum dikirim ke MCP Server.
    Mengembalikan dictionary argumen baru.
    """

    arguments = dict(arguments or {})

    # Normalisasi filter untuk tool yang menerima filters.
    if name in FILTER_TOOLS:
        arguments["filters"] = normalize_filters(arguments.get("filters"))

    # Normalisasi khusus analyze_data / metric_share.
    if name in DIMENSION_TOOLS:
        # Terima juga parameter "metrics" berisi satu metrik.
        metric = arguments.get("metric")
        if metric is None and "metrics" in arguments:
            metrics = arguments.get("metrics")
            if isinstance(metrics, list) and len(metrics) == 1:
                metric = metrics[0]
            elif isinstance(metrics, str):
                metric = metrics

        arguments["metric"] = normalize_metric(metric)
        arguments["dimensions"] = normalize_dimensions(arguments)

        if "limit" in arguments:
            arguments["limit"] = normalize_limit(arguments.get("limit"))

        _normalize_sort_arguments(arguments)

        if name == "metric_share" and arguments.get("within") is not None:
            arguments["within"] = normalize_dimension_names(
                arguments.get("within"), "within"
            )

        # Hapus argumen alternatif yang tidak dikenal MCP.
        arguments = _keep_allowed_arguments(name, arguments)

    elif name == "compare_metrics":
        metrics = arguments.get("metrics")
        if metrics is None:
            metrics = arguments.get("metric")
        arguments["metrics"] = normalize_metric_list(metrics)
        arguments["dimensions"] = normalize_dimensions(arguments)
        if "limit" in arguments:
            arguments["limit"] = normalize_limit(arguments.get("limit"))
        _normalize_sort_arguments(arguments, arguments["metrics"])
        arguments = _keep_allowed_arguments(name, arguments)

    elif name == "top_categories":
        # group_by adalah parameter resmi tool ini, bukan alias dimensions.
        group_by = arguments.pop("group_by", None)
        if group_by is None:
            group_by = arguments.pop("per", None)
        top_n = arguments.get("top_n")
        for alias in ("n", "top", "limit", "jumlah"):
            if top_n is None and arguments.get(alias) is not None:
                top_n = arguments.get(alias)

        arguments["metric"] = normalize_metric(
            arguments.get("metric") or "total_pelayanan"
        )
        arguments["dimensions"] = normalize_dimensions(arguments)
        arguments["group_by"] = normalize_dimension_names(group_by, "group_by")
        arguments["top_n"] = normalize_top_n(top_n)
        arguments["order"] = normalize_order(arguments.get("order")) or "desc"
        arguments = _keep_allowed_arguments(name, arguments)

    elif name == "profile_summary":
        if arguments.get("dimensions") is not None:
            arguments["dimensions"] = normalize_dimension_names(
                arguments.get("dimensions")
            )
        arguments = _keep_allowed_arguments(name, arguments)

    # Normalisasi tahun untuk service_growth dan metric_growth.
    elif name in GROWTH_TOOLS:
        previous_year = arguments.get("previous_year")
        if previous_year is None:
            previous_year = arguments.get("tahun_awal")

        current_year = arguments.get("current_year")
        if current_year is None:
            current_year = arguments.get("tahun_akhir")

        normalized_years = {
            "previous_year": normalize_year(
                previous_year, "previous_year"
            ),
            "current_year": normalize_year(
                current_year, "current_year"
            ),
        }

        if name == "metric_growth":
            normalized_years["metric"] = normalize_metric(
                arguments.get("metric")
            )

        arguments = normalized_years

    elif name == "compare_costs":
        compare_arguments = {
            "filters": arguments.get("filters", {})
        }
        dimensions = normalize_dimensions(arguments)
        if dimensions:
            compare_arguments["dimensions"] = dimensions
            if arguments.get("limit") is not None:
                compare_arguments["limit"] = normalize_limit(arguments["limit"])
        arguments = compare_arguments

    elif name == "service_mix":
        arguments = {
            "filters": arguments.get("filters", {})
        }

    elif name == "inspect_data":
        arguments = {}

    if name in FILTER_TOOLS and question:
        arguments = apply_explicit_question_constraints(
            question,
            arguments,
            include_year_breakdown=(name == "analyze_data"),
            tool_name=name,
        )

    if question:
        arguments = _apply_breakdown_dimensions(name, arguments, question)

    if name in ("analyze_data", "metric_share", "compare_metrics", "compare_costs"):
        arguments = _raise_limit_for_crosstab(arguments)

    return arguments


def format_tableau_context(context, max_filters=20, max_values=15):
    """
    Mengubah konteks dashboard Tableau (dikirim extension) menjadi teks
    ringkas untuk Qwen. Menerima string atau dictionary:
    {"dashboard": "...", "worksheet": "...",
     "filters": [{"field": "...", "values": [...]}]}
    """

    if not context:
        return ""

    if isinstance(context, str):
        return context.strip()[:2000]

    if not isinstance(context, dict):
        return ""

    lines = []

    dashboard = context.get("dashboard")
    if isinstance(dashboard, str) and dashboard.strip():
        lines.append(f"Dashboard: {dashboard.strip()[:100]}")

    worksheet = context.get("worksheet")
    if isinstance(worksheet, str) and worksheet.strip():
        lines.append(f"Worksheet: {worksheet.strip()[:100]}")

    filters = context.get("filters")
    filter_lines = []

    if isinstance(filters, list):
        for item in filters[:max_filters]:
            if not isinstance(item, dict):
                continue

            field = item.get("field")
            if not isinstance(field, str) or not field.strip():
                continue

            values = item.get("values")
            if isinstance(values, (str, int, float)):
                values = [values]
            if not isinstance(values, list):
                continue

            clean_values = [
                str(value)[:80]
                for value in values[:max_values]
                if isinstance(value, (str, int, float))
                and not isinstance(value, bool)
            ]

            if not clean_values:
                continue

            suffix = " (dan lainnya)" if len(values) > max_values else ""
            filter_lines.append(
                f"- {field.strip()[:80]}: {', '.join(clean_values)}{suffix}"
            )

    if filter_lines:
        lines.append("Filter aktif:")
        lines.extend(filter_lines)

    return "\n".join(lines)


def clean_model_answer(text):
    """
    Membersihkan proses berpikir yang ikut terkirim sebagai jawaban:
    - blok <think>...</think> lengkap,
    - teks sebelum </think> (model thinking sering tidak mengirim tag
      pembuka <think>),
    - blok <think> yang tidak pernah ditutup (jawaban terpotong).
    """
    if not text:
        return ""

    text = _THINK_BLOCK.sub("", text)

    lowered = text.lower()
    closing = lowered.rfind("</think>")
    if closing != -1:
        text = text[closing + len("</think>"):]
        lowered = text.lower()

    opening = lowered.find("<think>")
    if opening != -1:
        text = text[:opening]

    # Qwen kadang menyalin blok konteks dashboard ke jawaban; blok itu
    # bukan bagian jawaban dan dapat menyesatkan (filter belum tentu dipakai).
    marker = text.find("[Konteks dashboard Tableau]")
    if marker != -1:
        text = text[:marker]

    return text.strip()


NO_TOOL_NUDGE = (
    "[Pesan sistem] Jawabanmu berisi angka tetapi kamu belum memanggil tool "
    "MCP. Semua angka WAJIB berasal dari tool. Panggil tool yang sesuai "
    "sekarang untuk pertanyaan terakhir pengguna; jangan menulis angka "
    "sebelum menerima hasil tool."
)

NUMBER_CORRECTION_PROMPT = (
    "[Pesan sistem] Jawabanmu memuat angka yang TIDAK ADA di hasil tool: "
    "{numbers}. Tulis ulang jawabanmu. Salin angka persis dari hasil tool di "
    "atas (jangan dihitung atau dibulatkan sendiri). Jika ragu, jangan "
    "sebutkan angkanya."
)

def number_mode():
    """
    ANSWER_NUMBER_MODE di .env:
    - warn    (default): narasi tetap tampil + peringatan bila ada angka
                         yang tidak ada di data.
    - correct          : Qwen diminta menulis ulang; jika masih salah,
                         narasi disembunyikan.
    """
    value = os.getenv("ANSWER_NUMBER_MODE", "warn").strip().lower()
    return "correct" if value == "correct" else "warn"


HIDDEN_NARRATIVE_MESSAGE = (
    "Penjelasan otomatis tidak ditampilkan karena tidak sepenuhnya sesuai "
    "dengan data. Silakan lihat Data resmi di bawah."
)

NO_DATA_ANSWER = (
    "Maaf, saya belum berhasil mengambil data dari sistem untuk pertanyaan "
    "ini, sehingga tidak menampilkan angka apa pun. Silakan ulangi "
    "pertanyaan dengan lebih spesifik, misalnya: \"Berapa jumlah pelayanan "
    "per DIVRE?\""
)

TRUNCATED_ANSWER_MESSAGE = (
    "Maaf, saya belum berhasil mengambil data untuk pertanyaan ini (jawaban "
    "model terpotong sebelum memanggil tool). Silakan klik \"Percakapan "
    "baru\" lalu ulangi pertanyaan, atau naikkan OLLAMA_NUM_PREDICT di .env."
)


def _message_role(message):
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _format_integer(number):
    return f"{int(number):,}".replace(",", ".")


# =========================================================
# CHATBOT
# =========================================================

class BPJSChatbot:

    def __init__(self, model=None, max_tool_rounds=4, ollama_client=None):

        self.model = model or os.getenv(
            "OLLAMA_MODEL", "qwen3:latest"
        )

        self.max_tool_rounds = max_tool_rounds

        # Pengaturan model dapat diubah melalui environment variable.
        self.temperature = _env_float("OLLAMA_TEMPERATURE", 0)
        # Default aman untuk laptop RAM 8 GB. Naikkan lewat .env bila
        # perangkat lebih kuat.
        self.num_predict = _env_int("OLLAMA_NUM_PREDICT", 512, minimum=1)
        self.num_ctx = _env_int("OLLAMA_NUM_CTX", 8192, minimum=2048)
        self.keep_alive = os.getenv("OLLAMA_KEEP_ALIVE", "5m")
        self.max_history_turns = _env_int(
            "MAX_HISTORY_TURNS", 6, minimum=1
        )

        # Model "thinking" (mis. qwen3 versi 2507) selalu berpikir dulu.
        # OLLAMA_THINK=1 memisahkan proses berpikir dari jawaban sehingga
        # tidak ikut tampil (butuh OLLAMA_NUM_PREDICT lebih besar).
        self.think = os.getenv("OLLAMA_THINK", "0").strip().lower() in (
            "1", "true", "yes", "on",
        )

        # Batas waktu satu panggilan Qwen (detik), agar tidak menunggu
        # tanpa akhir bila model terlalu berat untuk perangkat.
        self.ollama_timeout = _env_float("OLLAMA_TIMEOUT", 180)

        # Client Ollama membaca OLLAMA_HOST dari environment.
        self.ollama_client = ollama_client or ollama.Client(
            timeout=self.ollama_timeout
        )

        self.stack = None
        self.session = None

        self.available_tools = {}
        self.ollama_tools = []
        self.last_analysis = None

        # Jejak tool pada pertanyaan terakhir (untuk evaluasi & debugging).
        self.last_trace = []

        # Tool yang dikirim ke Qwen pada pertanyaan terakhir per percakapan.
        self.last_selected_tools = []
        self._previous_tools = {}

        # Hasil tool yang berhasil pada pertanyaan terakhir (nama, payload).
        self.last_payloads = []

        # Spesifikasi grafik untuk jawaban terakhir (dibuat dari last_payloads).
        self.last_charts = []

        # Setiap percakapan (mis. per pengguna Tableau) punya riwayat sendiri.
        self.conversations = {}
        self.reset_conversation()

    # -----------------------------------------------------
    # Riwayat percakapan
    # -----------------------------------------------------

    @property
    def messages(self):
        """Riwayat percakapan default (kompatibel dengan versi lama)."""
        return self._get_conversation(DEFAULT_CONVERSATION_ID)

    @messages.setter
    def messages(self, value):
        self.conversations[DEFAULT_CONVERSATION_ID] = value

    @staticmethod
    def _new_history():
        return [
            {
                "role": "system",
                "content": SYSTEM_PROMPT
            }
        ]

    def _get_conversation(self, conversation_id=None):
        conversation_id = conversation_id or DEFAULT_CONVERSATION_ID
        if conversation_id not in self.conversations:
            self.conversations[conversation_id] = self._new_history()
        return self.conversations[conversation_id]

    def reset_conversation(self, conversation_id=None):
        conversation_id = conversation_id or DEFAULT_CONVERSATION_ID
        self.conversations[conversation_id] = self._new_history()

    def _trim_history(self, history):
        """
        Menjaga riwayat tetap dalam batas MAX_HISTORY_TURNS putaran
        pertanyaan terakhir agar konteks Qwen tidak penuh. Pemotongan
        selalu di awal pesan user sehingga pesan tool tidak terpisah dari
        pemanggilnya.
        """

        user_indexes = [
            index for index, message in enumerate(history)
            if _message_role(message) == "user"
        ]

        if len(user_indexes) <= self.max_history_turns:
            return

        cut_index = user_indexes[-self.max_history_turns]
        system_messages = [
            message for message in history[:cut_index]
            if _message_role(message) == "system"
        ][:1]

        history[:] = system_messages + history[cut_index:]

    # -----------------------------------------------------
    # Koneksi MCP
    # -----------------------------------------------------

    async def start(self):

        self.stack = AsyncExitStack()
        await self.stack.__aenter__()

        try:
            server_params = StdioServerParameters(
                command=sys.executable,
                args=["-m", "mcp_server.server"],
                # Selalu jalankan dari folder proyek agar modul
                # mcp_server ditemukan walaupun dijalankan dari folder lain.
                cwd=str(PROJECT_ROOT),
                # Teruskan environment (DB_*, dll.) ke proses MCP Server.
                env=dict(os.environ),
            )

            read_stream, write_stream = await self.stack.enter_async_context(
                stdio_client(server_params)
            )

            self.session = await self.stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )

            await self.session.initialize()

            tool_result = await self.session.list_tools()

        except BaseException:
            await self.close()
            raise

        self.available_tools = {}
        self.ollama_tools = []

        for tool in tool_result.tools:

            self.available_tools[tool.name] = tool

            self.ollama_tools.append({
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description or "",
                    "parameters": tool.input_schema
                }
            })

        logger.info("MCP Server terhubung.")
        logger.info("Tools tersedia: %s", ", ".join(self.available_tools))

        return self

    async def close(self):

        if self.stack:
            stack = self.stack
            self.stack = None
            self.session = None
            await stack.aclose()

    async def __aenter__(self):
        return await self.start()

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    @property
    def is_ready(self):
        return self.session is not None

    # -----------------------------------------------------
    # Utilitas tool
    # -----------------------------------------------------

    @staticmethod
    def parse_arguments(arguments):

        if arguments is None:
            return {}

        if isinstance(arguments, dict):
            return arguments

        if isinstance(arguments, str):
            if not arguments.strip():
                return {}

            parsed = json.loads(arguments)

            if isinstance(parsed, dict):
                return parsed

        raise ValueError("Format argumen MCP tidak valid.")

    @staticmethod
    def extract_tool_result(result):

        contents = []

        for item in getattr(result, "content", None) or []:

            if hasattr(item, "text"):
                contents.append(item.text)
            else:
                contents.append(str(item))

        if not contents:
            return json.dumps(
                {"message": "MCP tidak mengembalikan konten."},
                ensure_ascii=False
            )

        return "\n".join(contents)

    @staticmethod
    def apply_explicit_question_constraints(question, arguments):
        """Pastikan kode/tahun eksplisit dari pertanyaan tidak diganti Qwen."""
        return apply_explicit_question_constraints(question, arguments)

    async def execute_tool(self, tool_call, question=None):

        name = tool_call.function.name

        if name not in self.available_tools:
            return (
                name,
                json.dumps(
                    {
                        "success": False,
                        "error": "Tool tidak terdaftar dan tidak diizinkan.",
                    },
                    ensure_ascii=False,
                ),
            )

        try:
            arguments = self.parse_arguments(
                tool_call.function.arguments
            )

            rerouted, rerouted_arguments = reroute_tool_call(
                name, arguments, question
            )
            if rerouted in self.available_tools:
                name, arguments = rerouted, rerouted_arguments

            arguments = normalize_tool_arguments(
                name,
                arguments,
                question=question,
            )

            logger.info("[MCP] Menjalankan tool: %s", name)
            logger.info("[MCP] Arguments: %s", arguments)

            result = await self.session.call_tool(
                name,
                arguments=arguments
            )

            output = self.extract_tool_result(result)

            logger.debug("[MCP Raw Result]: %s", output)
            logger.info("[MCP] Tool selesai.")

            self._record_trace(name, arguments, output)

            return name, output

        except Exception as error:
            logger.warning("[MCP] Tool gagal: %s", error)
            self._record_trace(name, None, None, error=str(error))

            return name, json.dumps(
                {
                    "success": False,
                    "error": "Tool gagal dijalankan.",
                    "detail": str(error)
                },
                ensure_ascii=False
            )

    def _record_trace(self, name, arguments, output, error=None):
        success = False
        message = error
        if output is not None:
            try:
                payload = json.loads(output)
                success = payload.get("success") is True
                message = payload.get("message") or payload.get("detail")
            except (ValueError, TypeError, AttributeError):
                success = False
        self.last_trace.append({
            "tool": name,
            "arguments": arguments,
            "success": success,
            "message": message,
        })

    # -----------------------------------------------------
    # Percakapan
    # -----------------------------------------------------

    def _tools_for(self, tool_names):
        if tool_names is None:
            return self.ollama_tools
        wanted = set(tool_names)
        return [
            tool for tool in self.ollama_tools
            if tool["function"]["name"] in wanted
        ]

    async def _chat(self, history, tool_names=None):
        """
        Mengirim percakapan ke Qwen. System prompt dan daftar tool dibatasi
        pada tool yang relevan (tool_names) agar lebih ringan.
        """
        messages = [
            {"role": "system", "content": build_system_prompt(tool_names)}
        ] + [
            message for message in history
            if _message_role(message) != "system"
        ]

        try:
            return await asyncio.to_thread(
                self.ollama_client.chat,
                model=self.model,
                messages=messages,
                tools=self._tools_for(tool_names),
                think=self.think,
                options={
                    "temperature": self.temperature,
                    "num_predict": self.num_predict,
                    "num_ctx": self.num_ctx,
                },
                keep_alive=self.keep_alive,
            )
        except httpx.TimeoutException as error:
            raise TimeoutError(
                f"Qwen tidak menjawab dalam {int(self.ollama_timeout)} detik. "
                "Model mungkin terlalu berat untuk perangkat ini; coba model "
                "lebih kecil (mis. OLLAMA_MODEL=qwen3:4b) atau turunkan "
                "OLLAMA_NUM_CTX di .env."
            ) from error

    async def ask(self, question, conversation_id=None, context=None):
        """
        Menjawab pertanyaan pengguna.

        conversation_id: memisahkan riwayat antar pengguna/sesi.
        context: konteks tambahan (mis. filter dashboard Tableau) berupa
                 string atau dictionary; hanya dikirim ke Qwen dan tidak
                 dipakai untuk mengunci filter eksplisit.
        """

        if not self.session:
            raise RuntimeError("Chatbot belum terhubung ke MCP.")

        if not isinstance(question, str) or not question.strip():
            raise ValueError("Pertanyaan tidak boleh kosong.")

        question = question.strip()
        history = self._get_conversation(conversation_id)
        checkpoint = len(history)

        content = question
        context_text = format_tableau_context(context)
        if context_text:
            content += (
                "\n\n[Konteks dashboard Tableau]\n"
                + context_text
            )

        history.append({
            "role": "user",
            "content": content
        })

        conversation_key = conversation_id or DEFAULT_CONVERSATION_ID
        self.last_selected_tools = select_tools(
            question,
            available=list(self.available_tools) or None,
            previous_tools=self._previous_tools.get(conversation_key),
        )

        self.last_charts = []

        try:
            answer, _ = await self._answer(
                history, question, self.last_selected_tools
            )

        except BaseException:
            # Kembalikan riwayat ke kondisi sebelum pertanyaan agar
            # percakapan berikutnya tidak rusak oleh pesan setengah jadi.
            del history[checkpoint:]
            raise

        # Riwayat dipadatkan: hanya pertanyaan dan jawaban akhir yang
        # disimpan (hasil tool mentah tidak dibawa ke pertanyaan berikutnya)
        # agar konteks Qwen tetap kecil dan cepat.
        del history[checkpoint + 1:]
        history.append({
            "role": "assistant",
            "content": answer
        })

        # Grafik dibuat Python dari data resmi yang sama dengan tabel jawaban.
        self.last_charts = build_charts(self.last_payloads)

        used_tools = [entry["tool"] for entry in self.last_trace if entry["success"]]
        if used_tools:
            self._previous_tools[conversation_key] = list(dict.fromkeys(used_tools))

        self._trim_history(history)
        return answer

    def _finalize_model_answer(self, answer, question):
        """
        Menambahkan tabel angka resmi (dibuat Python) di bawah narasi Qwen.
        Pemeriksaan angka dilakukan sebelumnya di _answer.
        """
        if not self.last_payloads:
            return answer

        parts = [answer]

        if number_check_enabled() and number_mode() == "warn":
            warning = unverified_warning(
                find_unverified_numbers(answer, self.last_payloads, question)
            )
            if warning:
                parts.append(warning)

        if data_table_enabled():
            table = format_data_tables(self.last_payloads)
            if table:
                parts.append(table)

        return "\n\n".join(parts)

    async def _answer(self, history, question, tool_names=None):
        """
        Menjalankan loop tool-calling.
        Mengembalikan (jawaban, sudah_tercatat_di_riwayat).
        """

        self.last_analysis = None
        self.last_trace = []
        self.last_payloads = []
        verified_total_sent = False
        nudged = False
        corrected = False

        for round_number in range(self.max_tool_rounds + 1):

            response = await self._chat(history, tool_names)

            history.append(response.message)

            tool_calls = response.message.tool_calls or []

            # Tidak ada tool call: jawaban akhir Qwen
            if not tool_calls:

                answer = clean_model_answer(response.message.content)
                truncated = getattr(response, "done_reason", None) == "length"

                # Pengaman anti-angka-karangan: jawaban berisi angka data
                # padahal tidak ada hasil tool sama sekali.
                if answer and not truncated and not self.last_payloads:
                    context_texts = [
                        message.get("content") or ""
                        for message in history[:-1]
                        if isinstance(message, dict)
                        and message.get("role") in ("user", "assistant")
                    ]
                    if numbers_without_source(answer, question, context_texts):
                        logger.warning(
                            "[Guard] Qwen menjawab dengan angka tanpa memanggil tool."
                        )
                        history.pop()
                        if not nudged:
                            nudged = True
                            history.append({
                                "role": "user",
                                "content": NO_TOOL_NUDGE,
                            })
                            continue
                        history.append({
                            "role": "assistant",
                            "content": NO_DATA_ANSWER,
                        })
                        return NO_DATA_ANSWER, True

                # Narasi harus sama dengan data resmi: angka yang tidak ada
                # di hasil tool -> minta Qwen menulis ulang sekali; jika
                # masih salah, narasi disembunyikan (hanya data resmi).
                wrong_numbers = []
                if (
                    answer
                    and self.last_payloads
                    and number_check_enabled()
                    and number_mode() == "correct"
                ):
                    wrong_numbers = find_unverified_numbers(
                        answer, self.last_payloads, question
                    )

                if wrong_numbers:
                    logger.warning(
                        "[Guard] Angka narasi tidak sesuai data: %s",
                        ", ".join(wrong_numbers),
                    )
                    if not corrected and round_number < self.max_tool_rounds:
                        corrected = True
                        history[-1] = {"role": "assistant", "content": answer}
                        history.append({
                            "role": "user",
                            "content": NUMBER_CORRECTION_PROMPT.format(
                                numbers=", ".join(wrong_numbers[:6])
                            ),
                        })
                        continue
                    answer = HIDDEN_NARRATIVE_MESSAGE
                    truncated = False

                if truncated and not self.last_payloads:
                    # Qwen menulis panjang tanpa memanggil tool. Minta sekali
                    # untuk memanggil tool sebelum menyerah.
                    if not nudged and round_number < self.max_tool_rounds:
                        nudged = True
                        logger.warning(
                            "[Guard] Jawaban terpotong tanpa memanggil tool; "
                            "meminta Qwen memanggil tool."
                        )
                        history.pop()
                        history.append({"role": "user", "content": NO_TOOL_NUDGE})
                        continue
                    # Jangan tampilkan teks setengah jadi.
                    answer = TRUNCATED_ANSWER_MESSAGE
                elif truncated:
                    answer = (
                        (answer + " ...\n\n" if answer else "")
                        + "(Penjelasan model terpotong; lihat data resmi di bawah.)"
                    )
                elif not answer:
                    answer = (
                        "Maaf, saya belum berhasil menghasilkan jawaban. "
                        "Silakan coba pertanyaan kembali."
                    )

                if answer != TRUNCATED_ANSWER_MESSAGE:
                    answer = self._finalize_model_answer(answer, question)

                # Simpan versi jawaban yang sudah dibersihkan di riwayat.
                history[-1] = {
                    "role": "assistant",
                    "content": answer
                }
                return answer, True

            # Batas jumlah putaran tool calling
            if round_number >= self.max_tool_rounds:
                return (
                    "Analisis belum selesai karena batas pemanggilan "
                    "tools tercapai."
                ), False

            # Jalankan setiap tool yang dipilih Qwen
            for tool_call in tool_calls:

                name, output = await self.execute_tool(
                    tool_call,
                    question=question
                )

                history.append({
                    "role": "tool",
                    "tool_name": name,
                    "content": compact_tool_output(output)
                })

                try:
                    tool_payload = json.loads(output)
                    if (
                        isinstance(tool_payload, dict)
                        and tool_payload.get("success") is True
                    ):
                        self.last_payloads.append((name, tool_payload))
                except (ValueError, TypeError):
                    pass

                if name == "analyze_data":
                    try:
                        payload = json.loads(output)

                        if payload.get("success") is True:
                            self.last_analysis = payload

                    except (ValueError, TypeError, AttributeError):
                        self.last_analysis = None

            # Verifikasi total melalui query PostgreSQL terpisah.
            if (
                self.last_analysis
                and self.last_analysis.get("metric") == "total_pelayanan"
            ):
                grand_total = await self.get_verified_grand_total(
                    self.last_analysis
                )

                # Pertanyaan analitis: berikan total terverifikasi kepada
                # Qwen dan biarkan Qwen melanjutkan analisis/narasi.
                if not should_format_directly(question):
                    if not verified_total_sent:
                        history.append({
                            "role": "tool",
                            "tool_name": "analyze_data",
                            "content": json.dumps({
                                "verified_grand_total": (
                                    format_number_for_model(grand_total, "count")
                                    if grand_total is not None else None
                                ),
                                "note": (
                                    "Total keseluruhan dari query terpisah. "
                                    "Gunakan angka ini, jangan menjumlahkan "
                                    "hasil kelompok sendiri."
                                ),
                            }, ensure_ascii=False),
                        })
                        verified_total_sent = True
                        self.last_payloads.append((
                            "verified_total",
                            {"verified_grand_total": grand_total},
                        ))
                    self.last_analysis = None
                    continue

                question_lower = question.casefold()
                asks_percentage = any(
                    keyword in question_lower
                    for keyword in PERCENTAGE_KEYWORDS
                )

                if asks_percentage:
                    return self.format_percentage_analysis(
                        self.last_analysis,
                        grand_total
                    ), False

                return self.format_verified_analysis(
                    self.last_analysis,
                    grand_total
                ), False

        return "Maaf, proses analisis belum dapat diselesaikan.", False

    async def get_verified_grand_total(self, payload):
        """
        Mengambil total keseluruhan langsung dari PostgreSQL
        melalui MCP tanpa dimensi kelompok.
        """

        metric = payload.get("metric")
        filters = payload.get("filters") or {}

        try:
            result = await self.session.call_tool(
                "analyze_data",
                arguments={
                    "metric": metric,
                    "dimensions": [],
                    "filters": filters,
                    "limit": 1,
                }
            )

            output = self.extract_tool_result(result)
            data = json.loads(output)

            if data.get("success") is not True:
                return None

            rows = data.get("results", [])

            if len(rows) != 1:
                return None

            value = rows[0].get(metric)

            if isinstance(value, bool) or value is None:
                return None

            total = Decimal(str(value))

            if not total.is_finite():
                return None

            return total

        except Exception as error:
            logger.warning("[MCP] Verifikasi total gagal: %s", error)
            return None

    # -----------------------------------------------------
    # Format jawaban terverifikasi
    # -----------------------------------------------------

    @staticmethod
    def _row_label(row, metric):
        # Kolom kode (mis. divre_code) tidak ditampilkan bila labelnya ada,
        # agar tidak muncul "01, DIVRE 01".
        dimensions = [
            str(value)
            for key, value in row.items()
            if key != metric
            and not (key.endswith("_code") and key[:-5] in row)
        ]
        return ", ".join(dimensions) if dimensions else "Keseluruhan"

    @staticmethod
    def _describe_filters(payload):
        filters = payload.get("filters") or {}
        if not filters:
            return None

        parts = []
        for key, value in filters.items():
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value)
            parts.append(f"{key} = {value}")
        return "Filter: " + "; ".join(parts)

    @staticmethod
    def format_verified_analysis(payload, grand_total=None):
        """
        Menampilkan hasil kelompok tanpa menjumlahkan
        hasil kelompok secara manual.
        """

        results = payload.get("results", [])
        metric = payload.get("metric")

        if not results:
            return "Analisis berhasil, tetapi tidak ada data hasil."

        lines = []

        for row in results:
            value = row.get(metric)

            if isinstance(value, bool) or value is None:
                return "Hasil diterima, tetapi format angka tidak valid."

            try:
                number = Decimal(str(value))
            except Exception:
                return "Hasil diterima, tetapi format angka tidak valid."

            if not number.is_finite():
                return "Hasil diterima, tetapi format angka tidak valid."

            if number != number.to_integral_value():
                return "Hasil jumlah pelayanan bukan angka bulat yang valid."

            label = BPJSChatbot._row_label(row, metric)
            lines.append(f"- {label}: {_format_integer(number)}")

        answer = ["Hasil analisis jumlah pelayanan:"]

        filter_description = BPJSChatbot._describe_filters(payload)
        if filter_description:
            answer.append(filter_description)

        answer.extend(["", *lines, ""])

        if grand_total is not None:
            answer.append(f"Total keseluruhan: {_format_integer(grand_total)}")
            answer.append(
                "Total diambil dari query agregasi PostgreSQL terpisah."
            )
        else:
            answer.append(
                "Total keseluruhan belum dapat diverifikasi."
            )

        if payload.get("result_count", len(results)) >= payload.get(
            "limit", float("inf")
        ):
            answer.append("Catatan: hasil mungkin terpotong oleh batas jumlah baris.")

        return "\n".join(answer)

    @staticmethod
    def format_percentage_analysis(payload, grand_total=None):
        """
        Menampilkan jumlah dan persentase setiap kategori.
        Persentase dihitung menggunakan Calculation Engine.
        """

        results = payload.get("results", [])
        metric = payload.get("metric")

        if not results:
            return "Analisis berhasil, tetapi tidak ada data hasil."

        if grand_total is None or grand_total <= 0:
            return "Persentase tidak dapat ditampilkan karena total keseluruhan belum terverifikasi."

        lines = []

        for row in results:
            value = row.get(metric)

            if isinstance(value, bool) or value is None:
                return "Hasil diterima, tetapi format angka tidak valid."

            try:
                number = Decimal(str(value))
                if not number.is_finite() or number < 0:
                    return "Hasil diterima, tetapi format angka tidak valid."

                pct = percentage(number, grand_total)
                pct = pct.quantize(
                    Decimal("0.01"),
                    rounding=ROUND_HALF_UP
                )

            except Exception:
                return "Perhitungan persentase gagal."

            label = BPJSChatbot._row_label(row, metric)
            formatted_pct = str(pct).replace(".", ",")

            lines.append(
                f"- {label}: {_format_integer(number)} ({formatted_pct}%)"
            )

        answer = ["Hasil persentase pelayanan:"]

        filter_description = BPJSChatbot._describe_filters(payload)
        if filter_description:
            answer.append(filter_description)

        answer.extend([
            "",
            *lines,
            "",
            f"Total keseluruhan: {_format_integer(grand_total)}",
            "Persentase dihitung menggunakan Calculation Engine.",
        ])

        return "\n".join(answer)
