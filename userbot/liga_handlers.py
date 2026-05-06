"""Handlers da Liga — uma função async por estado da máquina.

Cada handler recebe (event, lead, session, client) e:
- Lê a mensagem (texto ou imagem)
- Chama Claude Vision se for imagem
- Atualiza o estado/saldo/métricas do lead
- Responde no DM

Não comita a sessão — quem chama (tracker._on_dm) cuida disso.
"""
from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from db.models import DailyVolume, Lead, LigaState, OperationProof
from ai.providers import analyze_account_screenshot, analyze_proof_image

logger = logging.getLogger(__name__)

BUENOS_AIRES_TZ = ZoneInfo("America/Argentina/Buenos_Aires") if ZoneInfo else None

# Pasta onde salvamos as imagens dos comprovantes pra revisão manual
ROOT = Path(__file__).resolve().parent.parent
PROOFS_DIR = ROOT / "media" / "proofs"
PROOFS_DIR.mkdir(parents=True, exist_ok=True)


def _save_proof_image(image_bytes: bytes) -> str | None:
    """Salva o png/jpg em media/proofs/<uuid>.ext e retorna o path relativo.

    Retorno fica em image_path do OperationProof — usado no painel pra mostrar
    a imagem na fila de revisão manual.
    """
    if not image_bytes:
        return None
    # Detecta extensão pelos magic bytes
    if image_bytes.startswith(b"\x89PNG"):
        ext = "png"
    elif image_bytes.startswith(b"\xFF\xD8"):
        ext = "jpg"
    elif image_bytes.startswith(b"GIF8"):
        ext = "gif"
    elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        ext = "webp"
    else:
        ext = "bin"
    name = f"{uuid.uuid4().hex}.{ext}"
    try:
        (PROOFS_DIR / name).write_bytes(image_bytes)
        # path relativo ao /media (que já é montado pelo FastAPI)
        return f"proofs/{name}"
    except Exception:
        logger.exception("[liga] falha ao salvar imagem do comprovante")
        return None

# Mensagens em ES rioplatense (voseo) — informal, humano, natural.
# Convenção Python: Mon=0 ... Sun=6.
_RELATIONSHIP_QUESTIONS = {
    0: "¿Qué par operaste más hoy?",                              # Lunes
    1: "¿Cuántas operaciones hiciste hoy?",                       # Martes
    2: "¿Usás algún indicador como referencia?",                  # Miércoles
    3: "¿Cuál fue el mayor desafío de hoy?",                      # Jueves
    4: "¿Cuál es tu meta de volumen para la semana que viene?",   # Viernes
    5: "¿Operás los fines de semana?",                            # Sábado
    6: "¿Cómo te fue la semana en general?",                      # Domingo
}


def _now_ba() -> datetime:
    if BUENOS_AIRES_TZ:
        return datetime.now(BUENOS_AIRES_TZ)
    return datetime.utcnow()


def _today_ba_str() -> str:
    return _now_ba().strftime("%Y-%m-%d")


def _relationship_question() -> str:
    weekday = _now_ba().weekday()
    return _RELATIONSHIP_QUESTIONS.get(weekday, "¿Cómo te fue operando hoy?")


async def _download_image_bytes(event) -> bytes | None:
    """Baixa a imagem da mensagem (se houver) em memória."""
    try:
        msg = event.message
        if not msg or not getattr(msg, "photo", None) and not getattr(msg, "document", None):
            return None
        # Telethon pode retornar bytes se passarmos um BytesIO
        buf = io.BytesIO()
        await msg.download_media(buf)
        data = buf.getvalue()
        return data if data else None
    except Exception:
        logger.exception("[liga] falha ao baixar mídia")
        return None


def _is_image_message(event) -> bool:
    msg = event.message
    if msg is None:
        return False
    if getattr(msg, "photo", None):
        return True
    doc = getattr(msg, "document", None)
    if doc is None:
        return False
    mime = getattr(doc, "mime_type", "") or ""
    return mime.startswith("image/")


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------
async def _process_account_screenshot(event, lead: Lead, session, img: bytes) -> None:
    """Roda Claude Vision na captura da conta e atualiza estado/saldo do lead.

    Usado em waiting_id, waiting_deposit e waitlist — qualquer estágio onde o lead
    precisa provar ID + saldo real.
    """
    image_path = _save_proof_image(img)
    result = analyze_account_screenshot(img, lead_id=lead.id)

    # Anti-fraude: se essa imagem já foi vista pra OUTRO lead, é print compartilhado
    img_hash = result.get("_image_hash")
    if img_hash:
        try:
            from ai.providers import check_image_seen_for_other_lead
            other_lead_id = check_image_seen_for_other_lead(img_hash, lead.id)
            if other_lead_id:
                logger.warning(
                    "[fraude] lead=%s mandou print já visto pro lead_id=%d (hash=%s...)",
                    lead.display_name, other_lead_id, img_hash[:12],
                )
                rejected = OperationProof(
                    lead_id=lead.id,
                    proof_date=_today_ba_str(),
                    volume_usd=result.get("saldo_real_usd") or 0.0,
                    account_id_raw=(str(result.get("id_conta")) if result.get("id_conta") else "")[:100],
                    platform=(result.get("plataforma") or "desconhecida")[:100],
                    confidence=(result.get("confianca") or "baixa")[:10],
                    image_path=image_path,
                    validated=False,
                    raw_ai_response=json.dumps(
                        {**result, "rejected_reason": "duplicate_image", "duplicate_of_lead_id": other_lead_id},
                        ensure_ascii=False,
                    ),
                )
                session.add(rejected)
                await event.reply(
                    "🚨 Detectei que esse mesmo print já foi enviado por outra pessoa. "
                    "Para entrar a la Liga preciso de um screenshot da *tua* conta. "
                    "Se essa for sua conta de verdade, abre a plataforma agora, tira um print *novo* "
                    "e me manda 📸"
                )
                return
        except Exception:
            logger.debug("[fraude] erro checando duplicata", exc_info=True)
    valido = bool(result.get("valido"))
    conf = (result.get("confianca") or "baixa").lower()
    saldo_real = result.get("saldo_real_usd")
    saldo_demo = result.get("saldo_demo_usd")
    id_conta = result.get("id_conta")

    try:
        saldo_real = float(saldo_real) if saldo_real is not None else None
    except Exception:
        saldo_real = None

    if not valido or conf == "baixa" or saldo_real is None:
        # Salva pra revisão manual no painel
        rejected = OperationProof(
            lead_id=lead.id,
            proof_date=_today_ba_str(),
            volume_usd=saldo_real or 0.0,
            account_id_raw=(str(id_conta) if id_conta else "")[:100],
            platform=(result.get("plataforma") or "desconhecida")[:100],
            confidence=conf[:10],
            image_path=image_path,
            validated=False,
            raw_ai_response=json.dumps(
                {**result, "rejected_reason": "low_confidence"}, ensure_ascii=False,
            ),
        )
        session.add(rejected)
        await event.reply(
            "No logré leer tu cuenta con claridad 😕 Te marqué para revisión manual y "
            "te aviso enseguida.\n\n"
            "Si querés acelerar, mandame otro screenshot que muestre:\n"
            "① Tu *ID* (debajo del email)\n"
            "② El saldo de la *Cuenta real* (no la demo) 📸"
        )
        return

    # Verifica consistência do ID com o que já estava cadastrado
    def _digits(s) -> str:
        return "".join(ch for ch in str(s or "") if ch.isdigit())

    existing_id = _digits(lead.liga_account_id)
    new_id = _digits(id_conta)

    if existing_id and new_id and existing_id != new_id:
        logger.warning(
            "[liga] mismatch ID lead=%s registrado=%s novo=%s — bloqueando update",
            lead.display_name, existing_id, new_id,
        )
        # Registra a tentativa pra auditoria mas marca como NÃO validada
        rejected_proof = OperationProof(
            lead_id=lead.id,
            proof_date=_today_ba_str(),
            volume_usd=saldo_real,
            account_id_raw=str(id_conta)[:100],
            platform=(result.get("plataforma") or "desconhecida")[:100],
            confidence=conf[:10],
            image_path=image_path,
            validated=False,  # rejeitado por mismatch de ID
            raw_ai_response=json.dumps({**result, "rejected_reason": "id_mismatch"}, ensure_ascii=False),
        )
        session.add(rejected_proof)
        await event.reply(
            f"⚠️ Pará un toque! El ID que veo en este screenshot (*{new_id}*) es "
            f"distinto del que ya tenía registrado para vos (*{existing_id}*).\n\n"
            "Para la Liga necesito ver *siempre la misma cuenta*. "
            "Si cambiaste de cuenta, avisame por mensaje antes de mandar el nuevo screenshot. "
            f"Sino, mandame el screenshot de la cuenta con ID `{existing_id}` 📸"
        )
        return

    # Registra o comprovante (a partir daqui, ID bate ou ainda não havia ID salvo)
    proof = OperationProof(
        lead_id=lead.id,
        proof_date=_today_ba_str(),
        volume_usd=saldo_real,
        account_id_raw=(str(id_conta) if id_conta else "")[:100],
        platform=(result.get("plataforma") or "desconhecida")[:100],
        confidence=conf[:10],
        image_path=image_path,
        validated=True,
        raw_ai_response=json.dumps(result, ensure_ascii=False),
    )
    session.add(proof)

    # Salva o ID se ainda não tinha (primeiro print)
    if new_id and not existing_id:
        lead.liga_account_id = new_id[:100]

    lead.liga_balance = saldo_real

    if saldo_real >= 100:
        lead.liga_state = LigaState.ACTIVE.value
        lead.last_bot_action = "screenshot_confirmed_active"
        logger.info(
            "[liga] lead %s -> ACTIVE id=%s saldo_real=$%.2f",
            lead.display_name, id_conta, saldo_real,
        )
        nome = lead.first_name or "crack"
        await event.reply(
            f"🏆 Listo, *{nome}*! Confirmado.\n\n"
            f"• ID: `{id_conta}`\n"
            f"• Banca real: *${saldo_real:.2f}*\n\n"
            "Ya estás oficialmente en la *Liga* 🚀 La ventana para mandar el comprobante "
            "diario es de *00:01 a 23:59* (hora Buenos Aires). Cada día mandame el screenshot "
            "del historial de operaciones 📸"
        )
    else:
        lead.liga_state = LigaState.WAITLIST.value
        lead.last_bot_action = "screenshot_confirmed_waitlist"
        diff = max(0.0, 100.0 - saldo_real)
        logger.info(
            "[liga] lead %s -> WAITLIST id=%s saldo_real=$%.2f (faltam $%.2f)",
            lead.display_name, id_conta, saldo_real, diff,
        )
        await event.reply(
            f"Recibí tu cuenta!\n\n"
            f"• ID: `{id_conta}`\n"
            f"• Banca real: *${saldo_real:.2f}*\n\n"
            f"Para entrar a la Liga necesitás un mínimo de *$100*. "
            f"Te faltan *${diff:.2f}*. Cuando completes, mandame otro screenshot 📸"
        )


async def handle_waiting_id(event, lead: Lead, session, client) -> None:
    """Estado: esperando o lead enviar a captura da conta (ou ID por texto)."""
    if _is_image_message(event):
        img = await _download_image_bytes(event)
        if not img:
            await event.reply("No pude descargar la imagen. Probá mandarla de nuevo 🙏")
            return
        await _process_account_screenshot(event, lead, session, img)
        return

    # Fallback: texto puro = ID. Mantém o fluxo de 2 passos pra quem não manda print.
    text = (event.message.message or "").strip()
    if not text:
        await event.reply(
            "Para entrar a la Liga, mandame un *screenshot de tu cuenta* en la plataforma "
            "mostrando el *ID* y el *saldo real* 📸\n\n"
            "(O si querés podés mandarme solo el ID en texto y después el screenshot.)"
        )
        return

    lead.liga_account_id = text[:100]
    lead.liga_state = LigaState.WAITING_DEPOSIT.value
    lead.last_bot_action = "asked_deposit"
    logger.info("[liga] lead %s registrou account_id=%s (texto)", lead.display_name, text[:50])

    await event.reply(
        f"Listo, te anoté el ID *{text}* ✅\n\n"
        "Ahora mandame un *screenshot de tu cuenta* mostrando el *saldo real* "
        "(mínimo $100 para entrar a la Liga) 📸"
    )


async def handle_waiting_deposit(event, lead: Lead, session, client) -> None:
    """Estado: esperando print da conta com saldo real >= $100."""
    if not _is_image_message(event):
        await event.reply(
            "Necesito el *screenshot de tu cuenta* mostrando el saldo real 📸 "
            "Mandámelo acá."
        )
        return

    img = await _download_image_bytes(event)
    if not img:
        await event.reply("No pude descargar la imagen. Probá mandarla de nuevo 🙏")
        return

    await _process_account_screenshot(event, lead, session, img)


async def handle_active_waiting_proof(event, lead: Lead, session, client) -> None:
    """Estado: lead ativo enviando comprovantes diários (lista de operações)."""
    today = _today_ba_str()

    if _is_image_message(event):
        img = await _download_image_bytes(event)
        if not img:
            await event.reply("No pude descargar la imagen. Probá mandarla de nuevo, por favor 🙏")
            return

        # Salva a imagem em disco SEMPRE (pra revisão manual no painel)
        image_path = _save_proof_image(img)

        result = analyze_proof_image(img, lead_id=lead.id)

        # Anti-fraude: print já visto pra outro lead → rejeita
        img_hash = result.get("_image_hash")
        if img_hash:
            try:
                from ai.providers import check_image_seen_for_other_lead
                other_lead_id = check_image_seen_for_other_lead(img_hash, lead.id)
                if other_lead_id:
                    logger.warning(
                        "[fraude] proof duplicado lead=%s outro_lead=%d hash=%s...",
                        lead.display_name, other_lead_id, img_hash[:12],
                    )
                    rejected = OperationProof(
                        lead_id=lead.id,
                        proof_date=_today_ba_str(),
                        volume_usd=0.0,
                        platform=(result.get("plataforma") or "desconhecida")[:100],
                        confidence="baixa",
                        image_path=image_path,
                        validated=False,
                        raw_ai_response=json.dumps(
                            {**result, "rejected_reason": "duplicate_image", "duplicate_of_lead_id": other_lead_id},
                            ensure_ascii=False,
                        ),
                    )
                    session.add(rejected)
                    await event.reply(
                        "🚨 Esse print já foi enviado por outra pessoa. "
                        "Tira um screenshot *novo* da tua conta e manda 📸"
                    )
                    return
            except Exception:
                logger.debug("[fraude] erro checando duplicata", exc_info=True)
        valido = bool(result.get("valido"))
        conf = (result.get("confianca") or "baixa").lower()
        tipo_conta = (result.get("tipo_conta") or "").lower()
        valor = result.get("valor_usd")
        try:
            valor = float(valor) if valor is not None else 0.0
        except Exception:
            valor = 0.0
        proof_date = result.get("data_operacao") or today

        # Conta DEMO → rejeita explícito
        if tipo_conta == "demo":
            proof = OperationProof(
                lead_id=lead.id,
                proof_date=proof_date or today,
                volume_usd=valor,
                account_id_raw=(result.get("id_conta") or "")[:100],
                platform=(result.get("plataforma") or "desconhecida")[:100],
                confidence=conf[:10],
                image_path=image_path,
                validated=False,
                raw_ai_response=json.dumps(
                    {**result, "rejected_reason": "demo_account"}, ensure_ascii=False,
                ),
            )
            session.add(proof)
            await event.reply(
                "Ese screenshot es de la *Cuenta Demo* 🎮 — para la Liga solo tomamos el "
                "historial de la *Cuenta Real*. Cambiá a Real y mandame otro 📸"
            )
            return

        # Imagem ruim ou IA com baixa confiança → fila de revisão manual
        if not valido or conf == "baixa" or valor <= 0:
            proof = OperationProof(
                lead_id=lead.id,
                proof_date=proof_date or today,
                volume_usd=valor,
                account_id_raw=(result.get("id_conta") or "")[:100],
                platform=(result.get("plataforma") or "desconhecida")[:100],
                confidence=conf[:10],
                image_path=image_path,
                validated=False,
                raw_ai_response=json.dumps(
                    {**result, "rejected_reason": "low_confidence"}, ensure_ascii=False,
                ),
            )
            session.add(proof)
            logger.warning(
                "[liga] proof low_confidence lead=%s @%s conf=%s valor=%s — vai pra revisão manual",
                lead.display_name, lead.username or "?", conf, valor,
            )
            await event.reply(
                "📥 Recibí tu screenshot! Lo estoy *revisando manualmente* "
                "(el sistema no logró leer todos los valores con claridad). "
                "Te aviso apenas confirmo. Tranqui, podés seguir mandando los próximos sin problema."
            )
            return

        # Verifica se a data do comprovante é de hoje (BA)
        if proof_date and proof_date != today:
            await event.reply(
                f"Este screenshot parece ser del *{proof_date}*. "
                f"La ventana de hoy (hora Buenos Aires) todavía no la mandaste. "
                "Mandame el de hoy! 📅"
            )
            return

        # Registra o comprovante validado
        proof = OperationProof(
            lead_id=lead.id,
            proof_date=proof_date,
            volume_usd=valor,
            account_id_raw=(result.get("id_conta") or "")[:100],
            platform=(result.get("plataforma") or "desconhecida")[:100],
            confidence=conf[:10],
            image_path=image_path,
            validated=True,
            raw_ai_response=json.dumps(result, ensure_ascii=False),
        )
        session.add(proof)

        # Atualiza/cria DailyVolume de hoje
        dv = (
            session.query(DailyVolume)
            .filter(DailyVolume.lead_id == lead.id, DailyVolume.date == today)
            .one_or_none()
        )
        if dv:
            dv.volume_usd = (dv.volume_usd or 0.0) + valor
        else:
            dv = DailyVolume(lead_id=lead.id, date=today, volume_usd=valor)
            session.add(dv)

        # Atualiza streak / proof_sent_today
        if not lead.proof_sent_today:
            lead.streak_days = (lead.streak_days or 0) + 1
        lead.proof_sent_today = True
        lead.last_bot_action = "proof_received"

        # Recalcula score
        try:
            from liga.scoring import calc_lead_score
            session.flush()
            lead.lead_score = calc_lead_score(lead, session)
        except Exception:
            logger.exception("[liga] falha ao recalcular score")

        # Total acumulado
        from sqlalchemy import func
        total = (
            session.query(func.sum(DailyVolume.volume_usd))
            .filter(DailyVolume.lead_id == lead.id)
            .scalar() or 0.0
        )

        question = _relationship_question()
        n = lead.streak_days or 1
        ops = result.get("num_operacoes")
        ops_msg = f" ({ops} operaciones)" if ops else ""
        logger.info(
            "[liga] proof OK lead=%s dia=%d hoje=$%.2f total=$%.2f streak=%d ops=%s",
            lead.display_name, n, valor, total, lead.streak_days or 0, ops,
        )
        await event.reply(
            f"✅ Día *{n}* confirmado{ops_msg}!\n\n"
            f"Volumen de hoy: *${valor:.2f}* | "
            f"Acumulado: *${total:.2f}* | Racha: *{lead.streak_days or 0} días* 🔥\n\n"
            f"{question}"
        )
        return

    # Mensagem de texto
    if lead.proof_sent_today:
        # Conversa normal — deixa o tracker fazer a classificação heurística depois
        return

    await event.reply(
        "Esperando tu screenshot de hoy 📸 "
        "Mandame el historial de operaciones cuando termines de operar!"
    )


async def handle_waitlist(event, lead: Lead, session, client) -> None:
    """Estado: depósito feito mas abaixo de $100. Aguarda novo print da conta."""
    if not _is_image_message(event):
        bal = lead.liga_balance or 0.0
        diff = max(0.0, 100.0 - bal)
        await event.reply(
            f"Tu banca actual es *${bal:.2f}*. Para entrar a la Liga te faltan "
            f"*${diff:.2f}*. Cuando completes, mandame otro screenshot de la cuenta 📸"
        )
        return

    img = await _download_image_bytes(event)
    if not img:
        await event.reply("No pude descargar la imagen. Probá mandarla de nuevo 🙏")
        return

    await _process_account_screenshot(event, lead, session, img)


async def handle_unknown(event, lead: Lead, session, client) -> None:
    """Estado desconhecido / NEW — fallback genérico.

    Apenas loga; o tracker já tem a lógica de métricas/classificação heurística
    do remarketing rodando depois deste handler.
    """
    logger.debug("[liga] handle_unknown lead=%s state=%s", lead.display_name, lead.liga_state)
    return
