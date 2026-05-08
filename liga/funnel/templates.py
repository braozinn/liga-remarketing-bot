"""Templates de funis pré-configurados.

Em vez do user montar etapa-por-etapa, oferece funis prontos com:
- Scripts em ES rioplatense já criados (voseo)
- Etapas pré-configuradas com transições corretas
- Delays sensatos
- Tudo em DRY RUN — user só precisa testar e ativar

Templates disponíveis:
- vip_aquisicao: funil de primeiro contato → entrada no grupo VIP
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from db import SessionLocal
from db.models import (
    Funnel, FunnelStep, Script, ScriptVariant, ScriptMode,
)

logger = logging.getLogger(__name__)


# ============================================================================
# TEMPLATE: Funil de aquisição VIP
# ============================================================================
#
# Fluxo:
#   1. Lead diz "quero entrar no VIP"     → bot manda boas-vindas + link + email diff + "avisame"
#   2. Lead diz "listo"                   → bot pede ID
#   3. Lead manda ID (texto ou imagem)    → bot valida, manda bolinha de instruções
#   4. Lead diz "listo" (depois de depositar) → bot valida depósito, manda link grupo + bem-vindo
#
VIP_AQUISICAO_TEMPLATE = {
    "name": "Aquisição VIP (template pronto)",
    "description": "Funil de primeiro contato: lead pergunta como entrar → cadastro → ID → depósito → grupo. Em ES rioplatense (voseo).",
    "config": {
        "delay_min": 8,
        "delay_max": 20,
        "delay_between_min": 1,
        "delay_between_max": 5,
        "active_window_start": 8,
        "active_window_end": 23,
        "daily_cap": 150,
        "min_confidence": 0.7,
        "min_deposit_usd": 20,
    },

    "scripts": [
        # 1 script por etapa, sem variações. Mensagens definidas pelo Facundo.
        # Linha em branco dentro do texto = NOVA MENSAGEM (split automático no dispatcher).
        # Markdown ativo: *negrito*, _itálico_, [texto](url), [nombre]

        ("intro_completa",
         "Para unirte al grupo privado donde envío más operaciones y hago reuniones en vivo tenes que registrarte con mi enlace\n\n"
         "https://broker-qx.pro/sign-up\n\n"
         "Si ya tenes una cuenta en Quotex elimínala y créate una nueva con un correo electrónico diferente\n\n"
         "Una vez lo hagas, avísame!"),

        ("pedir_id", "enviame el ID de tu cuenta!"),

        ("pedir_deposito",
         "Ahora tenes que hacer un depósito de $20 USD o más para tener acceso al grupo privado\n\n"
         "Una vez lo hagas, ya podes ingresar al grupo privado"),

        ("link_grupo", "https://t.me/+zdaQT6bejPZhODk5"),

        ("aguardar_deposito",
         "El depósito todavía no me figura. Esperá unos minutos y avisame de nuevo!"),
    ],

    # Etapas: (source_state, trigger_intent, target_state, [variant_keys], extra_action, delays)
    # Lembrete: dispatcher splita cada script em múltiplas msgs por linha em branco.
    "steps": [
        {
            "source_state": "new",
            "trigger_intent": "quer_entrar_vip",
            "target_state": "onboarding",
            "scripts": ["intro_completa"],   # 1 script com 4 blocos = 4 msgs separadas
            "media_position": "before",       # ⚠ FAZER UPLOAD BOLINHA INTRO (manda antes dos textos)
            "delay_min": 8, "delay_max": 20,
            "delay_between_min": 2, "delay_between_max": 5,
            "extra_action": None,
            "_note": "Lead manda 'quero VIP'. Bot manda 🎥 BOLINHA INTRO (upload manual) + 4 mensagens.",
        },
        {
            "source_state": "onboarding",
            "trigger_intent": "confirmou",
            "target_state": "waiting_id",
            "scripts": ["pedir_id"],
            "media_position": "before",
            "delay_min": 8, "delay_max": 18,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": None,
            "_note": "Lead diz 'listo'. Bot pede ID.",
        },
        {
            "source_state": "waiting_id",
            "trigger_intent": "enviou_id_texto",
            "target_state": "waiting_deposit",
            "scripts": [],                    # SEM TEXTO — só bolinha de depósito
            "media_position": "replace",      # ⚠ FAZER UPLOAD BOLINHA DEPÓSITO (substitui textos)
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "Lead manda ID em texto. Bot valida no @QuotexPartnerBot. Se inválido, notifica admin. Se válido, manda 🎥 BOLINHA DEPÓSITO (upload manual, modo replace).",
        },
        {
            "source_state": "waiting_id",
            "trigger_intent": "enviou_id_imagem",
            "target_state": "waiting_deposit",
            "scripts": [],
            "media_position": "replace",      # mesma bolinha de depósito
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "Lead manda print do ID. Vision + partner bot. Mesma 🎥 BOLINHA DEPÓSITO.",
        },
        # ─── CAMINHO RÁPIDO 1: lead chega já mandando print direto (estado new) ───
        # Lead novo manda print da conta Quotex sem nem dizer "quiero entrar".
        # Vision detecta ID, dispatcher força intent=enviou_id_imagem, e essa
        # etapa pula a intro inteira — vai direto pra waiting_deposit.
        {
            "source_state": "new",
            "trigger_intent": "enviou_id_imagem",
            "target_state": "waiting_deposit",
            "scripts": [],                    # sem texto
            "media_position": "replace",      # bolinha de depósito direto
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "CAMINHO RÁPIDO: lead novo já mandou print do ID — pula intro, valida e pede depósito direto.",
        },
        # ─── CAMINHO RÁPIDO 2: lead em onboarding pula 'listo' e manda print ───
        # Lead recebeu intro, criou conta, e em vez de dizer "listo" mandou
        # print direto. Bot reconhece e pula a etapa "pedir ID" — vai direto
        # pra validar e pedir depósito.
        {
            "source_state": "onboarding",
            "trigger_intent": "enviou_id_imagem",
            "target_state": "waiting_deposit",
            "scripts": [],
            "media_position": "replace",
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "CAMINHO RÁPIDO: lead pulou 'listo' e mandou print — valida e pede depósito direto.",
        },
        {
            "source_state": "waiting_deposit",
            "trigger_intent": "confirmou",
            "target_state": "active",
            "scripts": ["link_grupo"],
            "media_position": "before",
            "delay_min": 5, "delay_max": 10,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_deposit",
            "_note": "Lead diz 'depositei'. Bot confere se >= $20. Se sim, manda link grupo. Se não, notifica admin.",
        },
        {
            "source_state": "waiting_deposit",
            "trigger_intent": "deposito_promessa",
            "target_state": "waiting_deposit",  # fica no mesmo estado
            "scripts": ["aguardar_deposito"],
            "media_position": "before",
            "delay_min": 8, "delay_max": 15,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": None,
            "_note": "Lead diz 'depósito não tá aparecendo'. Bot pede pra esperar.",
        },
    ],
}


def create_vip_aquisicao_funnel(force_recreate: bool = False) -> dict:
    """Cria o funil VIP de aquisição completo (script + variantes + funnel + steps).

    Args:
        force_recreate: se True, APAGA funil existente com mesmo nome e recria
                       do zero. Use quando atualizar o template (ex: novos scripts).

    Idempotente quando force_recreate=False: se já existir, retorna ele.

    Returns dict com {"ok": bool, "funnel_id": int, "created_scripts": int,
                      "created_steps": int, "warnings": [...]}
    """
    template = VIP_AQUISICAO_TEMPLATE
    warnings = []

    with SessionLocal() as s:
        # Idempotência (ou recriação)
        existing = s.query(Funnel).filter(Funnel.name == template["name"]).first()
        if existing:
            if force_recreate:
                logger.info("[template vip] força recriação — apagando funil existente id=%d", existing.id)
                s.delete(existing)
                s.commit()
                # Continua o fluxo abaixo pra criar do zero
            else:
                return {
                    "ok": True, "funnel_id": existing.id,
                    "already_exists": True,
                    "message": f"Funil '{template['name']}' já existe (id {existing.id}). Use 'Atualizar' pra recriar com mudanças do template.",
                }

        # 1. Cria/reusa o Script container das variantes
        script = s.query(Script).filter(Script.name == "Funil VIP - Aquisição (auto)").first()
        if not script:
            script = Script(
                name="Funil VIP - Aquisição (auto)",
                mode=ScriptMode.AI.value,
                briefing_pt="Auto-gerado pelo template VIP de aquisição. Edite os textos pelo painel.",
                objective="Funil de aquisição: cadastro → ID → depósito → grupo VIP",
                is_active=True,
            )
            s.add(script)
            s.commit()
            s.refresh(script)

        # 2. Cria as variantes (1 por entrada de "scripts")
        variant_id_by_key = {}
        created_variants = 0
        for key, text_es in template["scripts"]:
            existing_v = (
                s.query(ScriptVariant)
                .filter(ScriptVariant.script_id == script.id)
                .filter(ScriptVariant.label == key)
                .first()
            )
            if existing_v:
                variant_id_by_key[key] = existing_v.id
                continue
            v = ScriptVariant(
                script_id=script.id,
                label=key,
                text_es=text_es,
                ai_provider="manual",
                is_active=True,
            )
            s.add(v)
            s.commit()
            s.refresh(v)
            variant_id_by_key[key] = v.id
            created_variants += 1

        # 3. Cria o Funnel
        funnel = Funnel(
            name=template["name"],
            description=template["description"],
            is_active=False,
            is_dry_run=True,
            config_json=json.dumps(template["config"], indent=2),
        )
        s.add(funnel)
        s.commit()
        s.refresh(funnel)

        # 4. Cria as etapas
        created_steps = 0
        for idx, step_def in enumerate(template["steps"]):
            script_ids = []
            for key in step_def["scripts"]:
                if key in variant_id_by_key:
                    script_ids.append(variant_id_by_key[key])
                else:
                    warnings.append(f"variant '{key}' não encontrada na etapa {idx+1}")

            step = FunnelStep(
                funnel_id=funnel.id,
                source_state=step_def["source_state"],
                trigger_intent=step_def["trigger_intent"],
                target_state=step_def["target_state"],
                script_ids_json=json.dumps(script_ids),
                media_ids_json=json.dumps([]),  # user adiciona depois
                media_position=step_def.get("media_position", "before"),
                delay_min=step_def["delay_min"],
                delay_max=step_def["delay_max"],
                delay_between_min=step_def["delay_between_min"],
                delay_between_max=step_def["delay_between_max"],
                extra_action=step_def.get("extra_action"),
                order_index=idx,
            )
            s.add(step)
            created_steps += 1

        s.commit()
        funnel_id = funnel.id

    logger.info(
        "[template vip_aquisicao] criado funil=%d, %d variantes, %d etapas",
        funnel_id, created_variants, created_steps,
    )
    return {
        "ok": True,
        "funnel_id": funnel_id,
        "created_variants": created_variants,
        "created_steps": created_steps,
        "warnings": warnings,
        "message": f"Funil '{template['name']}' criado com {created_steps} etapas. Está em DRY RUN — adicione mídia (bolinhas) e teste antes de ativar.",
    }


def update_vip_aquisicao_texts() -> dict:
    """Atualiza os textos das variantes do funil VIP sem mexer nas etapas/mídia.

    Usa o `VIP_AQUISICAO_TEMPLATE['scripts']` como fonte da verdade e sobrescreve
    o `text_es` de cada ScriptVariant cujo label bate com a chave.

    Útil quando você muda só os textos no template (ex: corrigir link Quotex)
    e quer aplicar no funil existente sem recriar tudo (perderia mídia uploaded).

    Returns dict com {"ok", "updated", "skipped", "details": [...]}.
    """
    template = VIP_AQUISICAO_TEMPLATE
    updated = 0
    skipped = 0
    details = []

    with SessionLocal() as s:
        # Encontra o Script container
        script = s.query(Script).filter(Script.name == "Funil VIP - Aquisição (auto)").first()
        if not script:
            return {
                "ok": False,
                "error": "Script 'Funil VIP - Aquisição (auto)' não encontrado. Crie o funil VIP primeiro.",
            }

        for key, new_text in template["scripts"]:
            variant = (
                s.query(ScriptVariant)
                .filter(ScriptVariant.script_id == script.id)
                .filter(ScriptVariant.label == key)
                .first()
            )
            if not variant:
                # Cria a variante se não existir (caso o template tenha
                # adicionado scripts novos depois)
                variant = ScriptVariant(
                    script_id=script.id,
                    label=key,
                    text_es=new_text,
                    ai_provider="manual",
                    is_active=True,
                )
                s.add(variant)
                updated += 1
                details.append(f"+ criado: {key}")
                continue

            if variant.text_es == new_text:
                skipped += 1
                details.append(f"= sem mudanças: {key}")
                continue

            variant.text_es = new_text
            updated += 1
            details.append(f"✓ atualizado: {key}")

        s.commit()

    logger.info(
        "[template vip_aquisicao] textos atualizados: %d alterados, %d sem mudanças",
        updated, skipped,
    )
    return {
        "ok": True,
        "updated": updated,
        "skipped": skipped,
        "details": details,
        "message": f"✓ {updated} texto(s) atualizado(s), {skipped} sem mudanças. Etapas e mídia preservadas.",
    }
