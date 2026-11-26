# Library Tableau Extensions (opsional)

Letakkan file `tableau.extensions.1.latest.min.js` di folder ini agar extension
tidak bergantung pada internet. File dapat diunduh dari repositori resmi:
https://github.com/tableau/extensions-api/tree/main/lib

Jika file ini tidak ada, `index.html` otomatis memuat library dari CDN resmi
Tableau (`extensions.tableauusercontent.com`).

# Chart.js

`chart.umd.min.js` adalah Chart.js v4.4.1 (lisensi MIT, lihat
`chart.js.LICENSE.md`) untuk menggambar grafik di bawah setiap jawaban.
Disimpan lokal agar grafik tetap tampil tanpa internet; jika file ini
dihapus, extension memakai CDN cdnjs.
