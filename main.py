"""
main.py — Portfolio "Contact" backend (FastAPI → Telegram bridge)
================================================================================
Riceve la submission del form React (Name / Email / Project subject /
Project details) e la inoltra come messaggio formattato a Telegram, senza
database né SMTP.

Avvio locale:
    cd backend
    python -m venv .venv
    source .venv/bin/activate            # Windows: .venv\\Scripts\\activate
    pip install -r requirements.txt
    cp .env.example .env                  # poi compila i valori (vedi README)
    uvicorn main:app --reload --port 8000

Token e Chat ID NON sono hardcoded: vengono letti dal file .env.
================================================================================
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Optional

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field

# ── Config (da .env) ──────────────────────────────────────────────────────────
load_dotenv()

TELEGRAM_BOT_TOKEN: Optional[str] = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID: Optional[str] = os.getenv("TELEGRAM_CHAT_ID")

# Origins CORS: lista separata da virgole nel .env, con default per lo sviluppo.
# In produzione metti il tuo dominio (es. "https://seba.dev") oppure "*".
_raw_origins = os.getenv(
    "CORS_ORIGINS", "https://sebamox.dev"
)
ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]

TELEGRAM_API_URL = (
    f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    if TELEGRAM_BOT_TOKEN
    else None
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s · %(levelname)s · %(name)s · %(message)s",
)
logger = logging.getLogger("contact-api")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    logger.warning(
        "TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID non impostati. "
        "Copia .env.example in .env e compila i valori."
    )


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Portfolio Contact API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,          # nessun cookie/sessione → wildcard "*" ammesso
    allow_methods=["POST", "OPTIONS", "GET"],
    allow_headers=["*"],
)


# ── Modello dati: combacia ESATTAMENTE con i 4 campi del form React ────────────
class ContactRequest(BaseModel):
    """Payload inviato da ContactPage.jsx (più un timestamp opzionale)."""
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr
    # "Project subject" non è obbligatorio nella UI → opzionale anche qui.
    subject: str = Field(default="", max_length=200)
    details: str = Field(..., min_length=1, max_length=5000)
    # Il frontend allega un ISO timestamp; lo accettiamo ma non è richiesto.
    timestamp: Optional[str] = None


# ── Telegram ──────────────────────────────────────────────────────────────────
class TelegramError(Exception):
    """Sollevata quando l'API Telegram non accetta/non riceve il messaggio."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"Telegram error {status_code}: {detail}")


def _esc(text: Optional[str]) -> str:
    """Escape minimale per parse_mode=HTML di Telegram (& < >)."""
    return (
        (text or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def build_message(payload: ContactRequest) -> str:
    """Compone un messaggio HTML pulito e leggibile sul telefono."""
    ts = payload.timestamp or datetime.now(timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    subject = payload.subject.strip() if payload.subject else "—"

    return (
        "🛰 <b>NEW PROJECT INQUIRY</b>\n"
        "<i>portfolio · contact form</i>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Name</b>\n{_esc(payload.name)}\n\n"
        f"✉️ <b>Email</b>\n<code>{_esc(str(payload.email))}</code>\n\n"
        f"🏷 <b>Subject</b>\n{_esc(subject)}\n\n"
        f"📝 <b>Details</b>\n{_esc(payload.details)}\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🕒 {_esc(ts)}"
    )


async def send_telegram_message(text: str) -> None:
    """
    Invia il messaggio via Bot API. Funzione isolata = facile da testare/mockare.
    Solleva TelegramError su qualsiasi esito non riuscito.
    """
    if not TELEGRAM_API_URL or not TELEGRAM_CHAT_ID:
        raise TelegramError(500, "Messaging backend non configurato.")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                TELEGRAM_API_URL,
                json={
                    "chat_id": TELEGRAM_CHAT_ID,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
    except httpx.RequestError as exc:
        # Rete irraggiungibile / timeout verso Telegram
        raise TelegramError(502, f"Impossibile raggiungere Telegram: {exc!s}") from exc

    if resp.status_code != 200:
        # Telegram ha risposto ma ha rifiutato (token errato, chat_id errato, ecc.)
        raise TelegramError(
            502, f"Telegram ha risposto {resp.status_code}: {resp.text}"
        )


# ── Endpoints ─────────────────────────────────────────────────────────────────
@app.get("/health", tags=["meta"])
async def health() -> dict:
    """Health-check + stato configurazione (senza esporre i segreti)."""
    return {
        "status": "ok",
        "telegram_configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID),
    }


@app.post("/api/contact", status_code=status.HTTP_200_OK, tags=["contact"])
async def contact(payload: ContactRequest) -> dict:
    """
    Riceve la submission del form e la inoltra a Telegram.
    - 200  → inviato
    - 422  → payload non valido (gestito da FastAPI/Pydantic, es. email errata)
    - 500  → backend non configurato (manca token/chat id)
    - 502  → Telegram irraggiungibile o ha rifiutato il messaggio
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logger.error("Submission ricevuta ma il backend non è configurato.")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Server messaging is not configured.",
        )

    message = build_message(payload)

    try:
        await send_telegram_message(message)
    except TelegramError as exc:
        logger.error("Invio Telegram fallito: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not deliver your message right now. Please try again.",
        ) from exc

    logger.info("Inoltrata inquiry da %s <%s>", payload.name, payload.email)
    return {"ok": True, "message": "Message delivered to Telegram."}