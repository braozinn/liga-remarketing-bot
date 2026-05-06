"""Detecção de leads.

Estratégias COMBINADAS pra excluir quem já está no grupo privado:
1. iter_participants do grupo (limitado a ~200 pelo Telegram em grupos grandes)
2. Scan de DMs procurando o LINK DE CONVITE que VOCÊ (suporte) enviou
   → quem recebeu o link de você é considerado já convertido/excluído

Os 2 conjuntos são unidos pra cobertura máxima.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import re
from datetime import datetime
from typing import Optional, Set

from telethon.errors import FloodWaitError
from telethon.tl.types import User

from db import SessionLocal
from db.models import Lead, LeadStatus

from .client import get_client, get_private_group_entity

PARTNER_BOT_USERNAME = "@QuotexPartnerBot"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Extração de ID da conta na plataforma a partir do histórico de DMs
# ---------------------------------------------------------------------------
# Padrões em ordem de confiança (1º match ganha): contexto explícito > número solto
_ID_CONTEXT_PATTERNS = [
    # "ID: 12345678" / "id 12345678" / "#12345678"
    re.compile(r"(?:\bID\s*[:\-]?\s*|#\s*)(\d{7,9})\b", re.IGNORECASE),
    # "cuenta: 12345678" / "conta 12345678" / "account #12345678"
    re.compile(r"\b(?:cuenta|conta|account)\s*[:\-#]?\s*(\d{7,9})\b", re.IGNORECASE),
    # "mi cuenta es 12345678" / "meu id e 12345678" / "my id is 12345678"
    re.compile(
        r"\b(?:mi|m[ií]|meu|my)\s+(?:ID|id|cuenta|conta|account)\s+"
        r"(?:es|é|e|is|=|:)?\s*(\d{7,9})\b",
        re.IGNORECASE,
    ),
]
# Match exato: a mensagem inteira é só um número de 7-9 dígitos (alta confiança)
_ID_STANDALONE = re.compile(r"^\s*(\d{7,9})\s*$")


def _looks_like_phone(num: str) -> bool:
    """Heurística pra evitar capturar telefones como ID."""
    # Telefone latam costuma ter 10-12 dígitos; ID costuma ser 7-9
    if len(num) >= 10:
        return True
    return False


# Faixa válida de ID Quotex/QXBroker. Ajuste se a plataforma mudar.
ID_MIN_DIGITS = 7
ID_MAX_DIGITS = 9


def _looks_like_valid_id(num) -> bool:
    """True se 'num' parece um ID válido de conta (apenas dígitos, 7-9 chars)."""
    if num is None:
        return False
    s = str(num).strip()
    if not s.isdigit():
        return False
    return ID_MIN_DIGITS <= len(s) <= ID_MAX_DIGITS


async def find_recent_account_id_in_dms(
    client, user_id: int,
    max_messages: int = 30,
    scan_images: bool = True,
    max_images: int = 2,
) -> dict:
    """Varre as últimas N msgs em DM com esse user procurando o ID da conta.

    1º: tenta achar em texto (regex de padrões + número solto)
    2º: se nada e scan_images=True, baixa as últimas K imagens e usa Claude Vision

    Retorna dict:
      - id: str | None
      - date: datetime | None
      - source: "text" | "image" | None
    """
    out = {"id": None, "date": None, "source": None}

    # ----- 1) Text scan -----
    image_msgs: list = []
    try:
        async for msg in client.iter_messages(user_id, limit=max_messages):
            text = (msg.message or "").strip()
            if text:
                # 1a) Mensagem inteira = só o número
                m = _ID_STANDALONE.match(text)
                if m and _looks_like_valid_id(m.group(1)):
                    return {"id": m.group(1), "date": msg.date, "source": "text"}
                # 1b) Padrões com contexto
                for pat in _ID_CONTEXT_PATTERNS:
                    m = pat.search(text)
                    if m and _looks_like_valid_id(m.group(1)):
                        return {"id": m.group(1), "date": msg.date, "source": "text"}
            # Coleta imagens pra fallback (do mais novo pro mais antigo)
            if scan_images and len(image_msgs) < max_images:
                if getattr(msg, "photo", None):
                    image_msgs.append(msg)
                else:
                    doc = getattr(msg, "document", None)
                    if doc and (getattr(doc, "mime_type", "") or "").startswith("image/"):
                        image_msgs.append(msg)
    except FloodWaitError as e:
        logger.warning("[scan_id] FloodWait %ds em user_id=%s", e.seconds, user_id)
        return out
    except Exception:
        logger.debug("[scan_id] erro varrendo DMs com user_id=%s", user_id, exc_info=True)
        return out

    # ----- 2) Image scan (Claude Vision) -----
    if scan_images and image_msgs:
        # Lazy import pra não quebrar quando ANTHROPIC_API_KEY ausente
        try:
            from ai.providers import analyze_account_screenshot
        except Exception:
            return out

        for msg in image_msgs:
            try:
                buf = io.BytesIO()
                await msg.download_media(buf)
                img_bytes = buf.getvalue()
                if not img_bytes:
                    continue
                result = analyze_account_screenshot(img_bytes)
                if result.get("valido") and result.get("id_conta"):
                    cand = str(result["id_conta"])
                    if _looks_like_valid_id(cand):
                        return {"id": cand, "date": msg.date, "source": "image"}
                    else:
                        # Vision retornou algo, mas tamanho fora do range — ignora
                        # e segue procurando. Lead vai pra needs_review.
                        logger.info(
                            "[scan_id] vision retornou id suspeito '%s' (tamanho fora 7-9) — ignorando",
                            cand,
                        )
            except FloodWaitError as e:
                logger.warning("[scan_id] FloodWait %ds (imagem) user_id=%s", e.seconds, user_id)
                await asyncio.sleep(min(e.seconds, 30))
            except Exception:
                logger.debug("[scan_id] erro processando imagem", exc_info=True)
    return out


# ---------------------------------------------------------------------------
# Validação via @QuotexPartnerBot
# ---------------------------------------------------------------------------
_PARTNER_VALID_RE   = re.compile(r"Trader\s*#\s*(\d+)", re.IGNORECASE)
_PARTNER_INVALID_RE = re.compile(r"Trader with ID\s*=\s*'(\d+)'\s*was\s+not\s+found", re.IGNORECASE)
_PARTNER_COUNTRY_RE = re.compile(r"Country:\s*([^\n\r]+)", re.IGNORECASE)
_PARTNER_BALANCE_RE = re.compile(r"Balance:\s*\$?\s*([\d.,]+)", re.IGNORECASE)
_PARTNER_DEPOSITS_RE= re.compile(r"Deposits Sum:\s*\$?\s*([\d.,]+)", re.IGNORECASE)
_PARTNER_TURNOVER_RE= re.compile(r"Turnover All:\s*\$?\s*([\d.,]+)", re.IGNORECASE)


def _parse_partner_response(text: str) -> dict:
    """Parseia resposta do @QuotexPartnerBot."""
    if not text:
        return {"status": "error", "reason": "empty", "raw": ""}

    m = _PARTNER_INVALID_RE.search(text)
    if m:
        return {"status": "invalid", "queried_id": m.group(1), "raw": text}

    m = _PARTNER_VALID_RE.search(text)
    if m:
        def _f(rgx):
            mm = rgx.search(text)
            if not mm: return None
            try: return float(mm.group(1).replace(",", ""))
            except Exception: return None

        country_m = _PARTNER_COUNTRY_RE.search(text)
        return {
            "status": "validated",
            "trader_id": m.group(1),
            "country": (country_m.group(1).strip() if country_m else None),
            "balance": _f(_PARTNER_BALANCE_RE),
            "deposits_sum": _f(_PARTNER_DEPOSITS_RE),
            "turnover": _f(_PARTNER_TURNOVER_RE),
            "raw": text,
        }

    return {"status": "error", "reason": "unrecognized_response", "raw": text}


async def validate_id_via_partner_bot(client, id_str: str, timeout: int = 15) -> dict:
    """Envia o ID pro @QuotexPartnerBot e parseia a resposta.

    Retorna dict com 'status' ∈ {'validated','invalid','error'} e dados extraídos.

    NÃO envia se o ID falhar nos critérios básicos (apenas dígitos, 7-9 chars).
    Isso evita queimar requests pro partner bot com lixo tipo '4834' que o
    Claude Vision às vezes retorna quando lê números errados de uma imagem.
    """
    if not id_str or not str(id_str).strip().isdigit():
        return {"status": "error", "reason": "invalid_id_format", "raw": ""}

    if not _looks_like_valid_id(id_str):
        logger.warning(
            "[partner_bot] recusando ID fora da faixa %d-%d dígitos: %r",
            ID_MIN_DIGITS, ID_MAX_DIGITS, id_str,
        )
        return {
            "status": "error",
            "reason": f"id_length_out_of_range_{ID_MIN_DIGITS}_{ID_MAX_DIGITS}",
            "raw": "",
        }

    try:
        async with client.conversation(PARTNER_BOT_USERNAME, timeout=timeout) as conv:
            await conv.send_message(str(id_str).strip())
            response = await conv.get_response()
            text = (response.message or "").strip()
            return _parse_partner_response(text)
    except asyncio.TimeoutError:
        return {"status": "error", "reason": "timeout", "raw": ""}
    except FloodWaitError as e:
        logger.warning("[partner_bot] FloodWait %ds", e.seconds)
        return {"status": "error", "reason": f"flood_wait_{e.seconds}s", "raw": ""}
    except Exception as e:
        logger.warning("[partner_bot] erro: %s", e)
        return {"status": "error", "reason": str(e)[:100], "raw": ""}


# ---------------------------------------------------------------------------
# Membros do grupo privado (limitado pelo Telegram)
# ---------------------------------------------------------------------------
async def get_private_group_member_ids() -> Set[int]:
    """Pega os IDs visíveis de membros do grupo privado.

    Limitação conhecida do Telegram: grupos grandes (>200) só listam ~200
    membros mais "recentes" mesmo pra admin. Pra cobertura completa, use a
    detecção via link de convite (get_users_who_received_invite_link).
    """
    client = await get_client()
    try:
        group = await get_private_group_entity()
    except Exception as e:
        logger.error("Não consegui resolver PRIVATE_GROUP: %s", e)
        return set()
    ids: Set[int] = set()
    try:
        async for member in client.iter_participants(group, aggressive=True):
            ids.add(member.id)
    except Exception as e:
        logger.error("Erro ao iterar participantes: %s", e)
    logger.info("[grupo privado] %d membros visíveis via API", len(ids))
    return ids


# ---------------------------------------------------------------------------
# Detecção via LINK DE CONVITE enviado em DM (a saída pro limite de 200)
# ---------------------------------------------------------------------------
def _extract_invite_token(invite_link: str) -> str:
    """Extrai a parte única do link (após t.me/+)."""
    if not invite_link:
        return ""
    m = re.search(r"t\.me/(?:\+|joinchat/)?([A-Za-z0-9_-]+)", invite_link)
    if m:
        return m.group(1)
    return invite_link.rsplit("/", 1)[-1].lstrip("+")


async def get_users_who_received_invite_link() -> Set[int]:
    """Escaneia DMs procurando msgs onde EU (suporte) enviei o link do grupo.
    Retorna os IDs dos usuários que receberam o link.

    Usa Telegram-side search (iter_messages com search=token) pra ser rápido,
    mas com fallback iterando últimas 200 msgs caso a busca não pegue tudo.
    """
    invite_link = os.getenv("PRIVATE_GROUP_INVITE_LINK", "").strip()
    if not invite_link:
        return set()

    token = _extract_invite_token(invite_link)
    if not token or len(token) < 5:
        logger.warning("Token inválido extraído de PRIVATE_GROUP_INVITE_LINK: '%s'", token)
        return set()

    client = await get_client()
    me = await client.get_me()
    received_ids: Set[int] = set()

    dialog_count = 0
    found_count = 0
    logger.info("[link] Escaneando DMs procurando '%s'...", token)

    async for dialog in client.iter_dialogs():
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if not isinstance(entity, User):
            continue
        if entity.bot or entity.is_self:
            continue
        dialog_count += 1

        try:
            # Busca server-side por mensagens contendo o token
            async for msg in client.iter_messages(entity, search=token, limit=10):
                if not msg or not msg.message:
                    continue
                # Confirma que a mensagem foi enviada por mim (suporte)
                from_id = msg.from_id
                sender_user_id = getattr(from_id, "user_id", None) if from_id else None
                # Se from_id None, msg de DM enviada por mim conta como out=True
                if msg.out or sender_user_id == me.id:
                    if token in msg.message or invite_link in msg.message:
                        received_ids.add(entity.id)
                        found_count += 1
                        break
        except FloodWaitError as e:
            wait = int(e.seconds) + 5
            logger.warning("[link] FloodWait %ds, esperando...", wait)
            await asyncio.sleep(wait)
        except Exception as e:
            logger.debug("[link] erro DM %s: %s", entity.id, e)

        # Pequeno delay anti-rate-limit
        await asyncio.sleep(0.03)

        if dialog_count % 50 == 0:
            logger.info("[link] progresso: %d DMs escaneados, %d receberam o link", dialog_count, found_count)

    logger.info("[link] FINAL: %d DMs escaneados, %d pessoas receberam o link", dialog_count, found_count)
    return received_ids


async def get_full_excluded_set() -> Set[int]:
    """Combina membros visíveis do grupo + quem recebeu o link.
    É o conjunto COMPLETO de pessoas que NÃO devem receber remarketing.
    """
    members = await get_private_group_member_ids()
    link_recipients = await get_users_who_received_invite_link()
    combined = members | link_recipients
    logger.info(
        "[exclusão] %d via grupo + %d via link = %d total únicos",
        len(members), len(link_recipients), len(combined),
    )
    return combined


# ---------------------------------------------------------------------------
# (legado) grupo VIP de leads — só usado se LEADS_SOURCE_GROUP setado
# ---------------------------------------------------------------------------
async def get_leads_group_entity():
    target = os.getenv("LEADS_SOURCE_GROUP", "").strip()
    if not target:
        return None
    client = await get_client()
    if target.lstrip("-").isdigit():
        return await client.get_entity(int(target))
    return await client.get_entity(target)


async def iter_leads_group_users():
    client = await get_client()
    group = await get_leads_group_entity()
    if group is None:
        return
    async for member in client.iter_participants(group, aggressive=True):
        if not isinstance(member, User):
            continue
        if member.bot or getattr(member, "is_self", False):
            continue
        yield member


async def iter_dm_users():
    """Itera usuários com quem você teve DM."""
    client = await get_client()
    async for dialog in client.iter_dialogs():
        if not dialog.is_user:
            continue
        entity = dialog.entity
        if not isinstance(entity, User):
            continue
        if entity.bot or entity.is_self:
            continue
        yield entity, dialog.date


# ---------------------------------------------------------------------------
# Sync principal: DM history + exclusão combinada (grupo + link)
# ---------------------------------------------------------------------------
async def sync_leads_from_dm_history(
    *,
    exclude_group_members: bool = True,
    scan_for_account_ids: bool = True,
) -> dict:
    """Sync leads varrendo DMs + cruzando com membros do grupo + recipients do link.

    Se scan_for_account_ids=True (default), também varre as últimas mensagens
    de cada DM procurando o ID da conta na plataforma e salva em
    `lead.liga_account_id` (apenas se ainda não houver um registrado).
    """
    excluded_ids: Set[int] = set()
    excluded_via_group = 0
    excluded_via_link = 0
    if exclude_group_members:
        try:
            members = await get_private_group_member_ids()
            excluded_via_group = len(members)
            excluded_ids |= members
        except Exception as e:
            logger.error("Falha listando grupo: %s", e)
        try:
            link_set = await get_users_who_received_invite_link()
            excluded_via_link = len(link_set)
            excluded_ids |= link_set
        except Exception as e:
            logger.error("Falha scan link: %s", e)

    client = await get_client() if scan_for_account_ids else None

    new_count = updated_count = excluded_count = total = 0
    ids_found = ids_validated = ids_invalid = ids_needs_review = 0
    # Cap pra evitar abuso do partner bot por sync
    max_partner_validations = int(os.getenv("MAX_PARTNER_VALIDATIONS_PER_SYNC", "100"))
    partner_validations_done = 0
    session = SessionLocal()
    try:
        async for user, last_msg_date in iter_dm_users():
            total += 1
            in_excluded = user.id in excluded_ids

            lead = session.query(Lead).filter_by(telegram_id=user.id).one_or_none()
            if lead is None:
                lead = Lead(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    phone=user.phone,
                    last_dm_at=last_msg_date.replace(tzinfo=None) if last_msg_date else None,
                    in_private_group=in_excluded,
                    status=LeadStatus.EXCLUDED.value if in_excluded else LeadStatus.PENDING.value,
                    source="dm_history",
                )
                session.add(lead)
                session.flush()  # garante lead.id pra logs
                new_count += 1
            else:
                lead.username = user.username
                lead.first_name = user.first_name
                lead.last_name = user.last_name
                lead.phone = user.phone or lead.phone
                if last_msg_date:
                    lead.last_dm_at = last_msg_date.replace(tzinfo=None)
                lead.in_private_group = in_excluded
                if in_excluded and lead.status != LeadStatus.EXCLUDED.value:
                    lead.status = LeadStatus.EXCLUDED.value
                lead.updated_at = datetime.utcnow()
                updated_count += 1
            if in_excluded:
                excluded_count += 1

            # Extração + validação de ID — só se ainda não tem status definido
            # e o lead não está no grupo privado (não precisa mais qualificar).
            if (
                scan_for_account_ids
                and client is not None
                and not in_excluded
                and lead.liga_id_status not in ("validated", "invalid")
                and not (lead.liga_account_id or "").strip()
            ):
                try:
                    cand = await find_recent_account_id_in_dms(
                        client, user.id, max_messages=30,
                        scan_images=True, max_images=2,
                    )
                    cand_id_raw = (cand.get("id") or "")[:100]

                    # Se extraiu algo mas tamanho não bate → needs_review e NÃO envia ao partner bot
                    if cand_id_raw and not _looks_like_valid_id(cand_id_raw):
                        logger.warning(
                            "[scan_id] lead=%s candidato fora da faixa: %r — needs_review",
                            lead.display_name, cand_id_raw,
                        )
                        lead.liga_id_partner_response = (
                            f"[bot] candidato '{cand_id_raw}' rejeitado "
                            f"(fora de {ID_MIN_DIGITS}-{ID_MAX_DIGITS} dígitos)"
                        )
                        lead.liga_id_status = "needs_review"
                        ids_needs_review += 1
                    elif cand_id_raw:
                        cand_id = cand_id_raw
                        lead.liga_account_id = cand_id
                        ids_found += 1
                        logger.info(
                            "[scan_id] lead=%s id=%s (via %s, msg de %s)",
                            lead.display_name, cand_id, cand.get("source"),
                            cand["date"].strftime("%Y-%m-%d") if cand.get("date") else "?",
                        )

                        # Valida via @QuotexPartnerBot (se ainda não atingiu cap)
                        if partner_validations_done < max_partner_validations:
                            partner_validations_done += 1
                            val = await validate_id_via_partner_bot(client, cand_id)
                            lead.liga_id_partner_response = (val.get("raw") or "")[:4000]
                            if val.get("status") == "validated":
                                lead.liga_id_status = "validated"
                                lead.liga_id_country = (val.get("country") or "")[:50]
                                lead.liga_id_balance = val.get("balance")
                                lead.liga_id_deposits_sum = val.get("deposits_sum")
                                lead.liga_id_turnover = val.get("turnover")
                                lead.liga_id_validated_at = datetime.utcnow()
                                ids_validated += 1
                                logger.info(
                                    "[partner_bot] OK lead=%s id=%s país=%s saldo=%s",
                                    lead.display_name, cand_id,
                                    val.get("country"), val.get("balance"),
                                )
                            elif val.get("status") == "invalid":
                                lead.liga_id_status = "invalid"
                                ids_invalid += 1
                                logger.warning(
                                    "[partner_bot] INVÁLIDO lead=%s id=%s",
                                    lead.display_name, cand_id,
                                )
                            else:
                                lead.liga_id_status = "extracted"
                                logger.warning(
                                    "[partner_bot] ERRO lead=%s id=%s reason=%s",
                                    lead.display_name, cand_id, val.get("reason"),
                                )
                            # Pequeno delay pra ser educado com o bot
                            await asyncio.sleep(0.8)
                        else:
                            lead.liga_id_status = "extracted"  # cap atingido
                    else:
                        lead.liga_id_status = "needs_review"
                        ids_needs_review += 1
                except FloodWaitError as e:
                    logger.warning("[scan_id] FloodWait %ds — pulando", e.seconds)
                    await asyncio.sleep(min(e.seconds, 30))
                except Exception:
                    logger.debug("[scan_id] erro silencioso", exc_info=True)

        # ALÉM DOS DMs: cria/atualiza leads pra IDs do grupo/link que NÃO
        # apareceram em DMs (ex: pessoas adicionadas direto no grupo).
        # Esses ficam como EXCLUDED pra não serem contactados se aparecerem depois.
        for tid in excluded_ids:
            lead = session.query(Lead).filter_by(telegram_id=tid).one_or_none()
            if lead is None:
                session.add(Lead(
                    telegram_id=tid,
                    in_private_group=True,
                    status=LeadStatus.EXCLUDED.value,
                    source="private_group_member",
                ))

        session.commit()
    finally:
        session.close()

    result = {
        "fonte": "dm_history + link",
        "total_scanned": total,
        "new_leads": new_count,
        "updated_leads": updated_count,
        "excluded_already_in_private_group": excluded_count,
        "excluded_via_group_listing": excluded_via_group,
        "excluded_via_invite_link": excluded_via_link,
        "account_ids_extracted": ids_found,
        "ids_validated": ids_validated,
        "ids_invalid": ids_invalid,
        "ids_needs_review": ids_needs_review,
        "synced_at": datetime.utcnow().isoformat(),
    }
    logger.info("Sync DM: %s", result)
    return result


async def sync_leads_from_group(*, exclude_group_members: bool = True) -> dict:
    """Sync via LEADS_SOURCE_GROUP (grupo VIP). Só roda se setado no .env."""
    if not os.getenv("LEADS_SOURCE_GROUP", "").strip():
        return await sync_leads_from_dm_history(exclude_group_members=exclude_group_members)

    excluded_ids: Set[int] = set()
    if exclude_group_members:
        try:
            excluded_ids |= await get_private_group_member_ids()
        except Exception as e:
            logger.error("Falha listando grupo: %s", e)
        try:
            excluded_ids |= await get_users_who_received_invite_link()
        except Exception as e:
            logger.error("Falha scan link: %s", e)

    new_count = updated_count = excluded_count = total = 0
    session = SessionLocal()
    try:
        async for user in iter_leads_group_users():
            total += 1
            in_private = user.id in excluded_ids

            lead = session.query(Lead).filter_by(telegram_id=user.id).one_or_none()
            if lead is None:
                lead = Lead(
                    telegram_id=user.id,
                    username=user.username,
                    first_name=user.first_name,
                    last_name=user.last_name,
                    phone=user.phone,
                    in_private_group=in_private,
                    in_leads_group=True,
                    status=LeadStatus.EXCLUDED.value if in_private else LeadStatus.PENDING.value,
                    source="leads_group",
                )
                session.add(lead)
                new_count += 1
            else:
                lead.username = user.username
                lead.first_name = user.first_name
                lead.last_name = user.last_name
                lead.phone = user.phone or lead.phone
                lead.in_private_group = in_private
                lead.in_leads_group = True
                if in_private and lead.status != LeadStatus.EXCLUDED.value:
                    lead.status = LeadStatus.EXCLUDED.value
                lead.updated_at = datetime.utcnow()
                updated_count += 1
            if in_private:
                excluded_count += 1
        session.commit()
    finally:
        session.close()

    return {
        "fonte": "leads_group + link",
        "total_scanned": total,
        "new_leads": new_count,
        "updated_leads": updated_count,
        "excluded_already_in_private_group": excluded_count,
        "synced_at": datetime.utcnow().isoformat(),
    }


async def sync_leads_auto(**kwargs) -> dict:
    if os.getenv("LEADS_SOURCE_GROUP", "").strip():
        return await sync_leads_from_group(**kwargs)
    return await sync_leads_from_dm_history(**kwargs)
