"""
Akses database PostgreSQL yang aman dan read-only.

Semua query analitik divalidasi dengan SQLGlot (hanya satu SELECT)
lalu dijalankan di dalam transaksi READ ONLY dengan batas waktu.
"""

import os
import re
import threading
import time
from collections import OrderedDict
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from sqlglot import exp, parse


# Membaca file .env dari folder utama proyek
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_PATH)

REQUIRED_DB_VARS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)

DEFAULT_STATEMENT_TIMEOUT = "30s"
DEFAULT_WORK_MEM = "256MB"
DEFAULT_CACHE_TTL = 600
DEFAULT_CACHE_MAX_ENTRIES = 256
DEFAULT_CONNECT_TIMEOUT = 10

# Node AST yang tidak boleh muncul di mana pun dalam query
FORBIDDEN_NODES = {
    "insert",
    "update",
    "delete",
    "drop",
    "create",
    "alter",
    "command",
    "merge",
    "truncate",
    "grant",
    "revoke",
    "transaction",
    "commit",
    "rollback",
    "set",
    "use",
    "copy",
}


def get_statement_timeout():
    """
    Batas waktu query, dapat diatur melalui DB_STATEMENT_TIMEOUT
    (contoh: 30s, 2min, 500ms). Nilai tidak valid memakai default.
    """
    value = (os.getenv("DB_STATEMENT_TIMEOUT") or "").strip()

    if re.fullmatch(r"\d+\s*(ms|s|min)?", value):
        return value.replace(" ", "")

    return DEFAULT_STATEMENT_TIMEOUT


def get_work_mem():
    """
    Memori kerja per operasi sort/hash untuk query chatbot (hanya berlaku
    di transaksi query, bukan seluruh server). Default PostgreSQL 4MB
    membuat COUNT DISTINCT/GROUP BY besar tumpah ke disk dan lambat.
    Atur dengan DB_WORK_MEM (contoh: 128MB, 512MB, 1GB).
    """
    value = (os.getenv("DB_WORK_MEM") or "").strip()

    if re.fullmatch(r"\d+\s*(kB|MB|GB)", value):
        return value.replace(" ", "")

    return DEFAULT_WORK_MEM


# =========================================================
# CACHE HASIL QUERY
# =========================================================
# Data analitik jarang berubah, sedangkan chatbot sering menjalankan query
# yang sama (total keseluruhan, pertanyaan ulang). Hasil disimpan di memori
# selama QUERY_CACHE_TTL detik (default 600). Set QUERY_CACHE_TTL=0 untuk
# mematikan cache.

_query_cache = OrderedDict()
_query_cache_lock = threading.Lock()


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _cache_key(sql, params):
    return (sql, repr(list(params or [])))


def _copy_rows(rows):
    return [dict(row) for row in rows]


def clear_query_cache():
    with _query_cache_lock:
        _query_cache.clear()


def _cache_get(key):
    ttl = _env_int("QUERY_CACHE_TTL", DEFAULT_CACHE_TTL)
    if ttl <= 0:
        return None

    with _query_cache_lock:
        entry = _query_cache.get(key)
        if entry is None:
            return None

        stored_at, rows = entry
        if time.monotonic() - stored_at > ttl:
            _query_cache.pop(key, None)
            return None

        _query_cache.move_to_end(key)
        return _copy_rows(rows)


def _cache_put(key, rows):
    if _env_int("QUERY_CACHE_TTL", DEFAULT_CACHE_TTL) <= 0:
        return

    max_entries = max(1, _env_int("QUERY_CACHE_MAX_ENTRIES", DEFAULT_CACHE_MAX_ENTRIES))

    with _query_cache_lock:
        _query_cache[key] = (time.monotonic(), _copy_rows(rows))
        _query_cache.move_to_end(key)
        while len(_query_cache) > max_entries:
            _query_cache.popitem(last=False)


def get_connection():
    missing = [
        key for key in REQUIRED_DB_VARS
        if not os.getenv(key)
    ]

    if missing:
        raise RuntimeError(
            f"Konfigurasi database belum lengkap: {', '.join(missing)}"
        )

    try:
        port = int(os.getenv("DB_PORT"))
    except ValueError as exc:
        raise RuntimeError("DB_PORT harus berupa angka.") from exc

    try:
        connect_timeout = int(
            os.getenv("DB_CONNECT_TIMEOUT", DEFAULT_CONNECT_TIMEOUT)
        )
    except ValueError:
        connect_timeout = DEFAULT_CONNECT_TIMEOUT

    return psycopg.connect(
        host=os.getenv("DB_HOST"),
        port=port,
        dbname=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
        connect_timeout=connect_timeout,
        application_name="bpjs_semantic_engine",
    )


def validate_read_only_select(sql):
    """
    Memvalidasi satu pernyataan SELECT menggunakan SQLGlot.
    """

    try:
        statements = parse(sql, read="postgres")
    except Exception as exc:
        raise ValueError("SQL tidak valid.") from exc

    # Harus tepat satu statement
    if len(statements) != 1 or statements[0] is None:
        raise ValueError("Hanya satu SQL statement yang diizinkan.")

    tree = statements[0]

    # Root harus SELECT, termasuk WITH ... SELECT
    if not isinstance(tree, exp.Select):
        raise ValueError("Hanya query SELECT yang diizinkan.")

    # Periksa seluruh AST, termasuk CTE dan subquery
    for node in tree.walk():
        if node.key.lower() in FORBIDDEN_NODES:
            raise ValueError(
                f"Operasi SQL tidak diizinkan: {node.key}"
            )

    # Tolak SELECT INTO dan SELECT FOR UPDATE
    if tree.args.get("into") is not None:
        raise ValueError("SELECT INTO tidak diizinkan.")

    if tree.args.get("locks"):
        raise ValueError("SELECT dengan locking tidak diizinkan.")


def execute_query(sql, params=None):
    """
    Eksekusi query analitik dalam transaksi read-only.
    """

    if not isinstance(sql, str):
        raise ValueError("SQL harus berupa string.")

    validate_read_only_select(sql)

    key = _cache_key(sql, params)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    rows = _execute_uncached(sql, params)
    _cache_put(key, rows)
    return rows


def _execute_uncached(sql, params=None):
    with get_connection() as conn:
        with conn.cursor() as cur:

            # Melindungi database dari perubahan data
            cur.execute("SET TRANSACTION READ ONLY")

            # Membatasi waktu eksekusi query (berlaku lokal di transaksi)
            cur.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                [get_statement_timeout()],
            )

            # Memori kerja lebih besar untuk sort/hash (lokal di transaksi)
            cur.execute(
                "SELECT set_config('work_mem', %s, true)",
                [get_work_mem()],
            )

            # Menjalankan query dengan parameter terpisah
            cur.execute(sql, params or [])

            if cur.description is None:
                return []

            columns = [
                description.name
                for description in cur.description
            ]

            rows = cur.fetchall()

            return [
                dict(zip(columns, row))
                for row in rows
            ]


def check_connection():
    """Memeriksa koneksi database. Mengembalikan True jika berhasil."""
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return cur.fetchone() == (1,)
    except Exception:
        return False


if __name__ == "__main__":
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_user, current_database()"
            )
            print(cur.fetchone())