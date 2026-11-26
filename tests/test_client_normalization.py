import pytest

from ai.qwen_mcp_client import (
    apply_explicit_question_constraints,
    clean_model_answer,
    format_tableau_context,
    normalize_tool_arguments,
)


def test_metric_and_dimension_aliases():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "Jumlah Peserta", "dimensions": "segmen", "filters": {}},
    )
    assert args["metric"] == "total_peserta_unik"
    assert args["dimensions"] == ["segmen_peserta"]


def test_extra_dimension_parameters_are_merged_and_removed():
    args = normalize_tool_arguments(
        "analyze_data",
        {
            "metric": "total_pelayanan",
            "dimensions": ["divre"],
            "segment": "jenis pelayanan",
            "group_by": ["divre"],
            "unknown": 1,
        },
    )
    assert args["dimensions"] == ["divre", "jenis_pelayanan"]
    # Tabulasi silang 2 dimensi otomatis mendapat limit 100.
    assert set(args) == {"metric", "dimensions", "filters", "limit"}
    assert args["limit"] == 100


def test_dimension_dict_moves_year_to_filters():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_pelayanan", "dimensions": {"tahun": ["2022", "2023"], "segmen_peserta": True}},
    )
    assert args["filters"]["tahun_pelayanan"] == [2022, 2023]
    assert args["dimensions"] == ["segmen_peserta"]


def test_filter_aliases_and_year_conversion():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_pelayanan", "filters": {"tahun": "2024", "kode rs": "1001"}},
    )
    assert args["filters"] == {"tahun_pelayanan": 2024, "fasilitas_kesehatan_code": "1001"}


def test_conflicting_year_filter_is_rejected():
    with pytest.raises(ValueError, match="bertentangan"):
        normalize_tool_arguments(
            "analyze_data",
            {"metric": "total_pelayanan", "filters": {"tahun": 2023, "tahun_pelayanan": 2024}},
        )


def test_growth_arguments_accept_indonesian_names():
    args = normalize_tool_arguments(
        "metric_growth",
        {"metric": "total_biaya_tagihan", "tahun_awal": "2022", "tahun_akhir": 2023},
    )
    assert args == {"previous_year": 2022, "current_year": 2023, "metric": "total_biaya_tagihan"}


def test_compare_costs_keeps_only_filters():
    args = normalize_tool_arguments("compare_costs", {"filters": {"tahun": 2023}, "metric": "x"})
    assert args == {"filters": {"tahun_pelayanan": 2023}}


def test_limit_is_clamped():
    args = normalize_tool_arguments("analyze_data", {"metric": "total_pelayanan", "limit": "500"})
    assert args["limit"] == 100


def test_explicit_divre_code_overrides_model_value():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_biaya_tagihan", "filters": {"divre": "DIVRE 1"}},
        question="Berapa biaya tagihan DIVRE 10 tahun 2024?",
    )
    # Kode DIVRE dan tahun dari pertanyaan sama-sama dikunci.
    assert args["filters"] == {"divre_code": "10", "tahun_pelayanan": 2024}


def test_multiple_divre_codes():
    args = apply_explicit_question_constraints(
        "bandingkan divre 1 dan divre 2",
        {"metric": "total_biaya_tagihan", "filters": {}},
    )
    assert args["filters"]["divre_code"] == ["01", "02"]


def test_explicit_codes_for_branch_and_hospital():
    args = apply_explicit_question_constraints(
        "pelayanan rumah sakit dengan kode 10015851 di kode cabang 0121",
        {"metric": "total_pelayanan", "filters": {"nama_rs": "RS A"}},
    )
    assert args["filters"]["fasilitas_kesehatan_code"] == "10015851"
    assert args["filters"]["cabang_code"] == "0121"
    assert "nama_rs" not in args["filters"]


def test_words_are_not_taken_as_codes():
    # Bug lama: "tersebut"/"dengan" bisa terbaca sebagai kode.
    args = apply_explicit_question_constraints(
        "berapa pelayanan di kode cabang tersebut dan kode rumah sakit dengan biaya tertinggi",
        {"metric": "total_pelayanan", "filters": {}},
    )
    assert args["filters"] == {}


def test_multi_year_question_adds_year_breakdown():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_pelayanan", "dimensions": [], "filters": {"tahun_pelayanan": 2022}},
        question="Jumlah pelayanan tahun 2022 dan 2023",
    )
    assert args["filters"]["tahun_pelayanan"] == [2022, 2023]
    assert args["dimensions"] == ["tahun_pelayanan"]
    assert args["limit"] == 100


@pytest.mark.parametrize("question", [
    "jumlah pelayanan pertahunnya",
    "jumlah pelayanan setiap tahun",
    "rincian per tahun pelayanan",
    "pelayanan dari tahun ke tahun",
])
def test_year_breakdown_phrases(question):
    args = apply_explicit_question_constraints(
        question, {"metric": "total_pelayanan", "dimensions": ["jenis_pelayanan"]}
    )
    assert args["dimensions"] == ["jenis_pelayanan", "tahun_pelayanan"]


def test_compare_costs_multi_year_is_broken_down_per_year():
    args = normalize_tool_arguments(
        "compare_costs", {"filters": {}}, question="biaya 2022 dan 2023"
    )
    assert args == {
        "filters": {"tahun_pelayanan": [2022, 2023]},
        "dimensions": ["tahun_pelayanan"],
    }


def test_format_tableau_context():
    text = format_tableau_context({
        "dashboard": "Dashboard Pelayanan",
        "filters": [
            {"field": "Tahun", "values": [2024]},
            {"field": "Segmen", "values": ["PBI", "PPU"]},
            {"field": "", "values": ["x"]},
            {"field": "Kosong", "values": []},
            "bukan dict",
        ],
    })
    assert text == (
        "Dashboard: Dashboard Pelayanan\n"
        "Filter aktif:\n"
        "- Tahun: 2024\n"
        "- Segmen: PBI, PPU"
    )
    assert format_tableau_context(None) == ""
    assert format_tableau_context(123) == ""


def test_clean_model_answer_removes_think_block():
    assert clean_model_answer("<think>analisis</think>\nJawaban") == "Jawaban"
    assert clean_model_answer(None) == ""


# ---------------- v1.2: tool & argumen baru ----------------

from ai.qwen_mcp_client import should_format_directly  # noqa: E402


def test_order_and_sort_aliases():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "peserta", "dimensions": ["kepesertaan"], "order": "paling sedikit", "sort_by": "nilai"},
    )
    assert args["metric"] == "total_peserta_unik"
    assert args["dimensions"] == ["segmen_peserta"]
    assert args["order"] == "asc"
    assert args["sort_by"] == "metric"


def test_invalid_order_is_rejected():
    with pytest.raises(ValueError, match="order"):
        normalize_tool_arguments("analyze_data", {"metric": "total_pelayanan", "order": "acak"})


def test_metric_share_within_aliases():
    args = normalize_tool_arguments(
        "metric_share",
        {"metric": "peserta", "dimensions": ["gender", "umur"], "within": "gender"},
    )
    assert args["dimensions"] == ["jenis_kelamin", "kelompok_umur"]
    assert args["within"] == ["jenis_kelamin"]


def test_compare_metrics_arguments():
    args = normalize_tool_arguments(
        "compare_metrics",
        {"metrics": "jumlah pelayanan, biaya verifikasi", "dimensions": "wilayah", "sort_by": "biaya_verifikasi"},
    )
    assert args["metrics"] == ["total_pelayanan", "total_biaya_verifikasi"]
    assert args["dimensions"] == ["divre"]
    assert args["sort_by"] == "total_biaya_verifikasi"


def test_top_categories_arguments():
    args = normalize_tool_arguments(
        "top_categories",
        {"metric": "jumlah_pelayanan", "dimension": "diagnosa", "group_by": "jenis pelayanan", "n": "3"},
    )
    assert args == {
        "metric": "total_pelayanan",
        "dimensions": ["diagnosis_primer"],
        "group_by": ["jenis_pelayanan"],
        "top_n": 3,
        "order": "desc",
        "filters": {},
    }


def test_compare_costs_accepts_dimensions():
    args = normalize_tool_arguments("compare_costs", {"dimensions": ["wilayah"]})
    assert args == {"filters": {}, "dimensions": ["divre"]}


def test_profile_summary_arguments():
    args = normalize_tool_arguments("profile_summary", {"dimensions": ["gender", "umur"], "x": 1})
    assert args == {"dimensions": ["jenis_kelamin", "kelompok_umur"], "filters": {}}


@pytest.mark.parametrize("question, expected", [
    ("Berapa jumlah pelayanan RJTL dan RITL?", True),
    ("Jumlah pelayanan setiap tahun", True),
    ("Apakah wilayah dengan jumlah pelayanan tertinggi juga biayanya tertinggi?", False),
    ("Kelompok umur mana yang paling banyak pelayanannya?", False),
    ("Temukan kategori dengan pelayanan tinggi tetapi rata-rata biaya rendah", False),
])
def test_should_format_directly(question, expected):
    assert should_format_directly(question) is expected


# ---------------- v1.2.6: kunci "rawat inap/jalan" dan satu tahun ----------------

Q47 = "Bagaimana distribusi peserta berdasarkan segmen kepesertaan untuk pelayanan rawat inap tahun 2024?"


def test_q47_missing_ritl_filter_is_enforced():
    # Kasus nyata: Qwen hanya mengirim filter tahun, lupa RITL.
    args = normalize_tool_arguments(
        "metric_share",
        {"metric": "peserta", "dimensions": ["segmen"], "filters": {"tahun_pelayanan": 2024}},
        question=Q47,
    )
    assert args["filters"] == {"tahun_pelayanan": 2024, "jenis_pelayanan": "RITL"}


def test_q47_missing_year_and_type_are_enforced():
    args = normalize_tool_arguments(
        "metric_share", {"metric": "peserta", "dimensions": ["segmen"]}, question=Q47,
    )
    assert args["filters"] == {"jenis_pelayanan": "RITL", "tahun_pelayanan": 2024}


def test_wrong_year_from_model_is_corrected():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_pelayanan", "filters": {"tahun": 2023}},
        question="Berapa jumlah pelayanan rawat jalan tahun 2024?",
    )
    assert args["filters"] == {"jenis_pelayanan": "RJTL", "tahun_pelayanan": 2024}


@pytest.mark.parametrize("tool, args, question", [
    # Keduanya disebut -> perbandingan, tidak dikunci.
    ("analyze_data", {"metric": "total_pelayanan", "dimensions": ["jenis_pelayanan"]},
     "Berapa jumlah pelayanan RJTL dan RITL?"),
    # Jenis pelayanan dipakai sebagai pengelompokan.
    ("top_categories", {"metric": "total_pelayanan", "dimensions": ["diagnosis_primer"], "group_by": ["jenis_pelayanan"]},
     "Diagnosis terbanyak per jenis pelayanan rawat inap"),
    # service_mix memang membandingkan RJTL/RITL.
    ("service_mix", {}, "Berapa persentase pelayanan rawat inap?"),
])
def test_service_type_not_forced_when_compared_or_grouped(tool, args, question):
    result = normalize_tool_arguments(tool, args, question=question)
    assert "jenis_pelayanan" not in result.get("filters", {})


@pytest.mark.parametrize("question", [
    "Berapa jumlah pelayanan sejak 2020?",
    "Jumlah pelayanan sampai 2023",
])
def test_year_range_words_do_not_force_single_year(question):
    args = normalize_tool_arguments("analyze_data", {"metric": "total_pelayanan"}, question=question)
    assert "tahun_pelayanan" not in args["filters"]


def test_year_not_forced_when_grouped_by_year():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_biaya_tagihan", "dimensions": ["tahun_pelayanan"]},
        question="Biaya tagihan per tahun sampai 2024",
    )
    assert "tahun_pelayanan" not in args["filters"]


def test_copied_tableau_context_is_removed_from_answer():
    raw = "Distribusi peserta ...\n\n[Konteks dashboard Tableau]\nFilter aktif:\n- tahun: 2024"
    assert clean_model_answer(raw) == "Distribusi peserta ..."


# ---------------- v1.2.7: rincian per tahun & limit tabulasi silang ----------------

Q48 = "Bandingkan jumlah pelayanan RJTL dan RITL per DIVRE pada tahun 2023 dan 2024."


def test_q48_compare_metrics_gets_year_breakdown_and_full_limit():
    # Kasus nyata: Qwen memakai compare_metrics tanpa dimensi tahun dan limit 20.
    args = normalize_tool_arguments(
        "compare_metrics",
        {"metrics": ["total_pelayanan"], "dimensions": ["divre", "jenis_pelayanan"],
         "filters": {"tahun_pelayanan": [2023, 2024]}},
        question=Q48,
    )
    assert args["dimensions"] == ["divre", "jenis_pelayanan", "tahun_pelayanan"]
    assert args["filters"] == {"tahun_pelayanan": [2023, 2024]}
    assert args["limit"] == 100


def test_metric_share_multi_year_is_within_each_year():
    args = normalize_tool_arguments(
        "metric_share", {"metric": "peserta", "dimensions": ["segmen"]},
        question="Distribusi peserta per segmen tahun 2023 dan 2024",
    )
    assert args["dimensions"] == ["tahun_pelayanan", "segmen_peserta"]
    assert args["within"] == ["tahun_pelayanan"]
    assert args["filters"]["tahun_pelayanan"] == [2023, 2024]


def test_crosstab_limit_respects_explicit_small_limit():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_biaya_verifikasi", "dimensions": ["divre", "segmen_peserta"], "limit": 5},
        question="5 kombinasi DIVRE dan segmen dengan biaya terbesar",
    )
    assert args["limit"] == 5


def test_year_range_is_not_broken_down():
    args = normalize_tool_arguments(
        "compare_metrics", {"metrics": ["total_pelayanan"], "dimensions": ["divre"]},
        question="Jumlah pelayanan per DIVRE antara 2020 dan 2023",
    )
    assert args["dimensions"] == ["divre"]


# ---------------- v1.2.11: rincian "di setiap X" ----------------

from ai.qwen_mcp_client import breakdown_dimensions, reroute_tool_call  # noqa: E402

Q49 = "Berapa persentase pelayanan RITL dibandingkan RJTL di setiap kelompok umur pada tahun 2024?"


def test_breakdown_dimensions_detection():
    assert breakdown_dimensions(Q49) == ["kelompok_umur"]
    assert breakdown_dimensions("jumlah pelayanan per DIVRE dan per segmen peserta") == ["divre", "segmen_peserta"]
    assert breakdown_dimensions("distribusi tipe faskes berdasarkan kepemilikan") == ["kepemilikan_fasilitas"]
    assert breakdown_dimensions("berapa jumlah pelayanan RJTL?") == []


def test_q49_service_mix_with_guessed_filters_is_rerouted():
    # Kasus nyata: service_mix + kelompok umur tebakan sebagai filter -> Total 0.
    name, args = reroute_tool_call(
        "service_mix",
        {"filters": {"kelompok_umur": ["0-14", "15-24", "25-34", "35-44", "45-54", "55-64", "65+"],
                     "tahun_pelayanan": 2024}},
        Q49,
    )
    args = normalize_tool_arguments(name, args, question=Q49)
    assert name == "metric_share"
    assert args["dimensions"] == ["kelompok_umur", "jenis_pelayanan"]
    assert args["within"] == ["kelompok_umur"]
    assert args["filters"] == {"tahun_pelayanan": 2024}
    assert args["limit"] == 100


def test_service_mix_without_breakdown_is_not_rerouted():
    assert reroute_tool_call("service_mix", {"filters": {}}, "Berapa persentase RJTL dan RITL?")[0] == "service_mix"


def test_guessed_filter_moved_to_dimension():
    args = normalize_tool_arguments(
        "analyze_data",
        {"metric": "total_pelayanan", "filters": {"segmen_peserta": ["PBI", "PPU"]}},
        question="Berapa jumlah pelayanan per segmen peserta?",
    )
    assert args["dimensions"] == ["segmen_peserta"]
    assert "segmen_peserta" not in args["filters"]


def test_metric_share_within_from_question():
    args = normalize_tool_arguments(
        "metric_share",
        {"metric": "peserta", "dimensions": ["segmen", "jenis kelamin"]},
        question="Bagaimana distribusi jenis kelamin pada masing-masing segmen peserta?",
    )
    assert args["within"] == ["segmen_peserta"]


Q50 = "Sebutkan 3 diagnosis primer terbanyak pada setiap kelompok umur."


def test_q50_group_dimension_moves_to_group_by():
    # Kasus nyata: Qwen memasukkan kelompok_umur ke dimensions (peringkat gabungan).
    args = normalize_tool_arguments(
        "top_categories",
        {"metric": "total_pelayanan", "dimensions": ["diagnosis_primer", "kelompok_umur"], "top_n": 3},
        question=Q50,
    )
    assert args["dimensions"] == ["diagnosis_primer"]
    assert args["group_by"] == ["kelompok_umur"]
    assert args["top_n"] == 3


def test_top_categories_missing_group_by_is_added():
    args = normalize_tool_arguments(
        "top_categories",
        {"metric": "total_pelayanan", "dimensions": ["fasilitas_kesehatan"], "top_n": 3},
        question="Tiga fasilitas kesehatan dengan jumlah pelayanan terbanyak di setiap DIVRE.",
    )
    assert args["group_by"] == ["divre"]
    assert args["dimensions"] == ["fasilitas_kesehatan"]


def test_top_categories_by_service_type_group():
    args = normalize_tool_arguments(
        "top_categories",
        {"metric": "total_pelayanan", "dimensions": ["diagnosis_primer"]},
        question="Bagaimana distribusi diagnosis berdasarkan jenis pelayanan?",
    )
    assert args["group_by"] == ["jenis_pelayanan"]


def test_top_categories_without_breakdown_unchanged():
    args = normalize_tool_arguments(
        "top_categories",
        {"metric": "total_biaya_verifikasi", "dimensions": ["segmen_peserta", "kelompok_umur"], "top_n": 5},
        question="Temukan lima kelompok peserta dengan total biaya verifikasi terbesar.",
    )
    assert args["dimensions"] == ["segmen_peserta", "kelompok_umur"]
    assert args["group_by"] == []
