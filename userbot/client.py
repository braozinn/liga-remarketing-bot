"""Cliente Telethon - singleton com reconnect automático e StringSession opcional."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = ROOT / "userbot.session"

_client: Optional[TelegramClient] = None
_lock = asyncio.Lock()
_reconnect_task: Optional[asyncio.Task] = None

# Reconnect: tenta indefinidamente a cada N segundos
RECONNECT_INTERVAL = int(os.getenv("TELETHON_RECONNECT_INTERVAL", "30"))


def _get_credentials() -> tuple[int, str, str]:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    phone = os.getenv("TELEGRAM_PHONE", "").strip()
    if not api_id or not api_hash or not phone:
        raise RuntimeError(
            "Configure TELEGRAM_API_ID, TELEGRAM_API_HASH e TELEGRAM_PHONE no .env"
        )
    return int(api_id), api_hash, phone


def _build_session():
    """Retorna o objeto session a ser usado.

    Prioridade:
    1. TELETHON_SESSION env (StringSession — sobrevive a redeploy/reinstall)
    2. SESSION_PATH (arquivo .session local — fallback)
    """
    session_str = os.getenv("TELETHON_SESSION", "").strip()
    if session_str:
        logger.info("[client] usando StringSession do .env")
        return StringSession(session_str)
    return str(SESSION_PATH)


async def _create_client() -> TelegramClient:
    """Cria TelegramClient com auto_reconnect habilitado."""
    api_id, api_hash, _ = _get_credentials()
    return TelegramClient(
        _build_session(),
        api_id,
        api_hash,
        connection_retries=10,         # tenta 10x antes de desistir num batch
        retry_delay=5,                  # 5s entre retries
        auto_reconnect=True,            # default True mas explícito
        request_retries=5,
    )


async def _reconnect_loop():
    """Loop em background que verifica conexão a cada RECONNECT_INTERVAL.

    Se o client desconectar (rede caiu, Telegram dropped session, etc),
    tenta reconectar indefinidamente. Bot nunca fica zumbi.
    """
    global _client
    while True:
        try:
            await asyncio.sleep(RECONNECT_INTERVAL)
            if _client is None:
                continue
            if not _client.is_connected():
                logger.warning("[client] DESCONECTADO — tentando reconectar...")
                try:
                    await _client.connect()
                    if _client.is_connected():
                        logger.info("[client] ✓ reconectado")
                except Exception:
                    logger.exception("[client] falha no reconnect — vai retentar")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("[client] erro no reconnect_loop")


async def get_client() -> TelegramClient:
    """Retorna o cliente Telethon. Cria e conecta na primeira chamada.
    Inicia reconnect_loop em background na primeira chamada.
    """
    global _client, _reconnect_task
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        if _client is None:
            _client = await _create_client()
        try:
            await _client.connect()
        except Exception:
            logger.exception("[client] erro inicial conectando — reconnect_loop vai tentar")

        # Inicia reconnect loop apenas uma vez
        if _reconnect_task is None or _reconnect_task.done():
            _reconnect_task = asyncio.create_task(_reconnect_loop())
            logger.info("[client] reconnect_loop iniciado (interval=%ds)", RECONNECT_INTERVAL)

        return _client


async def export_string_session() -> str:
    """Exporta a sessão atual como StringSession.

    Usar 1 vez pra pegar a string e colocar em TELETHON_SESSION no .env.
    Daí o bot sobrevive a redeploy sem precisar do .session file.
    """
    client = await get_client()
    if hasattr(client.session, "save"):
        # SQLiteSession (arquivo) — preciso converter pra StringSession
        # Pra isso, criar novo client com StringSession e copiar dados
        api_id, api_hash, _ = _get_credentials()
        new_client = TelegramClient(StringSession(), api_id, api_hash)
        new_client.session.set_dc(
            client.session.dc_id,
            client.session.server_address,
            client.session.port,
        )
        new_client.session.auth_key = client.session.auth_key
        return new_client.session.save()
    return client.session.save()


async def start_client() -> TelegramClient:
    """Garante que o client está autenticado. Pede código se necessário."""
    client = await get_client()
    _, _, phone = _get_credentials()

    if not await client.is_user_authorized():
        logger.warning("Conta não autenticada. Enviando código pra %s", phone)
        await client.send_code_request(phone)
        code = input(">>> Digite o código que você recebeu no Telegram: ").strip()
        try:
            await client.sign_in(phone=phone, code=code)
        except Exception as e:
            if "two-step" in str(e).lower() or "password" in str(e).lower() or "2fa" in str(e).lower():
                from getpass import getpass
                pw = getpass(">>> Senha 2FA: ")
                await client.sign_in(password=pw)
            else:
                raise

    me = await client.get_me()
    logger.info("Userbot conectado como %s (id=%s)", me.first_name, me.id)
    return client


async def stop_client() -> None:
    global _client
    if _client is not None:
        await _client.disconnect()
        _client = None


async def get_private_group_entity():
    target = os.getenv("PRIVATE_GROUP", "").strip()
    if not target:
        raise RuntimeError("PRIVATE_GROUP não configurado no .env")
    client = await get_client()
    if target.lstrip("-").isdigit():
        return await client.get_entity(int(target))
    return await client.get_entity(target)
