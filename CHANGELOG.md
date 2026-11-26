# Changelog

## 1.3.0 — Persiapan publikasi
- Nama tabel & kolom disamarkan menjadi nama generik (`public.claims`, `public.regions`, `member_id`, `claim_id`, `service_date`, `billed_amount`, dll.). Nama dimensi & metrik yang dipakai chatbot tidak berubah.
- Generator data sintetis baru: `scripts/generate_sample_data.py`.
- Test integrasi dataset kini memeriksa konsistensi (rincian = total), tanpa angka acuan dari dataset tertentu.
- Angka contoh di test & prompt diganti angka fiktif.
- `.env.example` dilengkapi seluruh variabel; `.gitignore` diperluas.
- README utama kini berbahasa Inggris (versi portofolio); README bahasa Indonesia dipindah ke `docs/README.id.md`.
- Dokumentasi arsitektur dalam bahasa Inggris (`docs/architecture.md`) dan Indonesia (`docs/architecture.id.md`), serta diagram (`docs/images/`).

## 1.2.5 — Pengaman angka karangan
- Jika Qwen menjawab dengan angka data tanpa memanggil tool, jawaban itu tidak ditampilkan; Qwen diminta sekali lagi memanggil tool, dan bila tetap tidak, chatbot menyampaikan bahwa data belum diperoleh (tanpa angka).
- Angka yang berasal dari pertanyaan atau jawaban sebelumnya tetap diizinkan (pertanyaan lanjutan).
- Prompt: aturan "selalu panggil tool sebelum menjawab pertanyaan data".
- Label hasil jumlah pelayanan tidak lagi dobel ("01, DIVRE 01" menjadi "DIVRE 01").

## 1.2.4 — Dukungan model "thinking"
- Proses berpikir model yang bocor ke jawaban dibersihkan (termasuk `...</think>` tanpa tag pembuka dan `<think>` yang tidak ditutup).
- Jawaban yang terpotong karena batas `OLLAMA_NUM_PREDICT` tidak lagi menampilkan teks berpikir; muncul pesan yang jelas (atau narasi terpotong + tabel data resmi bila tool sudah berhasil).
- Opsi `OLLAMA_THINK=1` untuk model thinking (proses berpikir dipisahkan dari jawaban).
- Log teknis `HTTP Request: POST ...` disembunyikan.

## 1.2.3 — Perbaikan pembacaan .env
- Proses chatbot (`main.py`, skrip evaluasi) kini membaca `.env`. Sebelumnya hanya MCP Server yang membacanya, sehingga `OLLAMA_MODEL`, `OLLAMA_NUM_CTX`, `OLLAMA_TIMEOUT`, dll. diabaikan dan chatbot selalu memakai `qwen3:latest`.

## 1.2.2 — Ringan untuk laptop 8 GB & jawaban lebih terjaga
- Hanya tool yang relevan (rata-rata 2-3 dari 10) yang dikirim ke Qwen per pertanyaan (`ai/answer_support.select_tools`); `TOOL_SELECTION=0` untuk mengirim semua.
- System prompt modular (`ai/prompts.py`): aturan inti selalu dikirim, panduan tool hanya untuk tool terpilih. Prompt per pertanyaan ±50% lebih pendek; seluruh aturan lama tetap ada.
- Tabel "Data resmi (dihitung sistem)" dibuat Python di bawah setiap jawaban Qwen.
- Pemeriksaan angka: angka di narasi Qwen dicocokkan dengan hasil tool; bila tidak ditemukan muncul peringatan.
- Riwayat percakapan dipadatkan (hanya pertanyaan + jawaban akhir), hasil tool untuk Qwen dikirim dalam JSON ringkas.
- Batas waktu Qwen (`OLLAMA_TIMEOUT`, default 180 detik) dengan pesan error yang jelas.
- Default baru: `OLLAMA_NUM_CTX=8192`, `OLLAMA_NUM_PREDICT=512`, `MAX_HISTORY_TURNS=6`.
- Aturan baru di prompt: jangan memanggil `inspect_data` kecuali pengguna menanyakan katalog.

## 1.2.1 — Performa di data besar
- Peserta/SJP unik dihitung dengan `SELECT DISTINCT` + `COUNT(*)` (hasil identik dengan `COUNT(DISTINCT)`, dapat memakai hash aggregate & paralelisme). Bisa dimatikan dengan `DISTINCT_COUNT_REWRITE=0`.
- `compare_metrics`, `profile_summary`, dan total keseluruhan menghitung metrik distinct secara terpisah lalu menggabungkannya di Python.
- `work_mem` per query (default 256MB, `DB_WORK_MEM`).
- Cache hasil query di memori (default 10 menit, `QUERY_CACHE_TTL`).

## 1.2.0 — Cakupan pertanyaan analitik

### Fitur baru
- Tool `compare_metrics`: beberapa metrik sekaligus per kategori, persentase kontribusi, peringkat per metrik, kategori tertinggi/terendah, dan penanda apakah kategori teratas sama untuk semua metrik.
- Tool `top_categories`: N kategori teratas/terbawah, opsional per kelompok (window function `ROW_NUMBER`).
- Tool `profile_summary`: ringkasan karakteristik beberapa dimensi + daftar temuan (insights) berbasis angka yang dihitung Python.
- `metric_share` mendukung `within` (persentase di dalam kelompok, mis. distribusi umur pada tiap jenis kelamin) dan pengurutan.
- `analyze_data` mendukung `order` (`asc`/`desc`) dan `sort_by` (`metric`/`dimension`) untuk pertanyaan "terbanyak"/"paling sedikit".
- `compare_costs` mendukung `dimensions` (rincian tagihan, verifikasi, selisih, persentase per DIVRE/segmen).
- Dimensi diaktifkan: diagnosis primer, diagnosis sekunder (dipecah per kode `;`), tingkat keparahan, kategori kronis, kategori katastrofik, jenis kepulangan, kelas rawat, jenis/tipe/kepemilikan fasilitas.
- Metrik baru: rata-rata biaya tagihan, rata-rata biaya RS, median biaya verifikasi, biaya verifikasi per peserta, rata-rata pelayanan per peserta.
- Bank pertanyaan (`tests/questions/question_bank.yaml`) + test otomatis + skrip evaluasi Qwen (`scripts/evaluate_questions.py`) dengan laporan Markdown/CSV.
- API mengembalikan `tools_used`; UI menampilkan "Sumber data".

### Perubahan perilaku
- Pertanyaan analitis tentang jumlah pelayanan ("apakah", "mana yang", "bandingkan", dll.) kini dijawab Qwen dengan total terverifikasi, tidak lagi langsung dipotong menjadi daftar angka — sehingga Qwen dapat membandingkan atau memanggil tool lanjutan. Pertanyaan hitungan sederhana tetap diformat Python seperti sebelumnya.
- Default `OLLAMA_NUM_PREDICT` 1024 (sebelumnya 500) agar ringkasan panjang tidak terpotong; `OLLAMA_NUM_CTX` 16384 ditambahkan.
- Pengurutan metrik memakai `NULLS LAST`.

## 1.1.0

### Perbaikan bug
- Alias filter di `dimensions.yaml` (`nama_divre`, `nama_divisi_regional`) kini benar-benar dibaca; sebelumnya menghasilkan error "Dimensi tidak dikenal".
- Jawaban terverifikasi (jumlah/persentase pelayanan) kini tersimpan di riwayat percakapan, sehingga pertanyaan lanjutan tetap punya konteks.
- Riwayat percakapan dikembalikan ke kondisi semula bila terjadi error (tidak lagi rusak oleh pesan setengah jadi).
- Riwayat percakapan dibatasi (`MAX_HISTORY_TURNS`) agar konteks Qwen tidak penuh pada sesi panjang.
- MCP Server selalu dijalankan dari folder proyek (sebelumnya gagal jika `main.py`/`api_server.py` dijalankan dari folder lain) dan menerima variabel environment.
- Deteksi kode cabang/RS dari pertanyaan tidak lagi salah menangkap kata biasa ("kode cabang tersebut" -> `TERSEBUT`).
- Urutan hasil yang bernilai sama kini deterministik (tie-breaker).
- Blok `<think>` dari Qwen dibersihkan dari jawaban.
- Error database dicatat ke `logs/mcp_server.log` (sebelumnya hilang tanpa jejak); detail tetap tidak dikirim ke pengguna.
- `fastapi` dan `uvicorn` ditambahkan ke `requirements.txt`.
- Test lama dapat dijalankan dengan pytest (sebelumnya error saat koleksi).

### Peningkatan
- Tool MCP baru `metric_share`: persentase metrik per kategori dihitung Calculation Engine (sesuai aturan "jangan menghitung sendiri").
- Penanganan error & validasi filter di MCP Server disatukan (tanpa duplikasi kode), format respons tetap sama.
- Konfigurasi YAML di-cache dan otomatis dimuat ulang saat file berubah.
- Rincian per bulan diurutkan kronologis; filter alias `bulan` didukung.
- Constraint eksplisit (kode DIVRE/cabang/RS) juga diterapkan pada `compare_costs`, `service_mix`, `metric_share`; mendukung beberapa DIVRE sekaligus.
- API: `session_id` per pengguna, endpoint `/reset`, `/health` memeriksa database, konteks filter Tableau, CORS opsional.
- Extension Tableau: inisialisasi Tableau Extensions API, membaca filter dashboard aktif, tombol percakapan baru, contoh pertanyaan, indikator proses, timeout, format teks tebal yang aman.
- CLI: perintah `reset` / `baru`.
- File `.bak` dipindahkan ke `archive/backups/` (tidak dihapus).
