import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture(scope="session")
def database_available():
    from mcp_server.db import check_connection

    if not check_connection():
        pytest.skip("Database PostgreSQL tidak tersedia.")
    return True
