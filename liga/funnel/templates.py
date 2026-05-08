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
        # Cada um vira 1 ScriptVariant. Bot usa em ordem nas etapas.
        ("registro_link", "Para unirte al grupo privado donde envío más operaciones y hago reuniones en vivo, registrate con mi enlace 👇"),
        ("registro_link_v2", "Para entrar al VIP donde mando todas las operaciones y hacemos reuniones en vivo, necesitás registrarte con mi link 👇"),

        ("link_quotex", "https://broker-qx.pro/sign-up"),

        ("email_diferente", "Si ya tenés una cuenta en Quotex, eliminala y creá una nueva con un correo diferente."),
        ("email_diferente_v2", "Si ya tenés cuenta en Quotex, tenés que eliminarla y crear una nueva con otro mail."),

        ("avisame", "Una vez lo hagas, avisame!"),
        ("avisame_v2", "Cuando termines, avisame!"),

        ("pedir_id", "Mandame el ID de tu cuenta nueva!"),
        ("pedir_id_v2", "Pasame el ID de tu cuenta de Quotex!"),

        ("aguardar_deposito", "No me figura el depósito todavía, dale unos minutos y avisame de nuevo!"),
        ("aguardar_deposito_v2", "Esperame que veo... no me aparece el depósito aún. Probá de nuevo en un par de minutos!"),

        ("excelente", "Excelente!"),
        ("excelente_v2", "Buenísimo!"),

        ("link_grupo_intro", "Ya estás dentro 🚀 Sumate al grupo:"),

        ("bienvenido", "Bienvenido al equipo! Cualquier duda, mandame mensaje. Estoy a la orden."),
        ("bienvenido_v2", "Bienvenido al grupo! 🚀 Cualquier cosa, escribime."),
    ],

    # Etapas: (source_state, trigger_intent, target_state, [variant_keys], extra_action, delays)
    "steps": [
        {
            "source_state": "new",
            "trigger_intent": "quer_entrar_vip",
            "target_state": "onboarding",
            "scripts": ["registro_link", "link_quotex", "email_diferente", "avisame"],
            "delay_min": 8, "delay_max": 20,
            "delay_between_min": 2, "delay_between_max": 5,
            "extra_action": None,
            "_note": "Lead chega + manda 'quero VIP'. Bot manda intro completa.",
        },
        {
            "source_state": "onboarding",
            "trigger_intent": "confirmou",
            "target_state": "waiting_id",
            "scripts": ["pedir_id"],
            "delay_min": 8, "delay_max": 18,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": None,
            "_note": "Lead diz 'listo'. Bot pede ID.",
        },
        {
            "source_state": "waiting_id",
            "trigger_intent": "enviou_id_texto",
            "target_state": "waiting_deposit",
            "scripts": [],  # nenhum texto — só ação de validação
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "Lead manda ID em texto. Bot valida no @QuotexPartnerBot.",
        },
        {
            "source_state": "waiting_id",
            "trigger_intent": "enviou_id_imagem",
            "target_state": "waiting_deposit",
            "scripts": [],
            "delay_min": 5, "delay_max": 12,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_id",
            "_note": "Lead manda print do ID. Vision + partner bot.",
        },
        {
            "source_state": "waiting_deposit",
            "trigger_intent": "confirmou",
            "target_state": "active",
            "scripts": ["excelente", "link_grupo_intro", "bienvenido"],
            "delay_min": 5, "delay_max": 10,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": "validate_deposit",
            "_note": "Lead diz 'depositei'. Bot valida e manda link do grupo.",
        },
        {
            "source_state": "waiting_deposit",
            "trigger_intent": "deposito_promessa",
            "target_state": "waiting_deposit",  # fica no mesmo estado
            "scripts": ["aguardar_deposito"],
            "delay_min": 8, "delay_max": 15,
            "delay_between_min": 1, "delay_between_max": 3,
            "extra_action": None,
            "_note": "Lead diz 'depósito não tá aparecendo'. Bot pede pra esperar.",
        },
    ],
}


def create_vip_aquisicao_funnel() -> dict:
    """Cria o funil VIP de aquisição completo (script + variantes + funnel + steps).

    Idempotente: se já existir um funil com o mesmo nome, retorna ele em vez
    de duplicar.

    Returns dict com {"ok": bool, "funnel_id": int, "created_scripts": int,
                      "created_steps": int, "warnings": [...]}
    """
    template = VIP_AQUISICAO_TEMPLATE
    warnings = []

    with SessionLocal() as s:
        # Idempotência
        existing = s.query(Funnel).filter(Funnel.name == template["name"]).first()
        if existing:
            return {
                "ok": True, "funnel_id": existing.id,
                "already_exists": True,
                "message": f"Funil '{template['name']}' já existe (id {existing.id}). Edite ou exclua antes de criar de novo.",
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
