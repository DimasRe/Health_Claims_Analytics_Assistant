import pytest

from mcp_server.db import get_statement_timeout, validate_read_only_select
from mcp_server.semantic_engine import build_query


def test_generated_queries_pass_validation():
    for dims, filters in (
        ([], {}),
        (["divre", "tahun_pelayanan"], {"tahun_pelayanan": [2022, 2023]}),
        (["fasilitas_kesehatan"], {"divre_code": "10"}),
    ):
        sql, _ = build_query("total_pelayanan", dims, filters)
        validate_read_only_select(sql)


@pytest.mark.parametrize("sql", [
    "DELETE FROM public.claims",
    "UPDATE public.claims SET service_type = 'x'",
    "DROP TABLE public.claims",
    "SELECT 1; DROP TABLE public.claims",
    "SELECT * INTO tmp FROM public.claims",
    "SELECT * FROM public.claims FOR UPDATE",
    "WITH x AS (DELETE FROM public.claims RETURNING *) SELECT * FROM x",
    "INSERT INTO public.claims DEFAULT VALUES",
])
def test_non_read_only_sql_is_rejected(sql):
    with pytest.raises(ValueError):
        validate_read_only_select(sql)


def test_statement_timeout_config(monkeypatch):
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT", "45s")
    assert get_statement_timeout() == "45s"
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT", "1; DROP")
    assert get_statement_timeout() == "30s"
