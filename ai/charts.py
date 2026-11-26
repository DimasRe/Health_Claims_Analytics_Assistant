"""
Spesifikasi grafik untuk setiap jawaban.

Grafik dibuat OTOMATIS oleh Python dari hasil tool (data yang sama persis
dengan tabel "Data resmi"), bukan oleh Qwen. Dengan begitu grafik selalu
konsisten dengan angka resmi dan tidak menambah beban model.

Format spesifikasi (dibaca extension/index.html, digambar dengan Chart.js):
{
  "kind": "bar" | "grouped_bar" | "stacked_percent" | "line",
  "title": str, "subtitle": str,
  "unit": "count" | "idr" | "percent" | "number",
  "labels": [str, ...],
  "series": [{"name": str, "values": [float|None, ...],
              "names": [str|None, ...] (opsional, nama per titik)}],
  "horizontal": bool,
  "notes": [str, ...]
}
"""

import os
from decimal import Decimal, InvalidOperation

from mcp_server.semantic_engine import load_config

TIME_DIMENSIONS = ("tahun_pelayanan", "bulan_pelayanan")
MAX_SERIES = 6
MAX_CHARTS = 3


def charts_enabled():
    """ANSWER_CHARTS=0 di .env untuk mematikan grafik."""
    value = os.getenv("ANSWER_CHARTS", "1").strip().lower()
    return value not in ("0", "false", "no", "off")


def max_categories():
    try:
        return max(3, int(os.getenv("CHART_MAX_CATEGORIES", "20")))
    except ValueError:
        return 20


# =========================================================
# UTILITAS
# =========================================================

def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not number.is_finite():
        return None
    return float(number)


def _metric_meta(metric):
    try:
        metrics_config, _ = load_config()
    except Exception:
        return metric, "number"
    config = (metrics_config.get("metrics") or {}).get(metric) or {}
    unit = str(config.get("unit") or "")
    if unit.startswith("IDR"):
        kind = "idr"
    elif unit in ("records", "people"):
        kind = "count"
    else:
        kind = "number"
    return config.get("label", metric), kind


def _dimension_label(dimension):
    try:
        _, dimensions_config = load_config()
    except Exception:
        return dimension
    return ((dimensions_config.get("dimensions") or {}).get(dimension) or {}).get(
        "label", dimension
    )


def _filter_subtitle(filters):
    if not isinstance(filters, dict) or not filters:
        return "Seluruh data"
    parts = []
    for key, value in filters.items():
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value)
        parts.append(f"{key} = {value}")
    return "Filter: " + "; ".join(parts)


def _value_text(row, dims):
    return " / ".join(str(row.get(dim)) for dim in dims) if dims else "Keseluruhan"


def _time_sort_key(value):
    number = _number(value)
    return (0, number) if number is not None else (1, str(value))


def _cap_note(shown, total, what="kategori"):
    if total > shown:
        return f"Grafik menampilkan {shown} dari {total} {what}; lihat Data resmi untuk seluruhnya."
    return None


def _chart(kind, title, subtitle, unit, labels, series, horizontal=False, notes=None):
    return {
        "kind": kind,
        "title": title,
        "subtitle": subtitle,
        "unit": unit,
        "labels": [str(label) for label in labels],
        "series": series,
        "horizontal": horizontal,
        "notes": [note for note in (notes or []) if note],
    }


def _pivot(rows, label_dims, series_dims, value_key, name_key=None):
    """
    Mengubah baris menjadi labels x series.
    label_dims -> sumbu kategori; series_dims -> satu seri per kombinasi.
    """
    labels, series_names = [], []
    cells = {}
    for row in rows:
        label = _value_text(row, label_dims)
        series_name = _value_text(row, series_dims) if series_dims else None
        if label not in labels:
            labels.append(label)
        if series_name not in series_names:
            series_names.append(series_name)
        cells[(label, series_name)] = row
    return labels, series_names, cells


def _series_from_pivot(labels, series_names, cells, value_key, name_key=None,
                       default_name=None):
    series = []
    for series_name in series_names:
        values, names = [], []
        for label in labels:
            row = cells.get((label, series_name))
            values.append(_number(row.get(value_key)) if row else None)
            names.append(str(row.get(name_key)) if (row and name_key) else None)
        item = {"name": series_name or default_name or "", "values": values}
        if name_key:
            item["names"] = names
        series.append(item)
    return series


# =========================================================
# PER TOOL
# =========================================================

def _single_metric_chart(rows, dims, metric, title, subtitle, notes=None):
    """Grafik umum untuk satu metrik dengan 0..n dimensi."""
    label, unit = _metric_meta(metric)
    limit = max_categories()
    notes = list(notes or [])

    if not dims or not rows:
        return None

    time_dims = [dim for dim in dims if dim in TIME_DIMENSIONS]
    other_dims = [dim for dim in dims if dim not in TIME_DIMENSIONS]

    # Tren waktu -> garis; seri = kombinasi dimensi lain.
    if time_dims:
        ordered = sorted(
            rows, key=lambda row: tuple(_time_sort_key(row.get(dim)) for dim in time_dims)
        )
        labels, series_names, cells = _pivot(ordered, time_dims, other_dims, metric)
        if len(series_names) <= MAX_SERIES:
            # Satu titik waktu saja tidak membentuk tren -> pakai batang.
            kind = "line" if len(labels) >= 2 else "grouped_bar"
            return _chart(
                kind,
                title or f"Tren {label} per "
                + " & ".join(_dimension_label(dim) for dim in time_dims),
                subtitle, unit, labels,
                _series_from_pivot(labels, series_names, cells, metric, default_name=label),
                notes=notes,
            )
        # Terlalu banyak seri: tukar sumbu -> kategori = dimensi lain, seri = waktu.
        labels, series_names, cells = _pivot(ordered, other_dims, time_dims, metric)
        if len(series_names) <= MAX_SERIES:
            shown = labels[:limit]
            notes.append(_cap_note(len(shown), len(labels)))
            return _chart(
                "grouped_bar", title or label, subtitle, unit, shown,
                _series_from_pivot(shown, series_names, cells, metric),
                horizontal=True, notes=notes,
            )

    # Satu dimensi -> batang horizontal.
    if len(dims) == 1:
        shown = rows[:limit]
        notes.append(_cap_note(len(shown), len(rows)))
        return _chart(
            "bar", title or f"{label} per {_dimension_label(dims[0])}", subtitle, unit,
            [_value_text(row, dims) for row in shown],
            [{"name": label, "values": [_number(row.get(metric)) for row in shown]}],
            horizontal=True, notes=notes,
        )

    # Dua dimensi atau lebih -> batang berkelompok (seri = dimensi terakhir).
    label_dims, series_dims = dims[:-1], dims[-1:]
    labels, series_names, cells = _pivot(rows, label_dims, series_dims, metric)
    if len(series_names) > MAX_SERIES:
        label_dims, series_dims = dims[-1:], dims[:-1]
        labels, series_names, cells = _pivot(rows, label_dims, series_dims, metric)

    if len(series_names) <= MAX_SERIES:
        shown = labels[:limit]
        notes.append(_cap_note(len(shown), len(labels)))
        return _chart(
            "grouped_bar", title or label, subtitle, unit, shown,
            _series_from_pivot(shown, series_names, cells, metric),
            horizontal=True, notes=notes,
        )

    # Masih terlalu banyak kombinasi -> gabungkan semua dimensi jadi satu label.
    shown = rows[:limit]
    notes.append(_cap_note(len(shown), len(rows), "kombinasi"))
    return _chart(
        "bar", title or label, subtitle, unit,
        [_value_text(row, dims) for row in shown],
        [{"name": label, "values": [_number(row.get(metric)) for row in shown]}],
        horizontal=True, notes=notes,
    )


def _chart_analyze(payload):
    return [_single_metric_chart(
        payload.get("results") or [],
        payload.get("dimensions") or [],
        payload.get("metric"),
        None,
        _filter_subtitle(payload.get("filters")),
    )]


def _chart_metric_share(payload):
    metric = payload.get("metric")
    label, _ = _metric_meta(metric)
    rows = payload.get("results") or []
    dims = payload.get("dimensions") or []
    within = payload.get("within") or []
    subtitle = _filter_subtitle(payload.get("filters"))
    notes = []
    if payload.get("additive") is False:
        notes.append("Peserta unik: satu peserta dapat masuk lebih dari satu kategori.")

    # Persentase di dalam kelompok -> batang bertumpuk 100%. Untuk peserta
    # unik (tidak aditif) jumlahnya bisa > 100%, jadi memakai batang
    # berdampingan agar tidak terpotong/menyesatkan.
    if within and rows:
        others = [dim for dim in dims if dim not in within]
        labels, series_names, cells = _pivot(rows, within, others, "persentase_dalam_kelompok")
        if len(series_names) <= MAX_SERIES:
            shown = labels[:max_categories()]
            notes.append(_cap_note(len(shown), len(labels)))
            additive = payload.get("additive") is not False
            if not additive:
                notes.append("Persentase dalam kelompok dapat berjumlah lebih dari 100%.")
            return [_chart(
                "stacked_percent" if additive else "grouped_bar",
                f"Komposisi {label} di setiap {', '.join(_dimension_label(d) for d in within)}",
                subtitle, "percent", shown,
                _series_from_pivot(shown, series_names, cells, "persentase_dalam_kelompok"),
                horizontal=True, notes=notes,
            )]

    return [_single_metric_chart(rows, dims, metric, f"Distribusi {label}", subtitle, notes)]


def _chart_top_categories(payload):
    metric = payload.get("metric")
    label, unit = _metric_meta(metric)
    rows = payload.get("results") or []
    dims = payload.get("dimensions") or []
    group_by = payload.get("group_by") or []
    subtitle = _filter_subtitle(payload.get("filters"))
    if not rows:
        return []

    if group_by:
        # Kategori = kelompok, seri = peringkat (#1, #2, ...); nama per titik.
        for row in rows:
            row["_rank"] = f"#{row.get('peringkat')}"
            row["_name"] = _value_text(row, dims)
        labels, series_names, cells = _pivot(rows, group_by, ["_rank"], metric)
        series_names = sorted(series_names, key=lambda name: int(str(name).lstrip("#") or 0))
        if len(series_names) <= MAX_SERIES:
            shown = labels[:max_categories()]
            return [_chart(
                "grouped_bar",
                f"{label}: {len(series_names)} teratas di setiap "
                f"{', '.join(_dimension_label(d) for d in group_by)}",
                subtitle, unit, shown,
                _series_from_pivot(shown, series_names, cells, metric, name_key="_name"),
                horizontal=True,
                notes=[
                    "Arahkan kursor ke batang untuk melihat nama kategori.",
                    _cap_note(len(shown), len(labels), "kelompok"),
                ],
            )]

    shown = rows[:max_categories()]
    return [_chart(
        "bar", f"Peringkat {label}", subtitle, unit,
        [f"#{row.get('peringkat')} {_value_text(row, dims)}" for row in shown],
        [{"name": label, "values": [_number(row.get(metric)) for row in shown]}],
        horizontal=True,
    )]


def _chart_compare_metrics(payload):
    metrics = payload.get("metrics") or []
    rows = payload.get("results") or []
    dims = payload.get("dimensions") or []
    subtitle = _filter_subtitle(payload.get("filters"))
    if not rows or not dims:
        return []

    units = {metric: _metric_meta(metric)[1] for metric in metrics}
    labels_meta = payload.get("labels") or {}

    # Satuan sama & satu dimensi non-waktu -> satu grafik berkelompok.
    if len(set(units.values())) == 1 and len(metrics) > 1 and len(dims) == 1 \
            and dims[0] not in TIME_DIMENSIONS:
        shown = rows[:max_categories()]
        return [_chart(
            "grouped_bar", "Perbandingan metrik", subtitle, units[metrics[0]],
            [_value_text(row, dims) for row in shown],
            [{"name": labels_meta.get(metric, metric),
              "values": [_number(row.get(metric)) for row in shown]} for metric in metrics],
            horizontal=True,
            notes=[_cap_note(len(shown), len(rows))],
        )]

    # Satuan berbeda -> satu grafik per metrik (tidak pernah dua sumbu).
    charts = []
    for metric in metrics[:MAX_CHARTS]:
        charts.append(_single_metric_chart(
            rows, dims, metric, labels_meta.get(metric) or _metric_meta(metric)[0], subtitle
        ))
    return charts


def _chart_compare_costs(payload):
    subtitle = _filter_subtitle(payload.get("filters"))
    breakdown = payload.get("breakdown")
    dims = payload.get("dimensions") or []
    series_meta = [
        ("total_biaya_tagihan", "Biaya tagihan"),
        ("total_biaya_verifikasi", "Biaya verifikasi"),
    ]

    if breakdown and dims:
        if any(dim in TIME_DIMENSIONS for dim in dims) and len(dims) == 1:
            ordered = sorted(breakdown, key=lambda row: _time_sort_key(row.get(dims[0])))
            labels = [_value_text(row, dims) for row in ordered]
            return [_chart(
                "line", "Biaya tagihan vs verifikasi", subtitle, "idr", labels,
                [{"name": name, "values": [_number(row.get(key)) for row in ordered]}
                 for key, name in series_meta],
            )]
        shown = breakdown[:max_categories()]
        return [_chart(
            "grouped_bar", "Biaya tagihan vs verifikasi", subtitle, "idr",
            [_value_text(row, dims) for row in shown],
            [{"name": name, "values": [_number(row.get(key)) for row in shown]}
             for key, name in series_meta],
            horizontal=True,
            notes=[_cap_note(len(shown), len(breakdown))],
        )]

    results = payload.get("results") or {}
    return [_chart(
        "bar", "Biaya tagihan vs verifikasi", subtitle, "idr",
        [name for _, name in series_meta],
        [{"name": "Total biaya", "values": [_number(results.get(key)) for key, _ in series_meta]}],
        horizontal=True,
        notes=[
            f"Selisih: {results.get('persentase_selisih_dari_tagihan')}% dari tagihan."
            if results.get("persentase_selisih_dari_tagihan") is not None else None
        ],
    )]


def _chart_service_mix(payload):
    rows = payload.get("results") or []
    if not rows:
        return []
    return [_chart(
        "bar", "Komposisi RJTL/RITL", _filter_subtitle(payload.get("filters")), "count",
        [str(row.get("jenis_pelayanan")) for row in rows],
        [{"name": "Jumlah pelayanan", "values": [_number(row.get("total_pelayanan")) for row in rows]}],
        horizontal=True,
    )]


def _chart_growth(payload):
    results = payload.get("results") or {}
    metric = payload.get("metric", "total_pelayanan")
    label, unit = _metric_meta(metric)
    previous = results.get("previous_value", results.get("previous_total"))
    current = results.get("current_value", results.get("current_total"))
    growth = _number(results.get("growth_percentage"))
    note = None
    if growth is not None:
        note = f"Pertumbuhan: {growth:+.2f}%".replace(".", ",")
    return [_chart(
        "bar", f"{label}: {payload.get('previous_year')} vs {payload.get('current_year')}",
        "Seluruh data", unit,
        [str(payload.get("previous_year")), str(payload.get("current_year"))],
        [{"name": label, "values": [_number(previous), _number(current)]}],
        notes=[note],
    )]


def _chart_profile(payload):
    charts = []
    subtitle = _filter_subtitle(payload.get("filters"))
    for dimension, profile in list((payload.get("profiles") or {}).items())[:MAX_CHARTS]:
        rows = profile.get("categories") or []
        if not rows:
            continue
        shown = rows[:max_categories()]
        charts.append(_chart(
            "bar", f"Jumlah pelayanan per {profile.get('label', dimension)}", subtitle, "count",
            [str(row.get(dimension)) for row in shown],
            [{"name": "Jumlah pelayanan", "values": [_number(row.get("total_pelayanan")) for row in shown]}],
            horizontal=True,
        ))
    return charts


_BUILDERS = {
    "analyze_data": _chart_analyze,
    "metric_share": _chart_metric_share,
    "top_categories": _chart_top_categories,
    "compare_metrics": _chart_compare_metrics,
    "compare_costs": _chart_compare_costs,
    "service_mix": _chart_service_mix,
    "service_growth": _chart_growth,
    "metric_growth": _chart_growth,
    "profile_summary": _chart_profile,
}


def build_charts(payloads):
    """
    payloads: list (nama_tool, payload) hasil tool yang berhasil.
    Mengembalikan maksimal MAX_CHARTS spesifikasi grafik.
    """
    if not charts_enabled():
        return []

    charts, seen = [], set()
    for name, payload in payloads:
        builder = _BUILDERS.get(name)
        if builder is None or not isinstance(payload, dict):
            continue
        key = repr(sorted(payload.items(), key=lambda item: item[0]))
        if key in seen:
            continue
        seen.add(key)
        try:
            # Salinan dangkal baris agar kolom bantu tidak mengubah payload asli.
            payload = dict(payload)
            for field in ("results", "breakdown"):
                if isinstance(payload.get(field), list):
                    payload[field] = [dict(row) if isinstance(row, dict) else row
                                      for row in payload[field]]
            produced = builder(payload)
        except Exception:
            continue
        for chart in produced:
            if chart and chart.get("labels") and chart.get("series"):
                charts.append(chart)
            if len(charts) >= MAX_CHARTS:
                return charts
    return charts
