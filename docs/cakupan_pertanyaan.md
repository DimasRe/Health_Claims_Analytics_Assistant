# Cakupan Pertanyaan

Dibuat dari `tests/questions/question_bank.yaml`. Kolom *Panggilan acuan* adalah tool + argumen yang terbukti menjawab pertanyaan (diuji oleh `tests/test_question_bank.py`).

| No | Pertanyaan | Tool yang tepat | Panggilan acuan |
|---|---|---|---|
| 1 | Bagaimana distribusi peserta berdasarkan jenis kelamin? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['jenis_kelamin'] |
| 2 | Kelompok umur mana yang memiliki jumlah peserta terbanyak? | analyze_data, top_categories, metric_share | `analyze_data` metric=total_peserta_unik, dimensions=['kelompok_umur'], order=desc |
| 3 | Bagaimana komposisi peserta berdasarkan segmen kepesertaan? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['segmen_peserta'] |
| 4 | Berapa persentase peserta pada masing-masing kelas hak? | metric_share | `metric_share` metric=total_peserta_unik, dimensions=['kelas_hak'] |
| 5 | Apakah jumlah peserta perempuan lebih banyak dibandingkan laki-laki? | analyze_data, metric_share | `analyze_data` metric=total_peserta_unik, dimensions=['jenis_kelamin'] |
| 6 | Bagaimana distribusi kelompok umur pada masing-masing jenis kelamin? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['jenis_kelamin', 'kelompok_umur'], within=['jenis_kelamin'], limit=100 |
| 7 | Segmen kepesertaan mana yang memiliki jumlah peserta paling sedikit? | analyze_data, top_categories | `analyze_data` metric=total_peserta_unik, dimensions=['segmen_peserta'], order=asc |
| 8 | Bagaimana perbandingan distribusi peserta berdasarkan kelompok umur dan segmen kepesertaan? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['kelompok_umur', 'segmen_peserta'], within=['kelompok_umur'], limit=100 |
| 9 | Berapa total biaya tagihan dan biaya verifikasi secara keseluruhan? | compare_costs, compare_metrics | `compare_costs`  |
| 10 | Berapa rata-rata biaya verifikasi per pelayanan? | analyze_data | `analyze_data` metric=rata_rata_biaya_verifikasi |
| 11 | Bagaimana perbandingan biaya tagihan dan biaya verifikasi berdasarkan DIVRE? | compare_costs, compare_metrics | `compare_costs` dimensions=['divre'] |
| 12 | Wilayah mana yang memiliki total biaya verifikasi tertinggi? | analyze_data, top_categories | `analyze_data` metric=total_biaya_verifikasi, dimensions=['divre'], order=desc |
| 13 | Bagaimana distribusi biaya verifikasi berdasarkan jenis pelayanan RJTL dan RITL? | metric_share, analyze_data, compare_metrics | `metric_share` metric=total_biaya_verifikasi, dimensions=['jenis_pelayanan'] |
| 14 | Berapa selisih biaya tagihan dan verifikasi berdasarkan wilayah? | compare_costs, analyze_data | `compare_costs` dimensions=['divre'] |
| 15 | Apakah wilayah dengan jumlah pelayanan tertinggi juga memiliki biaya verifikasi tertinggi? | compare_metrics | `compare_metrics` metrics=['total_pelayanan', 'total_biaya_verifikasi'], dimensions=['divre'] |
| 16 | Bagaimana perbandingan biaya rumah sakit berdasarkan wilayah? | analyze_data, metric_share, compare_metrics | `metric_share` metric=total_biaya_rs, dimensions=['divre'] |
| 17 | Bagaimana distribusi jenis kelamin pada masing-masing segmen peserta? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['segmen_peserta', 'jenis_kelamin'], within=['segmen_peserta'], limit=100 |
| 18 | Bagaimana distribusi kelas hak berdasarkan segmen peserta? | metric_share, analyze_data | `metric_share` metric=total_peserta_unik, dimensions=['segmen_peserta', 'kelas_hak'], within=['segmen_peserta'], limit=100 |
| 19 | Segmen peserta mana yang memiliki rata-rata biaya verifikasi tertinggi? | analyze_data, top_categories, compare_metrics | `analyze_data` metric=rata_rata_biaya_verifikasi, dimensions=['segmen_peserta'], order=desc |
| 20 | Bagaimana perbandingan biaya verifikasi berdasarkan kelompok umur? | compare_metrics, analyze_data, metric_share | `compare_metrics` metrics=['total_biaya_verifikasi', 'rata_rata_biaya_verifikasi'], dimensions=['kelompok_umur'] |
| 21 | Apakah kelompok umur yang lebih tua memiliki rata-rata biaya verifikasi lebih tinggi? | analyze_data, compare_metrics | `analyze_data` metric=rata_rata_biaya_verifikasi, dimensions=['kelompok_umur'], sort_by=dimension |
| 22 | Bagaimana komposisi RJTL dan RITL berdasarkan kelompok umur? | metric_share, analyze_data | `metric_share` metric=total_pelayanan, dimensions=['kelompok_umur', 'jenis_pelayanan'], within=['kelompok_umur'], limit=100 |
| 23 | Bagaimana distribusi pelayanan berdasarkan bulan dan jenis pelayanan? | analyze_data, metric_share | `analyze_data` metric=total_pelayanan, dimensions=['bulan_pelayanan', 'jenis_pelayanan'], limit=100 |
| 24 | Apakah terdapat perbedaan biaya verifikasi antara RJTL dan RITL? | compare_metrics, analyze_data | `compare_metrics` metrics=['total_biaya_verifikasi', 'rata_rata_biaya_verifikasi', 'median_biaya_verifikasi'], dimensions=['jenis_pelayanan'] |
| 25 | Apa diagnosis primer yang paling sering muncul? | top_categories, analyze_data | `top_categories` metric=total_pelayanan, dimensions=['diagnosis_primer'], top_n=10 |
| 26 | Bagaimana distribusi diagnosis berdasarkan jenis pelayanan? | top_categories, metric_share, analyze_data | `top_categories` metric=total_pelayanan, dimensions=['diagnosis_primer'], group_by=['jenis_pelayanan'], top_n=5 |
| 27 | Diagnosis apa yang paling banyak ditemukan pada pelayanan rawat inap? | top_categories, analyze_data | `top_categories` metric=total_pelayanan, dimensions=['diagnosis_primer'], filters={'jenis_pelayanan': 'RITL'}, top_n=5 |
| 28 | Bagaimana distribusi tingkat keparahan pelayanan? | metric_share, analyze_data | `metric_share` metric=total_pelayanan, dimensions=['severity_level'] |
| 29 | Bagaimana distribusi kategori penyakit kronis dan katastrofik? | metric_share, analyze_data | `metric_share` metric=total_pelayanan, dimensions=['kategori_kronis']<br>`metric_share` metric=total_pelayanan, dimensions=['kategori_katastrofik'] |
| 30 | Apa jenis kepulangan pasien yang paling banyak tercatat? | analyze_data, top_categories, metric_share | `analyze_data` metric=total_pelayanan, dimensions=['jenis_kepulangan'], order=desc |
| 31 | Temukan lima kelompok peserta dengan total biaya verifikasi terbesar. | top_categories, analyze_data | `top_categories` metric=total_biaya_verifikasi, dimensions=['segmen_peserta', 'kelompok_umur', 'jenis_kelamin'], top_n=5 |
| 32 | Apakah distribusi biaya pelayanan berbeda antara peserta laki-laki dan perempuan? | compare_metrics, analyze_data, metric_share | `compare_metrics` metrics=['total_biaya_verifikasi', 'rata_rata_biaya_verifikasi', 'median_biaya_verifikasi'], dimensions=['jenis_kelamin'] |
| 33 | Kelompok umur mana yang memiliki rata-rata biaya verifikasi paling rendah? | analyze_data, top_categories | `analyze_data` metric=rata_rata_biaya_verifikasi, dimensions=['kelompok_umur'], order=asc |
| 34 | Bandingkan karakteristik pelayanan RJTL dan RITL berdasarkan kelompok umur. | compare_metrics, metric_share, analyze_data | `compare_metrics` metrics=['total_pelayanan', 'total_biaya_verifikasi', 'rata_rata_biaya_verifikasi'], dimensions=['kelompok_umur', 'jenis_pelayanan'], limit=100 |
| 35 | Jika dilihat dari segmen peserta, bagaimana perbedaan jumlah pelayanan dan biaya verifikasinya? | compare_metrics | `compare_metrics` metrics=['total_pelayanan', 'total_biaya_verifikasi'], dimensions=['segmen_peserta'] |
| 36 | Buat ringkasan karakteristik peserta berdasarkan jenis kelamin, umur, dan segmen. | profile_summary | `profile_summary` dimensions=['jenis_kelamin', 'kelompok_umur', 'segmen_peserta'] |
| 37 | Temukan kategori dengan jumlah pelayanan tinggi tetapi rata-rata biaya verifikasinya rendah. | compare_metrics, profile_summary | `compare_metrics` metrics=['total_pelayanan', 'rata_rata_biaya_verifikasi'], dimensions=['segmen_peserta']<br>`profile_summary`  |
| 38 | Apa temuan menarik dari distribusi peserta dan biaya pelayanan dalam dataset ini? | profile_summary | `profile_summary`  |
| 39 | Berapa jumlah pelayanan RJTL dan RITL? | analyze_data, service_mix | `analyze_data` metric=total_pelayanan, dimensions=['jenis_pelayanan'] |
| 40 | Berapa persentase pelayanan RJTL dan RITL? | service_mix, analyze_data, metric_share | `service_mix`  |
| 41 | Berapa pertumbuhan jumlah pelayanan dari tahun 2022 ke 2023? | service_growth, metric_growth | `service_growth` previous_year=2022, current_year=2023 |
| 42 | Berapa pertumbuhan total biaya tagihan dari tahun 2022 ke 2023? | metric_growth | `metric_growth` metric=total_biaya_tagihan, previous_year=2022, current_year=2023 |
| 43 | Berapa total biaya tagihan DIVRE 10? | analyze_data, compare_costs | `analyze_data` metric=total_biaya_tagihan, filters={'divre_code': '10'} |
| 44 | Berapa jumlah pelayanan setiap tahun? | analyze_data | `analyze_data` metric=total_pelayanan, dimensions=['tahun_pelayanan'], limit=100 |
| 45 | Metrik dan dimensi apa saja yang tersedia? | inspect_data | `inspect_data`  |
