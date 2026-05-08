"""Notificações DM pro admin durante a Liga.

Centraliza todos os alertas críticos pro ADMIN_TELEGRAM_ID:
- Vision falhou em ler valor/ID de print
- Comprovante duplicado detectado
- ID mismatch (lead mandou outro ID)
- Outras situações que precisam revisão humana imediata

Tudo é best-effort — falhas no envio não derrubam o tracker.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Cooldown pra não spammar — mesma chave só notifica 1x por hora
_LAST_SENT: dict[str, datetime] = {}
_COOLDOWN_SECONDS = 3600  # 1h


def _admin_id() -> Optional[int]:
    raw = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _can_send(key: str) -> bool:
    """Rate limit por chave — evita flood de alertas."""
    last = _LAST_SENT.get(key)
    if last and (datetime.utcnow() - last).total_seconds() < _COOLDOWN_SECONDS:
        return False
    _LAST_SENT[key] = datetime.utcnow()
    return True


async def _send_dm(client, text: str) -> bool:
    """Envia DM pro ADMIN_TELEGRAM_ID. Retorna True se OK."""
    admin = _admin_id()
    if not admin or client is None:
        return False
    try:
        await client.send_message(admin, text, parse_mode="markdown", link_preview=False)
        return True
    except Exception:
        logger.exception("[notify] falha ao enviar DM para admin")
        return False


# ---------------------------------------------------------------------------
# Notificações específicas
# ---------------------------------------------------------------------------
async def notify_vision_failed(client, lead, reason: str = "vision_failed", details: str = "") -> None:
    """Vision não conseguiu extrair valor ou ID de um print.

    Manda DM pro admin com link pro lead pra revisão manual.
    Cooldown: 1 alerta por lead por hora (evita spam).
    """
    cooldown_key = f"vision_fail:{lead.id}"
    if not _can_send(cooldown_key):
        return

    nome = lead.display_name
    when = datetime.utcnow().strftime("%d/%m %H:%M")
    reason_label = {
        "vision_failed":   "🔍 IA não conseguiu ler",
        "low_confidence":  "🤔 Confiança baixa",
        "duplicate_image": "🚨 Imagem duplicada (anti-fraude)",
        "id_mismatch":     "⚠️ ID diferente do registrado",
    }.get(reason, reason)

    msg_lines = [
        f"⚠️ *Comprovante precisa revisão*",
        "",
        f"• Lead: *{nome}* (id `{lead.id}`)",
        f"• Motivo: {reason_label}",
        f"• Quando: {when} UTC",
    ]
    if details:
        # Trunca pra 200 chars pra não estourar mensagem
        clean = (details or "").strip().replace("`", "'")[:200]
        msg_lines.append(f"• Detalhe: _{clean}_")

    msg_lines += [
        "",
        f"👉 Revisa: http://localhost:8080/leads/{lead.id}",
        "💡 Ou abre /verifications/pending pra fila completa.",
    ]
    await _send_dm(client, "\n".join(msg_lines))
    logger.info("[notify] vision_failed enviado pro admin lead=%s motivo=%s", nome, reason)


async def notify_critical(client, title: str, message: str, key: Optional[str] = None) -> None:
    """Notificação crítica genérica. Use pra alertas que NÃO são por lead."""
    if key and not _can_send(key):
        return
    text = f"🚨 *{title}*\n\n{message}"
    await _send_dm(client, text)


async def notify_id_invalid(client, lead, attempted_id: str, partner_response: str = "") -> None:
    """Notifica admin quando ID que lead mandou foi rejeitado pelo @QuotexPartnerBot.

    Lead fica em waiting_id (não avança), bot NÃO responde nada pro lead.
    Você revisa manualmente.
    """
    cooldown_key = f"id_invalid:{lead.id}:{attempted_id}"
    if not _can_send(cooldown_key):
        return

    when = datetime.utcnow().strftime("%d/%m %H:%M")
    msg_lines = [
        f"⚠️ *ID INVÁLIDO no funil*",
        "",
        f"• Lead: *{lead.display_name}* (id `{lead.id}`)",
        f"• ID que mandou: `{attempted_id}`",
        f"• Quando: {when} UTC",
    ]
    if partner_response:
        clean_resp = (partner_response or "").strip().replace("`", "'")[:200]
        msg_lines.append(f"• Partner bot: _{clean_resp}_")
    msg_lines += [
        "",
        f"👉 Revisar: http://localhost:8080/liga/lead/{lead.id}",
        "💡 Lead continua em `waiting_id` — bot não responde, você decide.",
    ]
    await _send_dm(client, "\n".join(msg_lines))
    logger.info("[notify] id_invalid enviado pro admin lead=%s id=%s", lead.display_name, attempted_id)


async def notify_deposit_unconfirmed(client, lead, current_balance: float = 0.0, min_required: float = 20.0) -> None:
    """Notifica admin quando lead disse que depositou mas saldo < mínimo.

    Lead fica em waiting_deposit (não avança), bot NÃO responde nada pro lead.
    """
    cooldown_key = f"deposit_unconfirmed:{lead.id}"
    if not _can_send(cooldown_key):
        return

    when = datetime.utcnow().strftime("%d/%m %H:%M")
    msg_lines = [
        f"💰 *DEPÓSITO NÃO CONFIRMADO*",
        "",
        f"• Lead: *{lead.display_name}* (id `{lead.id}`)",
        f"• ID Quotex: `{lead.liga_account_id or '?'}`",
        f"• Saldo atual: *${current_balance:.2f}* (mín: ${min_required:.0f})",
        f"• Quando: {when} UTC",
        "",
        "_Lead disse que depositou mas saldo não bate. Pode ser depósito ainda processando, valor menor, ou lead mentindo._",
        "",
        f"👉 Revisar: http://localhost:8080/liga/lead/{lead.id}",
        "💡 Lead continua em `waiting_deposit` — bot não responde.",
    ]
    await _send_dm(client, "\n".join(msg_lines))
    logger.info("[notify] deposit_unconfirmed lead=%s saldo=$%.2f", lead.display_name, current_balance)


def notify_vision_failed_sync(client, lead, reason: str = "vision_failed", details: str = "") -> None:
    """Versão sync — agenda como task assíncrona se houver event loop, senão skipa.

    Útil pra chamar de dentro de funções sync. Best-effort.
    """
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.create_task(notify_vision_failed(client, lead, reason, details))
            return
    except RuntimeError:
        pass
    # Sem loop ativo — não notifica (raro, só se chamado em script CLI)
    logger.debug("[notify] sem event loop, pulando notify_vision_failed lead=%s", lead.id)


# ---------------------------------------------------------------------------
# Modo torneio (vigília intensiva)
# ---------------------------------------------------------------------------
def is_tournament_active() -> bool:
    """Retorna True se hoje está dentro do período do torneio.

    Lógica:
    - LIGA_TOURNAMENT_MODE=always → sempre ativo
    - LIGA_TOURNAMENT_MODE=never  → nunca ativo
    - LIGA_TOURNAMENT_MODE=auto (default) → checa LIGA_START_DATE/LIGA_END_DATE
    """
    mode = os.getenv("LIGA_TOURNAMENT_MODE", "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    # auto: usa as datas
    start = os.getenv("LIGA_START_DATE", "").strip()
    end = os.getenv("LIGA_END_DATE", "").strip()
    if not start or not end:
        return False
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        end_d = datetime.strptime(end, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        return start_d <= today <= end_d
    except Exception:
        return False


def days_until_tournament() -> Optional[int]:
    """Retorna quantos dias faltam pro torneio começar. None se já começou ou sem data."""
    start = os.getenv("LIGA_START_DATE", "").strip()
    if not start:
        return None
    try:
        start_d = datetime.strptime(start, "%Y-%m-%d").date()
        today = datetime.utcnow().date()
        delta = (start_d - today).days
        return delta if delta > 0 else None
    except Exception:
        return None
