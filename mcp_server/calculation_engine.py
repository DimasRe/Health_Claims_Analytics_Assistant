"""
Calculation Engine: seluruh perhitungan turunan (persentase, rasio,
pertumbuhan, selisih) dilakukan di Python dengan Decimal, bukan oleh LLM.
"""

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from mcp_server.semantic_engine import load_config


class CalculationError(Exception):
    """Kesalahan perhitungan analitik."""
    pass


def to_decimal(value):
    """Mengubah nilai numerik menjadi Decimal secara aman."""

    if isinstance(value, bool) or value is None:
        raise CalculationError("Nilai harus berupa angka.")

    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise CalculationError(f"Nilai numerik tidak valid: {value}")

    if not result.is_finite():
        raise CalculationError("Nilai harus berupa angka finite.")

    return result


def get_metric_rule(metric_key):
    """Mengambil aturan perhitungan dari metrics.yaml."""

    metrics_config, _ = load_config()
    metrics = metrics_config.get("metrics", {})

    if metric_key not in metrics:
        raise CalculationError(
            f"Metrik tidak ditemukan: {metric_key}"
        )

    metric = metrics[metric_key]

    if metric.get("status") != "validated":
        raise CalculationError(
            f"Metrik belum divalidasi: {metric_key}"
        )

    rule = metric.get("aggregation")

    if not isinstance(rule, dict):
        raise CalculationError(
            f"Aturan agregasi belum tersedia: {metric_key}"
        )

    return rule


def sum_disjoint_groups(metric_key, values, groups_are_disjoint=False):
    """
    Menjumlahkan hasil beberapa kelompok.

    Hanya diperbolehkan jika:
    - Metrik bersifat additive.
    - Kelompok tidak saling tumpang tindih.
    """

    rule = get_metric_rule(metric_key)

    if rule.get("additive") is not True:
        raise CalculationError(
            f"Metrik {metric_key} tidak boleh dijumlahkan "
            "antar-kelompok."
        )

    if not groups_are_disjoint:
        raise CalculationError(
            "Penjumlahan memerlukan konfirmasi bahwa "
            "kelompok tidak saling tumpang tindih."
        )

    if rule.get("rollup") not in (
        "sum",
        "sum_disjoint_groups"
    ):
        raise CalculationError(
            f"Metode rollup tidak mendukung penjumlahan: {metric_key}"
        )

    if not isinstance(values, (list, tuple)) or not values:
        raise CalculationError("Daftar nilai tidak boleh kosong.")

    return sum(
        (to_decimal(value) for value in values),
        Decimal("0")
    )


def weighted_average(
    metric_key,
    numerator_total,
    denominator_total
):
    """
    Menghitung weighted average dari total pembilang
    dibagi total penyebut.
    """

    rule = get_metric_rule(metric_key)

    if rule.get("type") != "average":
        raise CalculationError(
            f"Metrik bukan average: {metric_key}"
        )

    components = rule.get("components", {})

    if not components.get("numerator"):
        raise CalculationError("Komponen numerator belum tersedia.")

    if not components.get("denominator"):
        raise CalculationError("Komponen denominator belum tersedia.")

    numerator = to_decimal(numerator_total)
    denominator = to_decimal(denominator_total)

    if denominator == 0:
        raise CalculationError(
            "Penyebut tidak boleh nol."
        )

    return numerator / denominator


def percentage(part, whole):
    """Menghitung persentase."""

    part = to_decimal(part)
    whole = to_decimal(whole)

    if whole == 0:
        raise CalculationError(
            "Tidak dapat menghitung persentase dengan total nol."
        )

    return (part / whole) * Decimal("100")


def ratio(numerator, denominator):
    """Menghitung rasio."""

    numerator = to_decimal(numerator)
    denominator = to_decimal(denominator)

    if denominator == 0:
        raise CalculationError(
            "Penyebut rasio tidak boleh nol."
        )

    return numerator / denominator


def growth(current, previous):
    """Menghitung pertumbuhan antarperiode dalam persen."""

    current = to_decimal(current)
    previous = to_decimal(previous)

    if previous == 0:
        raise CalculationError(
            "Pertumbuhan tidak dapat dihitung karena "
            "nilai periode sebelumnya nol."
        )

    return (
        (current - previous) / abs(previous)
    ) * Decimal("100")


def calculate_difference(metric_key, component_values):
    """
    Menghitung metrik derived_difference berdasarkan
    urutan komponen pada metrics.yaml.
    """

    rule = get_metric_rule(metric_key)

    if rule.get("type") != "derived_difference":
        raise CalculationError(
            f"Metrik bukan derived_difference: {metric_key}"
        )

    components = rule.get("components", [])

    if len(components) != 2:
        raise CalculationError(
            "Metrik selisih harus memiliki dua komponen."
        )

    if not isinstance(component_values, dict):
        raise CalculationError(
            "Component values harus berupa dictionary."
        )

    first, second = components

    if first not in component_values or second not in component_values:
        raise CalculationError(
            "Nilai komponen belum lengkap."
        )

    return (
        to_decimal(component_values[first])
        - to_decimal(component_values[second])
    )


def validate_group_total(
    metric_key,
    group_values,
    grand_total,
    groups_are_disjoint=False
):
    """
    Memeriksa apakah jumlah kelompok sama dengan grand total.

    Tidak mengasumsikan bahwa distinct count dapat dijumlahkan.
    """

    calculated = sum_disjoint_groups(
        metric_key,
        group_values,
        groups_are_disjoint=groups_are_disjoint
    )

    expected = to_decimal(grand_total)

    return {
        "valid": calculated == expected,
        "calculated_total": calculated,
        "expected_total": expected,
        "difference": calculated - expected
    }


def format_indonesian_number(value, decimal_places=0):
    """Format angka menggunakan pemisah ribuan Indonesia."""

    number = to_decimal(value)

    if decimal_places < 0 or decimal_places > 8:
        raise CalculationError(
            "decimal_places harus antara 0 dan 8."
        )

    formatted = f"{number:,.{decimal_places}f}"

    return formatted.replace(",", "#").replace(".", ",").replace("#", ".")


def round_decimal(value, decimal_places=2):
    """Membulatkan angka secara half-up (pembulatan umum)."""

    number = to_decimal(value)

    if decimal_places < 0 or decimal_places > 8:
        raise CalculationError(
            "decimal_places harus antara 0 dan 8."
        )

    quantum = Decimal(1).scaleb(-decimal_places)
    return number.quantize(quantum, rounding=ROUND_HALF_UP)


if __name__ == "__main__":

    print("=== CALCULATION ENGINE TEST ===")

    # 1. Total pelayanan
    total = sum_disjoint_groups(
        "total_pelayanan",
        [318275, 61409],
        groups_are_disjoint=True
    )

    print("Total pelayanan:", format_indonesian_number(total))

    # 2. Persentase
    rjtl_percentage = percentage(318275, total)

    print(
        "Persentase RJTL:",
        format_indonesian_number(rjtl_percentage, 2),
        "%"
    )

    # 3. Weighted average
    average = weighted_average(
        "rata_rata_biaya_verifikasi",
        3_187_264_059_312.40,
        5_000_000
    )

    print(
        "Rata-rata biaya verifikasi:",
        format_indonesian_number(average, 2)
    )

    # 4. Pertumbuhan
    growth_value = growth(120, 100)

    print(
        "Pertumbuhan:",
        format_indonesian_number(growth_value, 2),
        "%"
    )

    # 5. Selisih biaya
    difference = calculate_difference(
        "selisih_tagihan_verifikasi",
        {
            "total_biaya_tagihan": 1000,
            "total_biaya_verifikasi": 800
        }
    )

    print(
        "Selisih biaya:",
        format_indonesian_number(difference)
    )

    # 6. Validasi total
    validation = validate_group_total(
        "total_pelayanan",
        [318275, 61409],
        379684,
        groups_are_disjoint=True
    )

    print("Validasi total:", validation)

    # 7. Uji perlindungan distinct count
    try:
        sum_disjoint_groups(
            "total_peserta_unik",
            [100, 200],
            groups_are_disjoint=True
        )
    except CalculationError as error:
        print("Proteksi distinct count:", error)

    print("\nCALCULATION ENGINE TEST SELESAI")
