"""Envio de mensagens — duas vias:

1. forward_to_lead: encaminha mensagens existentes (drop_author=True)
2. send_text_to_lead: envia texto novo (modo AI)

Ambas com anti-ban: delay aleatório, rate limit, pausa longa, backoff em FloodWait.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    UserPrivacyRestrictedError,
    UserIsBlockedError,
    ChatWriteForbiddenError,
)

from db import SessionLocal
from db.models import Lead, LeadStatus, Send, SendStatus

from .client import get_client

logger = logging.getLogger(__name__)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.getenv(key, str(default)))
    except ValueError:
        return default


@dataclass
class SendResult:
    success: bool
    telegram_message_ids: List[int] = field(default_factory=list)
    error: Optional[str] = None
    skipped_reason: Optional[str] = None


class RateLimiter:
    def __init__(self):
        self._sent_timestamps: list[float] = []
        self._sent_total = 0

    def _max_per_hour(self) -> int:
        return _env_int("MAX_SENDS_PER_HOUR", 120)

    def _long_pause_every(self) -> int:
        return _env_int("LONG_PAUSE_EVERY", 80)

    def _long_pause_seconds(self) -> int:
        return _env_int("LONG_PAUSE_SECONDS", 180)

    async def wait_if_needed(self) -> None:
        now = time.time()
        self._sent_timestamps = [t for t in self._sent_timestamps if now - t < 3600]
        if len(self._sent_timestamps) >= self._max_per_hour():
            wait = 3600 - (now - self._sent_timestamps[0]) + 5
            logger.warning("Limite/h atingido. Pausando %ds.", int(wait))
            await asyncio.sleep(max(60, wait))

        if self._sent_total > 0 and self._sent_total % self._long_pause_every() == 0:
            pause = self._long_pause_seconds()
            logger.info("Pausa longa após %d envios: %ds", self._sent_total, pause)
            await asyncio.sleep(pause)

    def record(self) -> None:
        self._sent_timestamps.append(time.time())
        self._sent_total += 1


_rate_limiter = RateLimiter()


def _interpolate_text(text: str, first_name: str = "", username: str = "") -> str:
    """Substitui placeholders no texto pelo nome/username do destinatário.

    Suporta (case-insensitive):
      [nombre], [Nombre], [NOMBRE]   (espanhol)
      [primer_nombre]
      [nome], [Nome]                 (português)
      [primeiro_nome]
      [name], [first_name]           (inglês)
      [username]                     (substitui pelo @username)

    Mantém suporte às formas antigas com {chaves}: {nome}, {first_name},
    {primer_nombre}, {primer_nombre}, {username}.
    """
    if not text:
        return text
    fn = first_name or ""
    un = f"@{username}" if username else ""

    # Forma nova preferida com [colchetes]
    name_pattern = re.compile(
        r"\[(nombre|primer_?nombre|nome|primeiro_?nome|name|first_?name)\]",
        re.IGNORECASE,
    )
    text = name_pattern.sub(fn, text)
    text = re.sub(r"\[username\]", un, text, flags=re.IGNORECASE)

    # Backwards compat com {chaves}
    text = re.sub(
        r"\{(nombre|primer_?nombre|nome|primeiro_?nome|name|first_?name)\}",
        fn, text, flags=re.IGNORECASE,
    )
    text = re.sub(r"\{username\}", un, text, flags=re.IGNORECASE)
    return text


def _interpolate(text: str, lead: Lead) -> str:
    """Wrapper pra interpolar usando dados do Lead do banco."""
    return _interpolate_text(text, lead.first_name or "", lead.username or "")


def split_into_blocks(text: str) -> List[str]:
    """Divide o texto em blocos por linhas em branco.

    Cada bloco vira uma MENSAGEM SEPARADA no Telegram. Permite que o usuário
    escreva o script no painel separado por linha em branco e o bot envie
    cada parte como mensagem independente.
    """
    if not text:
        return []
    blocks = re.split(r"\n\s*\n+", text)
    return [b.strip() for b in blocks if b and b.strip()]


# ---------------------------------------------------------------------------
# Vídeo bolinha (videoNote) - preparação correta
# ---------------------------------------------------------------------------
from pathlib import Path as _PathRoot

# Procura ffmpeg/ffprobe primeiro na pasta do projeto, depois no PATH do sistema
_PROJECT_ROOT = _PathRoot(__file__).resolve().parent.parent

def _find_binary(name: str) -> str:
    """Acha o caminho do binário (ffmpeg/ffprobe). Prioriza pasta do projeto."""
    import shutil
    # Windows: .exe / Linux/Mac: sem extensão
    candidates = [
        _PROJECT_ROOT / f"{name}.exe",
        _PROJECT_ROOT / name,
    ]
    for c in candidates:
        if c.exists():
            return str(c)
    # Fallback: PATH do sistema
    found = shutil.which(name)
    return found or name  # se não achou, retorna só o nome (vai dar erro depois mas tentou)


def check_ffmpeg_available() -> tuple[bool, str]:
    """Verifica se ffmpeg está disponível. Retorna (ok, info)."""
    import subprocess
    ff = _find_binary("ffmpeg")
    try:
        out = subprocess.check_output(
            [ff, "-version"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        first_line = out.decode("utf-8", errors="ignore").splitlines()[0] if out else ""
        return True, first_line
    except FileNotFoundError:
        return False, "ffmpeg não encontrado (rode instalar_ffmpeg.bat)"
    except Exception as e:
        return False, f"erro: {e}"


def _ffprobe_video(path: str) -> dict:
    """Lê dimensões/duração via ffprobe. Retorna dict ou {} se falhar."""
    import subprocess
    import json
    ffprobe = _find_binary("ffprobe")
    try:
        out = subprocess.check_output(
            [
                ffprobe, "-v", "error",
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height,duration",
                "-of", "json", path,
            ],
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        info = json.loads(out)
        s = (info.get("streams") or [{}])[0]
        return {
            "w": int(s.get("width", 0)),
            "h": int(s.get("height", 0)),
            "duration": int(float(s.get("duration", 0))) if s.get("duration") else 0,
        }
    except Exception:
        return {}


def prepare_video_note(path: str) -> str:
    """Processa um vídeo pra ficar no formato perfeito de videoNote.

    Recorta pro centro quadrado, redimensiona pra 384x384, corta em 60s.
    Retorna caminho do arquivo processado. Se ffmpeg não tiver, retorna o original.
    Cache: se já existe arquivo .vnote.mp4, reusa.
    """
    import subprocess
    from pathlib import Path as _P

    src = _P(path)
    if not src.exists():
        return path

    # Cache
    out = src.parent / f"{src.stem}.vnote.mp4"
    if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
        logger.info("Vídeo bolinha [cache]: %s", out.name)
        return str(out)

    info = _ffprobe_video(str(src))
    # Se já é quadrado pequeno e curto, usa direto (provavelmente gravado no Telegram)
    if info and info["w"] == info["h"] and info["w"] <= 480 and info["duration"] <= 60:
        logger.info("Vídeo já no formato bolinha (%dx%d, %ds)", info["w"], info["h"], info["duration"])
        return str(src)

    # Tenta processar com ffmpeg
    ffmpeg = _find_binary("ffmpeg")
    try:
        logger.info("Processando vídeo bolinha com ffmpeg: %s", src.name)
        subprocess.check_call(
            [
                ffmpeg, "-y",
                "-i", str(src),
                "-vf", "crop=in_h:in_h,scale=384:384",  # quadrado central + 384x384
                "-t", "60",                              # max 60s
                "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                "-c:a", "aac", "-b:a", "64k",
                "-movflags", "+faststart",
                "-pix_fmt", "yuv420p",
                str(out),
            ],
            stderr=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            timeout=120,
        )
        if out.exists() and out.stat().st_size > 0:
            logger.info(
                "Vídeo bolinha processado: %s (%d KB) — agora vai sair pequeno e nativo",
                out.name, out.stat().st_size // 1024,
            )
            return str(out)
    except FileNotFoundError:
        logger.error(
            "❌ ffmpeg NÃO ENCONTRADO! Vídeo bolinha vai sair GRANDE. "
            "Rode instalar_ffmpeg.bat na pasta do projeto."
        )
    except Exception as e:
        logger.warning("Falha processando vídeo bolinha: %s", e)

    return str(src)


async def send_video_note(client, entity, path: str):
    """Envia um vídeo como videoNote (bolinha) com atributos forçados.
    Processa o arquivo se preciso.
    """
    from telethon.tl.types import DocumentAttributeVideo

    final_path = prepare_video_note(path)
    info = _ffprobe_video(final_path)
    duration = info.get("duration", 0) or 0
    size = info.get("w") or 384
    if info.get("w") and info.get("h"):
        size = min(info["w"], info["h"])

    attrs = [
        DocumentAttributeVideo(
            duration=duration,
            w=size,
            h=size,
            round_message=True,
            supports_streaming=True,
        )
    ]
    return await client.send_file(
        entity,
        final_path,
        attributes=attrs,
        video_note=True,
    )


async def _resolve_chat(chat_str: str):
    client = await get_client()
    if chat_str == "me":
        return await client.get_me()
    if chat_str.lstrip("-").isdigit():
        return await client.get_entity(int(chat_str))
    return await client.get_entity(chat_str)


async def _check_lead_can_receive(lead_id: int):
    """Última checagem antes do envio. Retorna (telegram_id, display_name) ou skip.

    Esta é a SEGUNDA camada de proteção (a primeira é no scheduler que filtra
    por status e in_private_group). Aqui re-consultamos o lead pra garantir
    que NADA mudou entre a fila e o envio.

    REGRAS DE EXCLUSÃO (qualquer uma -> SKIP, NÃO ENVIA):
    - lead não existe
    - lead.in_private_group = True (está no grupo PRIVADO/pago)
    - lead.status = EXCLUDED, CONVERTED, BLOCKED
    """
    from datetime import datetime, timedelta
    with SessionLocal() as session:
        lead = session.query(Lead).get(lead_id)
        if not lead:
            return None, SendResult(False, error="Lead não encontrado")
        if lead.in_private_group:
            logger.info("SKIP %s: in_private_group=True (já no grupo privado)", lead.display_name)
            return None, SendResult(False, skipped_reason="já está no grupo privado")
        if lead.status == LeadStatus.EXCLUDED.value:
            logger.info("SKIP %s: status=EXCLUDED", lead.display_name)
            return None, SendResult(False, skipped_reason="status=excluded")
        if lead.status == LeadStatus.BLOCKED.value:
            logger.info("SKIP %s: status=BLOCKED", lead.display_name)
            return None, SendResult(False, skipped_reason="lead bloqueado/opt-out")
        if getattr(lead, "opted_out", False):
            logger.info("SKIP %s: opted_out=True", lead.display_name)
            return None, SendResult(False, skipped_reason="lead pediu opt-out")

        # Bloqueia envio quando lead está em cooldown OU é fresh (mandou DM nas últimas 24h)
        # Pode ser desligado via FORCE_SEND_IGNORE_STAGE=1 (overrride manual)
        if os.getenv("FORCE_SEND_IGNORE_STAGE", "0") != "1":
            try:
                from liga.remarketing_stage import is_eligible_for_dispatch
                eligible, reason = is_eligible_for_dispatch(lead)
                if not eligible:
                    logger.info("SKIP %s: stage não elegível — %s", lead.display_name, reason)
                    return None, SendResult(False, skipped_reason=f"stage: {reason}")
            except Exception:
                logger.debug("[stage] erro checando elegibilidade", exc_info=True)

        # Rate limit: max sends/semana por lead (anti-fadiga + anti-banimento)
        max_per_week = int(os.getenv("MAX_SENDS_PER_LEAD_PER_WEEK", "3"))
        if max_per_week > 0:
            cutoff = datetime.utcnow() - timedelta(days=7)
            recent_sends = (
                session.query(Send)
                .filter(Send.lead_id == lead_id)
                .filter(Send.status == SendStatus.SENT.value)
                .filter(Send.sent_at >= cutoff)
                .count()
            )
            if recent_sends >= max_per_week:
                logger.info(
                    "SKIP %s: rate_limit (%d sends nos últimos 7d, max=%d)",
                    lead.display_name, recent_sends, max_per_week,
                )
                return None, SendResult(
                    False, skipped_reason=f"rate_limit ({recent_sends}/sem)",
                )

        return (lead.telegram_id, lead.display_name), None


async def _wait_pre_send(lead_display: str, apply_rate_limit: bool):
    if apply_rate_limit:
        await _rate_limiter.wait_if_needed()
    delay = random.uniform(_env_int("SEND_DELAY_MIN", 15), _env_int("SEND_DELAY_MAX", 40))
    logger.info("Aguardando %.1fs antes de enviar pra %s", delay, lead_display)
    await asyncio.sleep(delay)


async def _mark_blocked(lead_id: int):
    with SessionLocal() as session:
        ld = session.query(Lead).get(lead_id)
        if ld:
            ld.status = LeadStatus.BLOCKED.value
            session.commit()


# ---------------------------------------------------------------------------
# FORWARD
# ---------------------------------------------------------------------------
async def forward_to_lead(
    lead_id: int,
    sources: List[dict],
    *,
    apply_rate_limit: bool = True,
) -> SendResult:
    """Encaminha mensagens fonte com drop_author=True."""
    if not sources:
        return SendResult(False, error="Nenhuma mensagem fonte configurada")

    info, skip = await _check_lead_can_receive(lead_id)
    if skip:
        return skip
    lead_telegram_id, lead_display = info

    client = await get_client()
    await _wait_pre_send(lead_display, apply_rate_limit)

    try:
        lead_entity = await client.get_entity(lead_telegram_id)
    except Exception as e:
        return SendResult(False, error=f"Não consegui resolver o lead: {e}")

    by_chat = defaultdict(list)
    drop_flags = {}
    for s in sources:
        chat = s["chat"]
        by_chat[chat].append(s["message_id"])
        drop_flags[chat] = s.get("drop_author", True)

    all_message_ids = []
    try:
        for chat, msg_ids in by_chat.items():
            try:
                from_entity = await _resolve_chat(chat)
            except Exception as e:
                return SendResult(False, error=f"Não consegui resolver chat fonte '{chat}': {e}")

            result = await client.forward_messages(
                entity=lead_entity,
                messages=msg_ids,
                from_peer=from_entity,
                drop_author=drop_flags.get(chat, True),
            )
            if isinstance(result, list):
                for m in result:
                    if m is not None:
                        all_message_ids.append(getattr(m, "id", 0))
            else:
                if result is not None:
                    all_message_ids.append(getattr(result, "id", 0))

            if len(by_chat) > 1:
                await asyncio.sleep(random.uniform(0.5, 1.5))

        _rate_limiter.record()
        return SendResult(True, telegram_message_ids=all_message_ids)

    except FloodWaitError as e:
        wait = int(e.seconds) + 5
        logger.warning("FloodWait: %ds", wait)
        await asyncio.sleep(wait)
        return SendResult(False, error=f"FloodWait {e.seconds}s")
    except PeerFloodError:
        return SendResult(False, error="PeerFlood — Telegram detectou spam. PARE por algumas horas.")
    except (UserPrivacyRestrictedError, UserIsBlockedError, ChatWriteForbiddenError) as e:
        await _mark_blocked(lead_id)
        return SendResult(False, error=f"Lead não aceita DM: {type(e).__name__}")
    except Exception as e:
        logger.exception("Erro ao encaminhar")
        return SendResult(False, error=str(e))


# ---------------------------------------------------------------------------
# SEND TEXT (modo AI)
# ---------------------------------------------------------------------------
async def send_text_to_lead(
    lead_id: int,
    text: str,
    *,
    media_paths: Optional[List[dict]] = None,
    apply_rate_limit: bool = True,
) -> SendResult:
    """Envia texto pra um lead. Cada bloco separado por linha em branco
    vira uma MENSAGEM SEPARADA, com pequeno delay entre elas.
    media_paths: lista [{path, caption, send_before_text}] de arquivos opcionais.
    """
    info, skip = await _check_lead_can_receive(lead_id)
    if skip:
        return skip
    lead_telegram_id, lead_display = info

    client = await get_client()

    with SessionLocal() as session:
        lead = session.query(Lead).get(lead_id)
        text_final = _interpolate(text, lead) if lead else text

    blocks = split_into_blocks(text_final)
    if not blocks and not media_paths:
        return SendResult(False, error="Texto vazio e sem mídia")

    await _wait_pre_send(lead_display, apply_rate_limit)

    try:
        entity = await client.get_entity(lead_telegram_id)
    except Exception as e:
        return SendResult(False, error=f"Não consegui resolver o lead: {e}")

    sent_ids = []
    media_paths = media_paths or []
    media_before = [m for m in media_paths if m.get("send_before_text")]
    media_after = [m for m in media_paths if not m.get("send_before_text")]

    try:
        # 1) Mídias que vão ANTES do texto
        for m in media_before:
            await asyncio.sleep(random.uniform(1.0, 2.0))
            if m.get("video_note"):
                msg = await send_video_note(client, entity, m["path"])
            else:
                msg = await client.send_file(
                    entity, m["path"],
                    caption=m.get("caption") or "",
                )
            sent_ids.append(getattr(msg, "id", 0))

        # 2) Blocos de texto (com markdown ativo pra links [texto](url) etc)
        for i, block in enumerate(blocks):
            if i > 0 or media_before:
                await asyncio.sleep(random.uniform(1.5, 3.5))
            try:
                msg = await client.send_message(entity, block, parse_mode="md", link_preview=True)
            except Exception as md_err:
                logger.warning("[sender] markdown falhou — fallback texto puro: %s", str(md_err)[:100])
                msg = await client.send_message(entity, block)
            sent_ids.append(msg.id)

        # 3) Mídias que vão DEPOIS do texto
        for m in media_after:
            await asyncio.sleep(random.uniform(1.5, 3.0))
            if m.get("video_note"):
                msg = await send_video_note(client, entity, m["path"])
            else:
                msg = await client.send_file(
                    entity, m["path"],
                    caption=m.get("caption") or "",
                )
            sent_ids.append(getattr(msg, "id", 0))

        _rate_limiter.record()
        return SendResult(True, telegram_message_ids=sent_ids)

    except FloodWaitError as e:
        wait = int(e.seconds) + 5
        await asyncio.sleep(wait)
        return SendResult(False, error=f"FloodWait {e.seconds}s")
    except PeerFloodError:
        return SendResult(False, error="PeerFlood — pare por algumas horas")
    except (UserPrivacyRestrictedError, UserIsBlockedError, ChatWriteForbiddenError) as e:
        await _mark_blocked(lead_id)
        return SendResult(False, error=f"Lead não aceita DM: {type(e).__name__}")
    except Exception as e:
        logger.exception("Erro ao enviar texto")
        return SendResult(False, error=str(e))


# ---------------------------------------------------------------------------
# TESTE - envia/encaminha pra um username SEM mexer em leads
# ---------------------------------------------------------------------------
async def send_test_to_username(
    username: str,
    script_id: int,
    *,
    variant_id: Optional[int] = None,
) -> SendResult:
    """Envia 1 execução do script pro username (modo teste).

    - Bypassa filtro de leads
    - Bypassa rate limiter (só 2s de delay natural)
    - Em forward mode: encaminha as sources com drop_author
    - Em ai mode: envia o texto da variante ativa (ou variant_id específico)
    """
    from db.models import Script, ScriptVariant

    if not username:
        return SendResult(False, error="Username de teste não configurado")
    username = username.strip()
    if not username.startswith("@") and not username.lstrip("-").isdigit():
        username = "@" + username

    client = await get_client()
    try:
        entity = await client.get_entity(username)
    except Exception as e:
        return SendResult(False, error=f"Não consegui resolver {username}: {e}")

    with SessionLocal() as session:
        script = session.query(Script).get(script_id)
        if not script:
            return SendResult(False, error="Script não encontrado")
        mode = (script.mode or "forward").lower()

        sources = []
        text = None
        media_list = []
        if mode == "forward":
            for s in script.sources:
                sources.append({
                    "chat": s.source_chat,
                    "message_id": s.source_message_id,
                    "drop_author": bool(s.drop_author),
                })
        else:  # ai
            if variant_id:
                v = session.query(ScriptVariant).get(variant_id)
                if not v or v.script_id != script_id:
                    return SendResult(False, error="Variante inválida")
                text = v.text_es
            else:
                actives = [v for v in script.variants if v.is_active]
                if not actives:
                    return SendResult(False, error="Nenhuma variante ativa no script")
                text = actives[0].text_es
            # Carrega mídias do script
            from pathlib import Path as _P
            media_dir = _P(__file__).resolve().parent.parent / "media"
            for m in script.media_items:
                media_list.append({
                    "path": str(media_dir / m.filename),
                    "caption": m.caption or "",
                    "send_before_text": bool(m.send_before_text),
                    "video_note": bool(getattr(m, "video_note", False)),
                })

    # Mini delay pra não parecer robótico
    await asyncio.sleep(2)

    try:
        if mode == "forward":
            if not sources:
                return SendResult(False, error="Script sem mensagens fonte")

            by_chat = defaultdict(list)
            drop_flags = {}
            for s in sources:
                by_chat[s["chat"]].append(s["message_id"])
                drop_flags[s["chat"]] = s["drop_author"]

            all_ids = []
            for chat, msg_ids in by_chat.items():
                from_entity = await _resolve_chat(chat)
                result = await client.forward_messages(
                    entity=entity, messages=msg_ids,
                    from_peer=from_entity,
                    drop_author=drop_flags.get(chat, True),
                )
                if isinstance(result, list):
                    for m in result:
                        if m is not None:
                            all_ids.append(getattr(m, "id", 0))
                else:
                    if result is not None:
                        all_ids.append(getattr(result, "id", 0))
                if len(by_chat) > 1:
                    await asyncio.sleep(0.8)
            return SendResult(True, telegram_message_ids=all_ids)
        else:
            # AI / texto direto: interpola [nombre] usando o nome da conta de teste
            target_first_name = getattr(entity, "first_name", "") or ""
            target_username = getattr(entity, "username", "") or ""
            text_interpolated = _interpolate_text(text or "", target_first_name, target_username)

            blocks = split_into_blocks(text_interpolated)
            if not blocks and not media_list:
                return SendResult(False, error="Texto da variante vazio e sem mídia")
            sent_ids = []
            media_before = [m for m in media_list if m.get("send_before_text")]
            media_after = [m for m in media_list if not m.get("send_before_text")]

            # Mídias antes (interpola legenda também)
            for m in media_before:
                cap = _interpolate_text(m.get("caption") or "", target_first_name, target_username)
                await asyncio.sleep(random.uniform(1.0, 2.0))
                if m.get("video_note"):
                    msg = await send_video_note(client, entity, m["path"])
                else:
                    msg = await client.send_file(entity, m["path"], caption=cap)
                sent_ids.append(getattr(msg, "id", 0))

            for i, block in enumerate(blocks):
                if i > 0 or media_before:
                    await asyncio.sleep(random.uniform(1.5, 3.5))
                try:
                    msg = await client.send_message(entity, block, parse_mode="md", link_preview=True)
                except Exception as md_err:
                    logger.warning("[sender testes] markdown falhou: %s", str(md_err)[:100])
                    msg = await client.send_message(entity, block)
                sent_ids.append(msg.id)

            # Mídias depois (interpola legenda)
            for m in media_after:
                cap = _interpolate_text(m.get("caption") or "", target_first_name, target_username)
                await asyncio.sleep(random.uniform(1.5, 3.0))
                if m.get("video_note"):
                    msg = await send_video_note(client, entity, m["path"])
                else:
                    msg = await client.send_file(entity, m["path"], caption=cap)
                sent_ids.append(getattr(msg, "id", 0))

            return SendResult(True, telegram_message_ids=sent_ids)

    except FloodWaitError as e:
        return SendResult(False, error=f"FloodWait {e.seconds}s")
    except Exception as e:
        logger.exception("Erro no teste")
        return SendResult(False, error=str(e))


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------
# Lock global por send_id pra evitar race entre run_campaign + rescue + process-queue
# despachando o MESMO send 2× (lead recebe duplicata). Como tudo roda num único
# processo asyncio, asyncio.Lock é suficiente.
import asyncio as _asyncio_mod
_SEND_LOCKS: dict[int, _asyncio_mod.Lock] = {}
_SEND_LOCKS_GUARD = _asyncio_mod.Lock()


async def _get_send_lock(send_id: int) -> _asyncio_mod.Lock:
    async with _SEND_LOCKS_GUARD:
        lock = _SEND_LOCKS.get(send_id)
        if lock is None:
            lock = _asyncio_mod.Lock()
            _SEND_LOCKS[send_id] = lock
        return lock


async def execute_send_record(send_id: int) -> SendResult:
    """Executa um Send QUEUED dispatching pelo modo do script.

    Idempotente: protegido por lock por send_id. Se 2 callers tentam executar
    o mesmo send (run_campaign + rescue + process-queue), um espera o outro,
    e quando entra, vê status != QUEUED e retorna skipped sem duplicar envio.
    """
    lock = await _get_send_lock(send_id)
    async with lock:
        try:
            return await _execute_send_record_locked(send_id)
        finally:
            # Limpa lock se send chegou em estado terminal (não vai precisar mais)
            try:
                with SessionLocal() as s:
                    sd = s.query(Send).get(send_id)
                    if sd and sd.status in (
                        SendStatus.SENT.value, SendStatus.FAILED.value,
                        SendStatus.SKIPPED.value,
                    ):
                        async with _SEND_LOCKS_GUARD:
                            _SEND_LOCKS.pop(send_id, None)
            except Exception:
                pass


async def _execute_send_record_locked(send_id: int) -> SendResult:
    with SessionLocal() as session:
        send = session.query(Send).get(send_id)
        if not send:
            return SendResult(False, error="Send não encontrado")
        if send.status != SendStatus.QUEUED.value:
            return SendResult(False, skipped_reason=f"status={send.status}")
        lead_id = send.lead_id
        script = send.script
        if script is None:
            return SendResult(False, error="Script não encontrado")
        mode = (script.mode or "forward").lower()

        sources = []
        text_to_send = None
        media_list = []
        if mode == "forward":
            for s in script.sources:
                sources.append({
                    "chat": s.source_chat,
                    "message_id": s.source_message_id,
                    "drop_author": bool(s.drop_author),
                })
        else:  # ai
            variant = send.variant
            if variant is None:
                return SendResult(False, error="Send sem variante associada (modo ai)")
            text_to_send = variant.text_es
            from pathlib import Path as _P
            media_dir = _P(__file__).resolve().parent.parent / "media"
            for m in script.media_items:
                media_list.append({
                    "path": str(media_dir / m.filename),
                    "caption": m.caption or "",
                    "send_before_text": bool(m.send_before_text),
                    "video_note": bool(getattr(m, "video_note", False)),
                })

    if mode == "forward":
        if not sources:
            with SessionLocal() as session:
                sd = session.query(Send).get(send_id)
                if sd:
                    sd.status = SendStatus.FAILED.value
                    sd.error = "Script sem mensagens fonte"
                    session.commit()
            return SendResult(False, error="Script sem mensagens fonte")
        result = await forward_to_lead(lead_id, sources)
    else:
        if not text_to_send and not media_list:
            return SendResult(False, error="Variante sem texto e sem mídia")
        result = await send_text_to_lead(lead_id, text_to_send or "", media_paths=media_list)

    with SessionLocal() as session:
        send = session.query(Send).get(send_id)
        if not send:
            return result
        if result.success:
            send.status = SendStatus.SENT.value
            send.sent_at = datetime.utcnow()
            send.telegram_message_ids = ",".join(str(m) for m in result.telegram_message_ids)
            if mode == "ai" and text_to_send:
                send.message_text = text_to_send
            lead = session.query(Lead).get(lead_id)
            if lead and lead.status == LeadStatus.PENDING.value:
                lead.status = LeadStatus.CONTACTED.value
            # Avança o remarketing_stage do lead (untouched→R1, R1_cold→R2, R2_cold→R3)
            try:
                from liga.remarketing_stage import advance_stage_on_send
                if lead:
                    advance_stage_on_send(lead, session)
            except Exception:
                logger.debug("[stage] erro avançando stage", exc_info=True)
            from db.models import Script, ScriptVariant
            sc = session.query(Script).get(send.script_id)
            if sc:
                sc.sends_count = (sc.sends_count or 0) + 1
            if send.variant_id:
                sv = session.query(ScriptVariant).get(send.variant_id)
                if sv:
                    sv.sends_count = (sv.sends_count or 0) + 1
        elif result.skipped_reason:
            send.status = SendStatus.SKIPPED.value
            send.error = result.skipped_reason
        else:
            send.status = SendStatus.FAILED.value
            send.error = result.error
        session.commit()

    return result