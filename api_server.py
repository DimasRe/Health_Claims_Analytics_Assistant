"""
REST API untuk BPJS Analytics Chatbot.

Menyajikan:
- POST /chat                 : bertanya ke chatbot
- POST /reset                : memulai percakapan baru
- GET  /health               : status backend, MCP, dan database
- GET  /extension/index.html : UI Tableau Extension (file .trex)

Jalankan:
    uvicorn api_server:app --host 127.0.0.1 --port 8000
atau:
    python api_server.py
"""

import asyncio
import logging
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ai.qwen_mcp_client import BPJSChatbot
from mcp_server.db import check_connection


BASE_DIR = Path(__file__).resolve().parent
EXTENSION_DIR = BASE_DIR / "extension"

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger("bpjs.api")

MAX_CONVERSATIONS = int(os.getenv("MAX_CONVERSATIONS", "200"))

chatbot = None
chatbot_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global chatbot

    async with BPJSChatbot() as client:
        chatbot = client
        logger.info("Chatbot siap (model: %s).", client.model)
        yield
        chatbot = None


app = FastAPI(
    title="BPJS Analytics Chatbot API",
    version="1.1.0",
    lifespan=lifespan,
)

# CORS hanya diaktifkan jika CORS_ORIGINS diisi, misalnya:
# CORS_ORIGINS=http://localhost:8000,https://tableau.example.com
# Extension yang disajikan dari server ini (same-origin) tidak memerlukan CORS.
_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "").split(",")
    if origin.strip()
]

if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )


class ChatRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    # Opsional: memisahkan riwayat percakapan antar pengguna/tab.
    session_id: str | None = Field(default=None, max_length=64)
    # Opsional: konteks filter dashboard Tableau dari extension.
    tableau_context: dict[str, Any] | str | None = None


class ChatResponse(BaseModel):
    answer: str
    session_id: str | None = None
    # Nama MCP tool yang dipakai untuk menjawab (untuk transparansi/debug).
    tools_used: list[str] = Field(default_factory=list)
    # Spesifikasi grafik dari data resmi (digambar oleh extension).
    charts: list[dict[str, Any]] = Field(default_factory=list)


class ResetRequest(BaseModel):
    session_id: str | None = Field(default=None, max_length=64)


def _limit_conversations(current_session_id):
    """Membatasi jumlah riwayat percakapan yang disimpan di memori."""
    conversations = chatbot.conversations
    while len(conversations) > MAX_CONVERSATIONS:
        oldest = next(
            (
                key for key in conversations
                if key not in (current_session_id, "default")
            ),
            None,
        )
        if oldest is None:
            break
        conversations.pop(oldest, None)


@app.get("/health")
async def health():
    database_ok = await asyncio.to_thread(check_connection)

    return {
        "status": "healthy",
        "service": "BPJS Analytics Chatbot API",
        "chatbot_ready": chatbot is not None and chatbot.is_ready,
        "database_connected": database_ok,
        "model": chatbot.model if chatbot else None,
        "tools": sorted(chatbot.available_tools) if chatbot else [],
    }


@app.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    if chatbot is None:
        raise HTTPException(
            status_code=503,
            detail="Chatbot belum siap.",
        )

    question = request.question.strip()

    if not question:
        raise HTTPException(
            status_code=400,
            detail="Pertanyaan tidak boleh kosong.",
        )

    session_id = (request.session_id or "").strip() or None

    try:
        async with chatbot_lock:
            answer = await chatbot.ask(
                question,
                conversation_id=session_id,
                context=request.tableau_context,
            )
            _limit_conversations(session_id)

            tools_used = [item["tool"] for item in chatbot.last_trace]
            charts = list(chatbot.last_charts)

        return ChatResponse(
            answer=answer,
            session_id=session_id,
            tools_used=tools_used,
            charts=charts,
        )

    except Exception:
        logger.exception("Gagal memproses pertanyaan.")
        raise HTTPException(
            status_code=500,
            detail=(
                "Gagal memproses pertanyaan. Pastikan Ollama/Qwen "
                "dan database berjalan."
            ),
        )


@app.post("/reset")
async def reset(request: ResetRequest | None = None):
    if chatbot is None:
        raise HTTPException(
            status_code=503,
            detail="Chatbot belum siap.",
        )

    session_id = ((request.session_id if request else None) or "").strip() or None

    async with chatbot_lock:
        chatbot.reset_conversation(session_id)

    return {"status": "ok", "session_id": session_id}


@app.get("/session")
async def new_session():
    """Membuat session_id acak untuk klien yang membutuhkannya."""
    return {"session_id": uuid.uuid4().hex}


@app.get("/", include_in_schema=False)
async def root():
    return RedirectResponse(url="/extension/index.html")


@app.get("/extension/index.html", include_in_schema=False)
async def extension_page():
    html_file = EXTENSION_DIR / "index.html"
    return FileResponse(html_file)


# File statis lain untuk extension (file .trex, library Tableau lokal, dll.)
app.mount(
    "/extension",
    StaticFiles(directory=EXTENSION_DIR),
    name="extension",
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host=os.getenv("API_HOST", "127.0.0.1"),
        port=int(os.getenv("API_PORT", "8000")),
        reload=False,
    )
