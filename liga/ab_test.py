"""A/B test com significância estatística pra variantes de Script.

Usa chi-square test of independence (2x2 contingency table) sem depender de scipy.
Aproximação da distribuição chi-square via fórmula de Wilson-Hilferty.

Referências: chi² crítico pra 1 grau de liberdade:
- 90% confiança → 2.706
- 95% confiança → 3.841
- 99% confiança → 6.635
"""
from __future__ import annotations

import logging
import math
from typing import Optional

from db import SessionLocal
from db.models import Script, ScriptVariant, Send, SendStatus

logger = logging.getLogger(__name__)


# Critical chi² values pra 1 dof
CHI2_CRITICAL = {
    0.90: 2.706,
    0.95: 3.841,
    0.99: 6.635,
}


def _chi2_2x2(a: int, b: int, c: int, d: int) -> float:
    """Chi-square test pra tabela 2x2 com correção de Yates.

    a = variant A success
    b = variant A failure
    c = variant B success
    d = variant B failure

    Returns chi² value (always >= 0).
    """
    n = a + b + c + d
    if n == 0:
        return 0.0
    # Expected frequencies
    row1 = a + b
    row2 = c + d
    col1 = a + c
    col2 = b + d
    if row1 == 0 or row2 == 0 or col1 == 0 or col2 == 0:
        return 0.0

    e_a = row1 * col1 / n
    e_b = row1 * col2 / n
    e_c = row2 * col1 / n
    e_d = row2 * col2 / n

    # Correção de Yates (continuity correction): subtrai 0.5 do |obs-esp|
    def _term(obs, exp):
        if exp == 0:
            return 0.0
        return ((abs(obs - exp) - 0.5) ** 2) / exp

    chi2 = _term(a, e_a) + _term(b, e_b) + _term(c, e_c) + _term(d, e_d)
    return chi2


def _confidence_from_chi2(chi2: float) -> float:
    """Retorna confiança aproximada (0-100) baseado em chi² (1 dof).

    Aproximação grosseira via tabela:
    """
    if chi2 < 0.5: return 50.0
    if chi2 < 1.0: return 60.0
    if chi2 < 1.5: return 70.0
    if chi2 < 2.0: return 80.0
    if chi2 < 2.706: return 88.0
    if chi2 < 3.0: return 91.0
    if chi2 < 3.841: return 94.0
    if chi2 < 5.0: return 97.0
    if chi2 < 6.635: return 98.0
    if chi2 < 10.0: return 99.5
    return 99.9


def compare_variants(variant_a_id: int, variant_b_id: int) -> dict:
    """Compara 2 variantes via chi² test."""
    with SessionLocal() as s:
        va = s.query(ScriptVariant).get(variant_a_id)
        vb = s.query(ScriptVariant).get(variant_b_id)
        if not va or not vb:
            return {"error": "variantes não encontradas"}

        a_sends = (
            s.query(Send).filter(Send.variant_id == variant_a_id)
            .filter(Send.status == SendStatus.SENT.value).count()
        )
        a_replies = (
            s.query(Send).filter(Send.variant_id == variant_a_id)
            .filter(Send.replied.is_(True)).count()
        )
        b_sends = (
            s.query(Send).filter(Send.variant_id == variant_b_id)
            .filter(Send.status == SendStatus.SENT.value).count()
        )
        b_replies = (
            s.query(Send).filter(Send.variant_id == variant_b_id)
            .filter(Send.replied.is_(True)).count()
        )

    a_no_reply = max(0, a_sends - a_replies)
    b_no_reply = max(0, b_sends - b_replies)
    a_rate = (a_replies / a_sends) if a_sends > 0 else 0.0
    b_rate = (b_replies / b_sends) if b_sends > 0 else 0.0

    chi2 = _chi2_2x2(a_replies, a_no_reply, b_replies, b_no_reply)
    confidence = _confidence_from_chi2(chi2)

    # Determina vencedor
    winner = None
    significant = chi2 >= CHI2_CRITICAL[0.95]  # 95% threshold padrão
    if significant:
        winner = "A" if a_rate > b_rate else "B"

    # Sample size mínimo (rule of thumb): N por grupo >= 30
    enough_data = a_sends >= 30 and b_sends >= 30
    delta_pct = ((a_rate - b_rate) * 100) if a_rate != b_rate else 0.0

    return {
        "variant_a": {
            "id": variant_a_id, "label": va.label, "text_preview": (va.text_es or "")[:100],
            "sends": a_sends, "replies": a_replies, "rate": a_rate,
        },
        "variant_b": {
            "id": variant_b_id, "label": vb.label, "text_preview": (vb.text_es or "")[:100],
            "sends": b_sends, "replies": b_replies, "rate": b_rate,
        },
        "chi2": chi2,
        "confidence_pct": confidence,
        "significant_at_95": significant,
        "winner": winner,
        "delta_pct_points": delta_pct,
        "enough_data": enough_data,
        "recommendation": _recommend(significant, winner, enough_data, a_rate, b_rate),
    }


def _recommend(significant: bool, winner: Optional[str], enough_data: bool, a_rate: float, b_rate: float) -> str:
    if not enough_data:
        return "Coletar mais dados (mínimo 30 sends/variante recomendado)"
    if not significant:
        return "Sem diferença significativa ainda. Continue testando."
    if winner == "A":
        ratio = (a_rate / max(b_rate, 0.001))
        return f"✓ Variante A é vencedora ({ratio:.1f}x melhor). Promover A, considerar desativar B."
    if winner == "B":
        ratio = (b_rate / max(a_rate, 0.001))
        return f"✓ Variante B é vencedora ({ratio:.1f}x melhor). Promover B, considerar desativar A."
    return "—"


def all_ab_tests_for_script(script_id: int) -> list[dict]:
    """Compara TODAS as variantes ativas de um script entre si."""
    with SessionLocal() as s:
        variants = (
            s.query(ScriptVariant)
            .filter(ScriptVariant.script_id == script_id)
            .filter(ScriptVariant.is_active.is_(True))
            .all()
        )
        var_ids = [v.id for v in variants]

    results = []
    for i, va_id in enumerate(var_ids):
        for vb_id in var_ids[i + 1:]:
            cmp = compare_variants(va_id, vb_id)
            if "error" not in cmp:
                results.append(cmp)
    return results
