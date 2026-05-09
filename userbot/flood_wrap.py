"""Wrapper global pra capturar FloodWaitError do Telethon e retentar.

Telegram aplica FloodWait quando uma conta faz muitas chamadas API muito
rápido. Sem tratamento, qualquer falha de FloodWait derruba o handler.
Com este wrapper, esperamos o tempo que o Telegram pediu e tentamos de
novo até MAX_RETRIES vezes.

Uso:
    from userbot.flood_wrap import safe_call

    result = await safe_call(client.send_message, target, "msg")

Ou como decorator:
    from userbot.flood_wrap import flood_safe

    @flood_safe(max_retries=3)
    async def minha_funcao():
        ...
"""
from __future__ import annotations

import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Limite máximo de wait (segundos) — se Telegram pedir mais que isso,
# desistimos e deixamos a exception propagar (provavelmente conta bloqueada)
MAX_FLOOD_WAIT_SECONDS = 600  # 10 min
DEFAULT_MAX_RETRIES = 3


async def safe_call(
    coro_func: Callable[..., Awaitable[T]],
    *args,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_wait: int = MAX_FLOOD_WAIT_SECONDS,
    **kwargs,
) -> T:
    """Chama coro_func(*args, **kwargs) com retry em FloodWaitError.

    Se Telegram retornar FloodWait, espera o tempo pedido + 2s de margem
    e tenta de novo. Até max_retries vezes.

    Se wait_seconds > max_wait, desiste e propaga a exception.
    """
    try:
        from telethon.errors import FloodWaitError
    except ImportError:
        # Telethon não instalado em algum ambiente — chama direto
        return await coro_func(*args, **kwargs)

    last_exc = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_func(*args, **kwargs)
        except FloodWaitError as e:
            last_exc = e
            wait = getattr(e, "seconds", None) or 30
            if wait > max_wait:
                logger.error(
                    "[flood_wrap] FloodWait %ds > max %ds — desistindo (conta pode estar restrita)",
                    wait, max_wait,
                )
                raise
            if attempt >= max_retries:
                logger.warning(
                    "[flood_wrap] esgotou %d retries — propagando FloodWait",
                    max_retries,
                )
                raise
            wait_with_margin = wait + 2
            logger.info(
                "[flood_wrap] FloodWait %ds (tentativa %d/%d) — aguardando %ds...",
                wait, attempt + 1, max_retries, wait_with_margin,
            )
            await asyncio.sleep(wait_with_margin)
    if last_exc:
        raise last_exc
    raise RuntimeError("safe_call: loop terminou sem retornar nem dar raise")


def flood_safe(max_retries: int = DEFAULT_MAX_RETRIES, max_wait: int = MAX_FLOOD_WAIT_SECONDS):
    """Decorator wrapper pro safe_call.

    Uso:
        @flood_safe()
        async def minha_funcao(client, ...):
            return await client.send_message(...)
    """
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            return await safe_call(func, *args, max_retries=max_retries, max_wait=max_wait, **kwargs)
        return wrapper
    return decorator
