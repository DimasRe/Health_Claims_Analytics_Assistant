"""Regresi: setelan OLLAMA_* di .env harus terbaca oleh proses chatbot."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_chatbot_reads_ollama_settings_from_env_file(tmp_path):
    for folder in ("ai", "mcp_server", "config"):
        shutil.copytree(PROJECT_ROOT / folder, tmp_path / folder)
    (tmp_path / ".env").write_text(
        "OLLAMA_MODEL=qwen3:4b\nOLLAMA_NUM_CTX=4096\nOLLAMA_TIMEOUT=99\n",
        encoding="utf-8",
    )

    env = {
        key: value for key, value in os.environ.items()
        if not key.startswith("OLLAMA_")
    }
    code = (
        "from ai.qwen_mcp_client import BPJSChatbot;"
        "b = BPJSChatbot(ollama_client=object());"
        "print(b.model, b.num_ctx, int(b.ollama_timeout))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd="/", env={**env, "PYTHONPATH": str(tmp_path)},
        capture_output=True, text=True, check=True,
    )
    assert result.stdout.split() == ["qwen3:4b", "4096", "99"]
