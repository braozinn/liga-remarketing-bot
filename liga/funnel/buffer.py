"""Buffer de mensagens com debounce pro funil.

Problema: lead manda múltiplas mensagens em sequência (ex: 'cree la cuenta'
+ print da conta), bot responde cada uma separadamente → respostas
conflitantes/duplicadas, parece funil burlable.

Solução: quando chega uma mensagem nova, espera N segundos. Se chegar
outra mensagem do mesmo lead nesse intervalo, RESETA o timer. Só processa
quando o lead "para de digitar" (N segundos sem novas msgs).

Quando processa, AGREGA tudo:
- Concatena textos
- Usa a última imagem (se houver)
- Considera todo o contexto da janela como UMA intenção

Configurável via .env:
- FUNNEL_DEBOUNCE_SECONDS: default 7 (tempo de espera após cada msg)
- FUNNEL_DEBOUNCE_MAX_WAIT: default 30 (timeout máximo desde a 1ª msg)
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Awaitable, Callable, Optional

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = int(os.getenv("FUNNEL_DEBOUNCE_SECONDS", "2"))
MAX_WAIT_SECONDS = int(os.getenv("FUNNEL_DEBOUNCE_MAX_WAIT", "10"))


class _LeadBuffer:
    """Estado de buffer pra um lead: msgs acumuladas + task pendente."""
    __slots__ = ("messages", "started_at", "task")

    def __init__(self):
        self.messages: list[dict] = []
        self.started_at: float = 0.0
        self.task: Optional[asyncio.Task] = None


# Singleton: lead_id -> _LeadBuffer
_buffers: dict[int, _LeadBuffer] = {}
_lock = asyncio.Lock()


async def submit(
    lead_id: int,
    message_text: str,
    is_image: bool,
    image_analysis: Optional[dict],
    image_bytes: Optional[bytes],
    callback: Callable[..., Awaitable[None]],
) -> None:
    """Adiciona mensagem ao buffer do lead. Quando ele para de mandar
    (debounce), `callback(combined_text, has_image, image_analysis, image_bytes)`
    é chamado UMA vez com tudo agregado.

    Se outra mensagem chegar antes do timer disparar, reseta o timer.
    """
    now = time.time()
    async with _lock:
        if lead_id not in _buffers:
            _buffers[lead_id] = _LeadBuffer()
            _buffers[lead_id].started_at = now

        buf = _buffers[lead_id]
        buf.messages.append({
            "text": message_text or "",
            "is_image": is_image,
            "image_analysis": image_analysis,
            "image_bytes": image_bytes,
            "ts": now,
        })

        # Cancela task anterior (se existir e não rodou ainda)
        if buf.task and not buf.task.done():
            buf.task.cancel()

        # Agenda nova task — espera DEBOUNCE_SECONDS, mas no máximo
        # MAX_WAIT_SECONDS desde a primeira mensagem
        elapsed = now - buf.started_at
        wait_time = max(0.5, min(DEBOUNCE_SECONDS, MAX_WAIT_SECONDS - elapsed))
        buf.task = asyncio.create_task(_fire_after(lead_id, wait_time, callback))

        logger.debug(
            "[buffer] lead=%d msg #%d adicionada (wait=%.1fs, total=%d msgs)",
            lead_id, len(buf.messages), wait_time, len(buf.messages),
        )


async def _fire_after(
    lead_id: int,
    wait_time: float,
    callback: Callable[..., Awaitable[None]],
) -> None:
    """Espera o tempo de debounce, depois agrega e dispara callback."""
    try:
        await asyncio.sleep(wait_time)
    except asyncio.CancelledError:
        # Outro handler cancelou (chegou msg nova) — desiste silenciosamente
        return

    async with _lock:
        buf = _buffers.pop(lead_id, None)

    if not buf or not buf.messages:
        return

    # Agrega
    texts = [m["text"] for m in buf.messages if m["text"]]
    combined_text = " · ".join(texts) if texts else ""

    # Última imagem do batch (mais recente prevalece pra Vision)
    last_img = None
    for m in reversed(buf.messages):
        if m["is_image"]:
            last_img = m
            break
    has_image = last_img is not None
    image_analysis = last_img["image_analysis"] if last_img else None
    image_bytes = last_img["image_bytes"] if last_img else None

    logger.info(
        "[buffer] lead=%d processando %d msg(s) agregadas (combined_text=%r, has_image=%s)",
        lead_id, len(buf.messages),
        (combined_text or "")[:80], has_image,
    )

    try:
        await callback(combined_text, has_image, image_analysis, image_bytes)
    except Exception:
        logger.exception("[buffer] erro no callback agregado")


async def reset(lead_id: int) -> None:
    """Limpa buffer de um lead (caso queira resetar manualmente).
    Async + lock pra evitar race com submit/_fire_after.
    """
    async with _lock:
        buf = _buffers.pop(lead_id, None)
    if buf and buf.task and not buf.task.done():
        buf.task.cancel()


def stats() -> dict:
    """Estatísticas pra debug.

    Snapshot via list() pra ser seguro mesmo sem lock — operação atômica
    em CPython garantida por GIL pra dict.keys() copy.
    """
    snapshot_keys = list(_buffers.keys())
    return {
        "active_buffers": len(snapshot_keys),
        "leads_buffering": snapshot_keys,
        "debounce_seconds": DEBOUNCE_SECONDS,
        "max_wait_seconds": MAX_WAIT_SECONDS,
    }
