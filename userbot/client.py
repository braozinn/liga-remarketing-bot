"""Cliente Telethon - singleton."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

from telethon import TelegramClient

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
SESSION_PATH = ROOT / "userbot.session"

_client: Optional[TelegramClient] = None
_lock = asyncio.Lock()


def _get_credentials() -> tuple[int, str, str]:
    api_id = os.getenv("TELEGRAM_API_ID", "").strip()
    api_hash = os.getenv("TELEGRAM_API_HASH", "").strip()
    phone = os.getenv("TELEGRAM_PHONE", "").strip()
    if not api_id or not api_hash or not phone:
        raise RuntimeError(
            "Configure TELEGRAM_API_ID, TELEGRAM_API_HASH e TELEGRAM_PHONE no .env"
        )
    return int(api_id), api_hash, phone


async def get_client() -> TelegramClient:
    """Retorna o cliente Telethon. Cria e conecta na primeira chamada."""
    global _client
    async with _lock:
        if _client is not None and _client.is_connected():
            return _client
        api_id, api_hash, _ = _get_credentials()
        _client = TelegramClient(str(SESSION_PATH), api_id, api_hash)
        await _client.connect()
        return _client


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
