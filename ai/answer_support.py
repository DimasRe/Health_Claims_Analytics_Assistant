"""
Pendukung jawaban chatbot (patch B):

1. select_tools        : memilih tool yang relevan per pertanyaan agar
                         prompt lebih pendek dan model kecil tidak bingung.
2. compact_tool_output : memadatkan hasil tool sebelum dibaca Qwen.
3. format_data_tables  : tabel angka resmi dari hasil tool (dibuat Python),
                         ditempel di bawah jawaban Qwen.
4. find_unverified_numbers : memeriksa apakah angka pada narasi Qwen
                         benar-benar ada di hasil tool.
"""

import json
import os
import re
from decimal import Decimal, InvalidOperation

from mcp_server.calculation_engine import format_indonesian_number
from mcp_server.semantic_engine import load_config


def _env_flag(name, default=True):
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


# =========================================================
# 1. PEMILIHAN TOOL
# =========================================================

ALL_TOOLS = [
    "analyze_data",
    "metric_share",
    "compare_metrics",
    "top_categories",
    "compare_costs",
    "service_mix",
    "service_growth",
    "metric_growth",
    "profile_summary",
    "inspect_data",
]

_RULES = [
    # (pola, tool yang ditambahkan)
    (r"persen|proporsi|komposisi|distribusi|kontribusi|porsi|sebaran|"
     r"pangsa|share", ["metric_share"]),
    (r"(?:persen|proporsi|komposisi|porsi|perbandingan).*"
     r"(?:rjtl|ritl|rawat (?:inap|jalan))|"
     r"(?:rjtl|ritl|rawat (?:inap|jalan)).*(?:persen|proporsi|komposisi|porsi)",
     ["service_mix"]),
    (r"tagihan.*(?:verifikasi|selisih)|(?:verifikasi|selisih).*tagihan|"
     r"selisih biaya", ["compare_costs"]),
    (r"bandingkan|perbandingan|dibandingkan|versus|\bvs\b|\bjuga\b|"
     r"sekaligus|perbedaan|berbeda|\bbeda\b|tetapi|namun|sedangkan|"
     r"karakteristik", ["compare_metrics"]),
    (r"terbanyak|tertinggi|terbesar|teratas|terendah|tersedikit|terkecil|"
     r"paling|\btop\b|peringkat|ranking|\blima\b|\bsepuluh\b|"
     r"\b\d+\s+(?:kategori|kelompok|diagnosis|diagnosa|teratas|terbesar)",
     ["top_categories"]),
    (r"diagnos", ["top_categories", "metric_share"]),
    (r"berdasarkan|menurut|\bper\s+\w|setiap|masing[- ]masing|antar",
     ["metric_share"]),
    (r"tinggi.{0,40}(?:tetapi|namun|tapi).{0,40}rendah|"
     r"rendah.{0,40}(?:tetapi|namun|tapi).{0,40}tinggi|anomali|\bpola\b",
     ["compare_metrics", "profile_summary"]),
    (r"pertumbuhan|tumbuh|kenaikan|penurunan|\bnaik\b|\bturun\b|growth|"
     r"perkembangan|\btren\b", ["service_growth", "metric_growth"]),
    (r"ringkas|temuan|insight|karakteristik|profil|gambaran|menarik|"
     r"kesimpulan|rangkum", ["profile_summary", "compare_metrics"]),
    (r"metrik apa|dimensi apa|data apa|tersedia|katalog|"
     r"bisa (?:dianalisis|ditanyakan)|kemampuan", ["inspect_data"]),
]

_COMPILED_RULES = [
    (re.compile(pattern, re.IGNORECASE), tools) for pattern, tools in _RULES
]

# Pertanyaan lanjutan pendek ("bagaimana dengan tahun 2023?")
_FOLLOW_UP = re.compile(
    r"^\s*(?:bagaimana|gimana|kalau|jika|lalu|terus|dan|bandingkan dengan)"
    r"\b.{0,60}$",
    re.IGNORECASE,
)


def tool_selection_enabled():
    """TOOL_SELECTION=0 di .env untuk selalu mengirim semua tool."""
    return _env_flag("TOOL_SELECTION", True)


def select_tools(question, available=None, previous_tools=None):
    """
    Memilih tool yang relevan untuk pertanyaan. analyze_data selalu ikut.
    Mengembalikan list nama tool (urut prioritas), maksimal
    TOOL_SELECTION_MAX (default 5).
    """

    available = list(available) if available is not None else list(ALL_TOOLS)

    if not tool_selection_enabled():
        return available

    text = question if isinstance(question, str) else ""
    selected = ["analyze_data"]

    for pattern, tools in _COMPILED_RULES:
        if pattern.search(text):
            for tool in tools:
                if tool not in selected:
                    selected.append(tool)

    if previous_tools and _FOLLOW_UP.search(text):
        for tool in previous_tools:
            if tool not in selected:
                selected.append(tool)

    # Pertanyaan umum tanpa kata kunci khusus: beri pilihan dasar.
    if len(selected) == 1:
        selected.append("metric_share")

    maximum = max(2, _env_int("TOOL_SELECTION_MAX", 5))
    selected = [tool for tool in selected if tool in available][:maximum]

    return selected or available


# =========================================================
# 2. PEMADATAN HASIL TOOL UNTUK QWEN
# =========================================================

def compact_tool_output(output, max_rows=None):
    """
    JSON ringkas (tanpa indentasi) dan list panjang dipotong.
    Batas baris untuk Qwen: TOOL_OUTPUT_MAX_ROWS (default 100), sama dengan
    batas query sehingga Qwen melihat seluruh hasil.
    """
    if max_rows is None:
        max_rows = max(10, _env_int("TOOL_OUTPUT_MAX_ROWS", 100))

    try:
        payload = json.loads(output)
    except (TypeError, ValueError):
        return output

    def trim(value):
        if isinstance(value, list) and len(value) > max_rows:
            return value[:max_rows] + [
                {"catatan": f"{len(value) - max_rows} baris lain tidak ditampilkan"}
            ]
        if isinstance(value, dict):
            return {key: trim(item) for key, item in value.items()}
        return value

    if isinstance(payload, dict) and model_number_format_enabled():
        payload = format_payload_for_model(payload)

    return json.dumps(trim(payload), ensure_ascii=False, separators=(",", ":"))


# =========================================================
# 2b. ANGKA SIAP-SALIN UNTUK QWEN
# =========================================================
# Model kecil sering salah saat MENGUBAH format angka panjang (mis.
# "3187264059312.40" ditulis "318.726.405.931.240,00" -> 100x lebih besar).
# Karena itu angka yang dibaca Qwen sudah diformat Python sehingga Qwen
# cukup MENYALIN. Tabel data resmi, grafik, dan pemeriksa angka tetap
# memakai angka asli.

# Key yang bukan nilai metrik: tidak diubah.
_RAW_KEYS = {
    "peringkat", "result_count", "limit", "top_n", "current_year",
    "previous_year", "tahun_pelayanan", "bulan_pelayanan",
}

# Key bernilai dengan satuan metrik utama payload.
_PAYLOAD_METRIC_KEYS = {
    "grand_total", "total_kelompok", "previous_value", "current_value",
    "difference", "verified_grand_total", "nilai",
}

_COMPACT_SCALES = (
    (Decimal("1e12"), "triliun"),
    (Decimal("1e9"), "miliar"),
    (Decimal("1e6"), "juta"),
)


def model_number_format_enabled():
    """TOOL_OUTPUT_FORMAT_NUMBERS=0 di .env untuk mengirim angka mentah."""
    return _env_flag("TOOL_OUTPUT_FORMAT_NUMBERS", True)


def _compact_rupiah(number):
    for scale, word in _COMPACT_SCALES:
        if abs(number) >= scale:
            return f"Rp {format_indonesian_number(number / scale, 2)} {word}"
    return None


def format_number_for_model(value, unit_kind):
    """unit_kind: 'idr' | 'count' | 'percent' | 'number'."""
    number = _to_decimal(value)
    if number is None:
        return value

    if unit_kind == "percent":
        return format_indonesian_number(number, 2) + "%"

    if unit_kind == "idr":
        full = "Rp " + format_indonesian_number(number, 2)
        compact = _compact_rupiah(number)
        return f"{full} (≈ {compact})" if compact else full

    if unit_kind == "count" or number == number.to_integral_value():
        return format_indonesian_number(number, 0)

    return format_indonesian_number(number, 2)


def _unit_kind(metric):
    if not metric:
        return None
    _, unit = _metric_info(metric)
    if unit.startswith("IDR"):
        return "idr"
    if unit in ("records", "people"):
        return "count"
    if unit:
        return "number"
    return None


def _known_metrics():
    try:
        metrics_config, _ = load_config()
    except Exception:
        return set()
    return set((metrics_config.get("metrics") or {}).keys())


def format_payload_for_model(payload):
    """
    Salinan payload dengan angka sudah diformat (teks) sesuai satuannya:
    Rupiah -> "Rp 3.187.264.059.312,40 (≈ Rp 3,19 triliun)", jumlah ->
    "1.227.146", persentase -> "36,88%". Tahun, peringkat, dan kode tetap.
    """
    metrics = _known_metrics()
    default_metric = payload.get("metric")
    if payload.get("operation") == "service_growth":
        default_metric = "total_pelayanan"

    def classify(key, context_metric):
        if not isinstance(key, str) or key in _RAW_KEYS or key.startswith("peringkat"):
            return None
        if key in metrics:
            return _unit_kind(key)
        if key.startswith("persen") or "persentase" in key or key == "growth_percentage":
            return "percent"
        if key in ("previous_total", "current_total"):
            return "count"
        if key in _PAYLOAD_METRIC_KEYS:
            return _unit_kind(context_metric) or _unit_kind(default_metric)
        return None

    def walk(value, key=None, context_metric=None):
        if isinstance(value, dict):
            return {
                child_key: walk(
                    child,
                    child_key,
                    child_key if child_key in metrics else context_metric,
                )
                for child_key, child in value.items()
            }
        if isinstance(value, list):
            return [walk(item, key, context_metric) for item in value]
        if isinstance(value, bool) or value is None:
            return value
        kind = classify(key, context_metric)
        if kind is None:
            return value
        return format_number_for_model(value, kind)

    return walk(payload)


# =========================================================
# 3. TABEL ANGKA RESMI
# =========================================================

def _to_decimal(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _metric_info(metric):
    try:
        metrics_config, _ = load_config()
    except Exception:
        return metric, ""
    config = (metrics_config.get("metrics") or {}).get(metric) or {}
    return config.get("label", metric), str(config.get("unit") or "")


def format_value(metric, value):
    """Format angka sesuai satuan metrik (Rp untuk biaya)."""
    number = _to_decimal(value)
    if number is None:
        return "-" if value is None else str(value)

    _, unit = _metric_info(metric)

    if unit.startswith("IDR"):
        return "Rp " + format_indonesian_number(number, 2)
    if unit in ("records", "people") or number == number.to_integral_value():
        return format_indonesian_number(number, 0)
    return format_indonesian_number(number, 2)


def format_percent(value):
    number = _to_decimal(value)
    if number is None:
        return "-"
    return format_indonesian_number(number, 2) + "%"


def _row_label(row, dimensions):
    parts = [str(row.get(dim)) for dim in dimensions if dim in row]
    return " / ".join(parts) if parts else "Keseluruhan"


def _limit_lines(lines, max_lines):
    if len(lines) <= max_lines:
        return lines
    return lines[:max_lines] + [f"- ... dan {len(lines) - max_lines} baris lainnya"]


def _table_analyze(payload, max_lines):
    metric = payload.get("metric")
    label, _ = _metric_info(metric)
    dimensions = payload.get("dimensions") or []
    lines = [
        f"- {_row_label(row, dimensions)}: {format_value(metric, row.get(metric))}"
        for row in payload.get("results") or []
    ]
    return label, _limit_lines(lines, max_lines)


def _table_metric_share(payload, max_lines):
    metric = payload.get("metric")
    label, _ = _metric_info(metric)
    dimensions = payload.get("dimensions") or []
    lines = []
    for row in payload.get("results") or []:
        text = (
            f"- {_row_label(row, dimensions)}: "
            f"{format_value(metric, row.get(metric))} "
            f"({format_percent(row.get('persentase'))})"
        )
        if row.get("persentase_dalam_kelompok") is not None:
            text += f", {format_percent(row['persentase_dalam_kelompok'])} dalam kelompoknya"
        lines.append(text)
    lines = _limit_lines(lines, max_lines)
    lines.append(f"- Total keseluruhan: {format_value(metric, payload.get('grand_total'))}")
    return f"Persentase {label}", lines


def _table_compare_metrics(payload, max_lines):
    metrics = payload.get("metrics") or []
    dimensions = payload.get("dimensions") or []
    labels = payload.get("labels") or {}
    lines = []
    for row in payload.get("results") or []:
        values = "; ".join(
            f"{labels.get(metric, metric)} {format_value(metric, row.get(metric))}"
            for metric in metrics
        )
        lines.append(f"- {_row_label(row, dimensions)}: {values}")
    lines = _limit_lines(lines, max_lines)
    totals = payload.get("totals") or {}
    if totals:
        lines.append("- Total: " + "; ".join(
            f"{labels.get(metric, metric)} {format_value(metric, totals.get(metric))}"
            for metric in metrics
        ))
    return "Perbandingan metrik", lines


def _table_compare_costs(payload, max_lines):
    results = payload.get("results") or {}
    lines = [
        f"- Total biaya tagihan: {format_value('total_biaya_tagihan', results.get('total_biaya_tagihan'))}",
        f"- Total biaya verifikasi: {format_value('total_biaya_verifikasi', results.get('total_biaya_verifikasi'))}",
        f"- Selisih: {format_value('selisih_tagihan_verifikasi', results.get('selisih_tagihan_verifikasi'))} "
        f"({format_percent(results.get('persentase_selisih_dari_tagihan'))} dari tagihan)",
    ]
    dimensions = payload.get("dimensions") or []
    breakdown = [
        f"- {_row_label(row, dimensions)}: tagihan "
        f"{format_value('total_biaya_tagihan', row.get('total_biaya_tagihan'))}; verifikasi "
        f"{format_value('total_biaya_verifikasi', row.get('total_biaya_verifikasi'))}; selisih "
        f"{format_value('selisih_tagihan_verifikasi', row.get('selisih_tagihan_verifikasi'))} "
        f"({format_percent(row.get('persentase_selisih_dari_tagihan'))})"
        for row in payload.get("breakdown") or []
    ]
    return "Biaya tagihan vs verifikasi", lines + _limit_lines(breakdown, max_lines)


def _table_service_mix(payload, max_lines):
    lines = [
        f"- {row.get('jenis_pelayanan')}: "
        f"{format_value('total_pelayanan', row.get('total_pelayanan'))} "
        f"({format_percent(row.get('persentase'))})"
        for row in payload.get("results") or []
    ]
    lines.append(f"- Total: {format_value('total_pelayanan', payload.get('grand_total'))}")
    return "Komposisi RJTL/RITL", lines


def _table_growth(payload, max_lines):
    results = payload.get("results") or {}
    metric = payload.get("metric", "total_pelayanan")
    previous = results.get("previous_value", results.get("previous_total"))
    current = results.get("current_value", results.get("current_total"))
    label, _ = _metric_info(metric)
    lines = [
        f"- Tahun {payload.get('previous_year')}: {format_value(metric, previous)}",
        f"- Tahun {payload.get('current_year')}: {format_value(metric, current)}",
        f"- Selisih: {format_value(metric, results.get('difference'))}",
        f"- Pertumbuhan: {format_percent(results.get('growth_percentage'))}",
    ]
    return f"Pertumbuhan {label}", lines


def _table_top_categories(payload, max_lines):
    metric = payload.get("metric")
    label, _ = _metric_info(metric)
    group_by = payload.get("group_by") or []
    dimensions = payload.get("dimensions") or []
    lines = []
    for row in payload.get("results") or []:
        prefix = f"[{_row_label(row, group_by)}] " if group_by else ""
        text = (
            f"- {prefix}#{row.get('peringkat')} {_row_label(row, dimensions)}: "
            f"{format_value(metric, row.get(metric))}"
        )
        if row.get("persentase") is not None:
            text += f" ({format_percent(row['persentase'])})"
        lines.append(text)
    return f"Peringkat {label}", _limit_lines(lines, max_lines)


def _table_profile(payload, max_lines):
    lines = [
        f"- {_metric_info(metric)[0]}: {value}"
        for metric, value in (payload.get("totals_formatted") or {}).items()
    ]
    insights = payload.get("insights") or []
    lines.extend(f"- {text}" for text in insights[:max_lines])
    if len(insights) > max_lines:
        lines.append(f"- ... dan {len(insights) - max_lines} temuan lainnya")
    return "Ringkasan profil", lines


_FORMATTERS = {
    "analyze_data": _table_analyze,
    "metric_share": _table_metric_share,
    "compare_metrics": _table_compare_metrics,
    "compare_costs": _table_compare_costs,
    "service_mix": _table_service_mix,
    "service_growth": _table_growth,
    "metric_growth": _table_growth,
    "top_categories": _table_top_categories,
    "profile_summary": _table_profile,
}


def describe_filters(filters):
    """Filter yang BENAR-BENAR dipakai query, untuk transparansi."""
    if not isinstance(filters, dict) or not filters:
        return "Filter: tidak ada (seluruh data)"
    parts = []
    for key, value in filters.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        parts.append(f"{key} = {value}")
    return "Filter: " + "; ".join(parts)


def data_table_enabled():
    """ANSWER_DATA_TABLE=0 di .env untuk tidak menampilkan tabel."""
    return _env_flag("ANSWER_DATA_TABLE", True)


def format_data_tables(payloads, max_lines=None):
    """
    payloads: list (nama_tool, payload) dari tool yang berhasil.
    Mengembalikan teks tabel angka resmi atau "" bila tidak ada.
    Batas baris per tabel: ANSWER_TABLE_MAX_LINES (default 100).
    """
    if max_lines is None:
        max_lines = max(5, _env_int("ANSWER_TABLE_MAX_LINES", 100))

    sections = []
    seen = set()

    for name, payload in payloads:
        formatter = _FORMATTERS.get(name)
        if formatter is None or not isinstance(payload, dict):
            continue

        key = json.dumps(payload, sort_keys=True, default=str)
        if key in seen:
            continue
        seen.add(key)

        try:
            title, lines = formatter(payload, max_lines)
        except Exception:
            continue

        if lines and payload.get("possibly_truncated"):
            lines.append(
                f"- ⚠️ Catatan: hasil dibatasi {payload.get('result_count', len(lines))} "
                "baris; kemungkinan ada kategori yang tidak ditampilkan."
            )

        if lines:
            filter_line = describe_filters(payload.get("filters"))
            header = f"{title}:" + (f"\n{filter_line}" if filter_line else "")
            sections.append(header + "\n" + "\n".join(lines))

    if not sections:
        return ""

    return "Data resmi (dihitung sistem):\n" + "\n\n".join(sections)


# =========================================================
# 4. PEMERIKSAAN ANGKA PADA NARASI
# =========================================================

_SCALES = {
    "ribu": Decimal("1e3"), "rb": Decimal("1e3"),
    "juta": Decimal("1e6"), "jt": Decimal("1e6"),
    "miliar": Decimal("1e9"), "milyar": Decimal("1e9"),
    "triliun": Decimal("1e12"),
}

_NUMBER_TOKEN = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:[.,]\d{3})+(?:[.,]\d+)?|\d+(?:[.,]\d+)?)"
    r"(?:\s*(%|persen|ribu|rb|juta|jt|miliar|milyar|triliun)\b|(%))?",
    re.IGNORECASE,
)

_DIGITS_IN_TEXT = re.compile(r"\d+(?:[.,]\d+)?")


def _interpretations(token):
    """Kemungkinan nilai sebuah token angka beserta jumlah desimalnya."""
    candidates = set()

    def add(text):
        try:
            value = Decimal(text)
        except InvalidOperation:
            return
        decimals = len(text.split(".")[1]) if "." in text else 0
        candidates.add((value, decimals))

    # Gaya Indonesia: titik ribuan, koma desimal.
    add(token.replace(".", "").replace(",", "."))
    # Gaya Inggris: koma ribuan, titik desimal.
    add(token.replace(",", ""))

    # Format campuran yang sering ditulis model kecil, mis.
    # "355.199.999.921.53": pemisah terakhir diikuti BUKAN 3 digit berarti
    # desimal, sisanya pemisah ribuan (apa pun karakternya).
    separators = [index for index, char in enumerate(token) if char in ".,"]
    if len(separators) >= 2:
        last = separators[-1]
        if len(token) - last - 1 != 3:
            integer_part = "".join(ch for ch in token[:last] if ch.isdigit())
            add(integer_part + "." + token[last + 1:])
    return candidates


def _reference_numbers(payloads, extra_texts=()):
    references = set()

    def walk(value):
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float, Decimal)):
            number = _to_decimal(value)
            if number is not None:
                references.add(number)
                references.add(abs(number))
        elif isinstance(value, str):
            number = _to_decimal(value)
            if number is not None:
                references.add(number)
                references.add(abs(number))
            else:
                for match in _DIGITS_IN_TEXT.findall(value):
                    for candidate, _ in _interpretations(match):
                        references.add(candidate)

    for _, payload in payloads:
        walk(payload)

    for text in extra_texts:
        for match in _DIGITS_IN_TEXT.findall(text or ""):
            for candidate, _ in _interpretations(match):
                references.add(candidate)

    references.update({Decimal(0), Decimal(100)})
    return references


def number_check_enabled():
    """ANSWER_NUMBER_CHECK=0 di .env untuk mematikan pemeriksaan angka."""
    return _env_flag("ANSWER_NUMBER_CHECK", True)


def find_unverified_numbers(answer, payloads, question="", extra_texts=()):
    """
    Mengembalikan daftar angka pada narasi yang tidak ditemukan pada hasil
    tool (dengan toleransi pembulatan). Angka kecil (<= 12) diabaikan
    karena biasanya berupa urutan, bulan, atau jumlah kategori.
    """

    if not answer or not payloads:
        return []

    references = _reference_numbers(
        payloads, extra_texts=[question, *extra_texts]
    )
    unverified = []

    for match in _NUMBER_TOKEN.finditer(answer):
        token = match.group(1)
        suffix = (match.group(2) or match.group(3) or "").lower()
        scale = _SCALES.get(suffix, Decimal(1))

        interpretations = _interpretations(token)

        if scale == 1 and all(
            value <= 12 and value == value.to_integral_value()
            for value, _ in interpretations
        ):
            continue

        matched = False
        for value, decimals in interpretations:
            target = value * scale
            tolerance = Decimal("0.5") * Decimal(10) ** (-decimals) * scale
            if any(abs(target - reference) <= tolerance for reference in references):
                matched = True
                break

        if not matched:
            text = match.group(0).strip()
            if text not in unverified:
                unverified.append(text)

    return unverified


def unverified_warning(numbers):
    if not numbers:
        return ""
    shown = ", ".join(numbers[:6])
    return (
        "⚠️ Periksa: angka berikut pada penjelasan tidak ditemukan pada hasil "
        f"sistem: {shown}. Gunakan data resmi di bawah sebagai acuan."
    )


def numbers_without_source(answer, question="", context_texts=()):
    """
    Angka pada jawaban yang TIDAK berasal dari tool mana pun, pertanyaan,
    maupun percakapan sebelumnya. Dipakai ketika Qwen menjawab tanpa
    memanggil tool: angka seperti ini hampir pasti karangan.
    """
    return find_unverified_numbers(
        answer, [("tanpa_tool", {})], question, extra_texts=context_texts
    )
