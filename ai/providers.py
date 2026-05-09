"""Abstração de provedores de IA (Anthropic Claude / OpenAI).

Inclui:
- Cache por hash SHA256 das imagens (evita pagar Claude Vision pela mesma imagem 2×)
- Tracking de uso (tabela AIUsage) com estimativa de custo $
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)


# Pricing aproximado (USD por 1M tokens) — atualize conforme a tabela da Anthropic
_MODEL_PRICING = {
    "claude-haiku-4-5-20251001":  {"in": 1.0,  "out": 5.0},   # placeholder
    "claude-haiku-4-5":           {"in": 1.0,  "out": 5.0},
    "claude-sonnet-4-5-20251022": {"in": 3.0,  "out": 15.0},
    "claude-opus-4-5":            {"in": 15.0, "out": 75.0},
    "gpt-4o-mini":                {"in": 0.15, "out": 0.60},
    "gpt-4o":                     {"in": 2.50, "out": 10.0},
}


def _estimate_cost(model: str, in_tokens: int, out_tokens: int) -> float:
    """Estima $ baseado nos tokens (precificação por 1M tokens)."""
    pricing = _MODEL_PRICING.get(model)
    if not pricing:
        # Fallback genérico (Sonnet-like)
        pricing = {"in": 3.0, "out": 15.0}
    return (in_tokens / 1_000_000) * pricing["in"] + (out_tokens / 1_000_000) * pricing["out"]


def _record_usage(provider: str, model: str, operation: str, in_tokens: int, out_tokens: int, cached: bool = False, lead_id: Optional[int] = None):
    """Salva uma linha em AIUsage. Best-effort — não derruba a request se falhar."""
    try:
        from db import SessionLocal
        from db.models import AIUsage
        cost = 0.0 if cached else _estimate_cost(model, in_tokens, out_tokens)
        with SessionLocal() as s:
            s.add(AIUsage(
                provider=provider, model=model, operation=operation,
                input_tokens=in_tokens, output_tokens=out_tokens,
                cost_usd=cost, cached=cached, lead_id=lead_id,
            ))
            s.commit()
    except Exception:
        logger.debug("[ai_usage] não conseguiu gravar telemetria", exc_info=True)


def _image_hash(image_bytes: bytes) -> str:
    """SHA256 hex da imagem — chave de cache + chave anti-fraude."""
    return hashlib.sha256(image_bytes).hexdigest()


def _cache_lookup(image_hash: str, operation: str) -> Optional[dict]:
    """Tenta recuperar análise prévia da mesma imagem. Retorna dict ou None."""
    try:
        from db import SessionLocal
        from db.models import ImageCache
        with SessionLocal() as s:
            row = s.query(ImageCache).filter_by(image_hash=image_hash, operation=operation).first()
            if not row:
                return None
            row.seen_count = (row.seen_count or 0) + 1
            row.last_seen_at = datetime.utcnow()
            s.commit()
            try:
                return json.loads(row.raw_response)
            except Exception:
                return None
    except Exception:
        logger.debug("[image_cache] erro lookup", exc_info=True)
        return None


def _cache_store(image_hash: str, operation: str, result: dict, lead_id: Optional[int] = None) -> None:
    """Guarda resultado pra reuso futuro."""
    try:
        from db import SessionLocal
        from db.models import ImageCache
        with SessionLocal() as s:
            row = s.query(ImageCache).filter_by(image_hash=image_hash, operation=operation).first()
            if row:
                row.seen_count = (row.seen_count or 0) + 1
                row.last_seen_at = datetime.utcnow()
            else:
                s.add(ImageCache(
                    image_hash=image_hash, operation=operation,
                    raw_response=json.dumps(result, ensure_ascii=False)[:50_000],
                    first_lead_id=lead_id, seen_count=1,
                ))
            s.commit()
    except Exception:
        logger.debug("[image_cache] erro store", exc_info=True)


def check_image_seen_for_other_lead(image_hash: str, current_lead_id: int) -> Optional[int]:
    """Anti-fraude: se essa imagem já foi vista antes pra OUTRO lead, retorna o lead_id.

    Útil pra detectar quando 2 leads diferentes mandam o mesmo print
    (alguém compartilhou o screenshot).
    """
    try:
        from db import SessionLocal
        from db.models import ImageCache
        with SessionLocal() as s:
            row = s.query(ImageCache).filter_by(image_hash=image_hash).first()
            if row and row.first_lead_id and row.first_lead_id != current_lead_id:
                return row.first_lead_id
    except Exception:
        pass
    return None


class AIError(RuntimeError):
    pass


def _get_provider() -> str:
    return os.getenv("AI_PROVIDER", "anthropic").strip().lower()


def _get_model(provider: str) -> str:
    if provider == "anthropic":
        return os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    if provider == "openai":
        return os.getenv("OPENAI_MODEL", "gpt-4o-mini")
    raise AIError(f"Provedor desconhecido: {provider}")


def generate_completion(
    system: str,
    user: str,
    *,
    max_tokens: int = 2000,
    temperature: float = 0.8,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> str:
    provider = (provider or _get_provider()).lower()
    model = model or _get_model(provider)

    if provider == "anthropic":
        return _anthropic_complete(system, user, model, max_tokens, temperature)
    if provider == "openai":
        return _openai_complete(system, user, model, max_tokens, temperature)
    raise AIError(f"Provedor desconhecido: {provider}")


def _anthropic_complete(system, user, model, max_tokens, temperature) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise AIError("ANTHROPIC_API_KEY não configurada no .env")
    try:
        from anthropic import Anthropic
    except ImportError as e:
        raise AIError("Pacote 'anthropic' não instalado. Rode: pip install anthropic") from e

    client = Anthropic(api_key=api_key)
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    parts = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text)
        elif isinstance(block, dict) and "text" in block:
            parts.append(block["text"])

    # Telemetria de uso
    usage = getattr(msg, "usage", None)
    in_t = getattr(usage, "input_tokens", 0) if usage else 0
    out_t = getattr(usage, "output_tokens", 0) if usage else 0
    _record_usage("anthropic", model, "generate_completion", in_t, out_t)

    return "".join(parts).strip()


def _openai_complete(system, user, model, max_tokens, temperature) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise AIError("OPENAI_API_KEY não configurada no .env")
    try:
        from openai import OpenAI
    except ImportError as e:
        raise AIError("Pacote 'openai' não instalado. Rode: pip install openai") from e

    client = OpenAI(api_key=api_key)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return (resp.choices[0].message.content or "").strip()


# ---------------------------------------------------------------------------
# Visão — análise de comprovantes de operação
# ---------------------------------------------------------------------------
_PROOF_SYSTEM_PROMPT = """Você é um validador de prints de histórico de operações de trading
(QXBroker, Quotex, IQ Option, Pocket Option, etc.).

A imagem geralmente é uma LISTA de operações ("Operaciones", "Histórico", "History"),
mostrando várias operações em linhas. Cada operação tem um valor de aposta na coluna
"Cantidad" / "Quantidade" (em USD).

Extraia APENAS estes campos em JSON:

- data_operacao: a data principal do dia operado em formato YYYY-MM-DD. Se as operações
  forem de dias diferentes, use a data mais recente. (null se não visível)
- valor_usd: SOMA NUMÉRICA de todos os valores na coluna "Cantidad" mostrados na imagem
  (em USD). Esse é o "volume operado" do dia. Não some "Beneficio" (lucro). Some apenas
  os valores de aposta/cantidad. Ignorar entradas que estejam faltando o valor.
- num_operacoes: quantas operações distintas você consegue contar no print
  (número inteiro, ou null se incerto)
- id_conta: identificador numérico da conta na plataforma (null se não visível)
- plataforma: nome da plataforma (qxbroker, quotex, iqoption, pocketoption, etc.) ou "desconhecida"
- ip: endereço IP visível ao lado das operações se houver, senão null
- tipo_conta: "real" se há indicação de "Cuenta real" / "Real" / "Live"; "demo" se "Cuenta demo";
  null se não dá pra distinguir
- confianca: "alta" se você conseguiu somar TODAS as Cantidades visíveis com clareza E o
  tipo_conta é "real"; "media" se alguma operação é ilegível ou tipo_conta indeciso;
  "baixa" se imagem cortada/borrada/não é histórico de operações
- valido: true se a imagem é histórico de operações de trading; false caso contrário

REGRAS:
1. Cuenta real e Cuenta demo são DIFERENTES — sempre identifique. Se for DEMO, marque
   tipo_conta="demo" e confianca="baixa" (a Liga só aceita Conta Real).
2. Some apenas a coluna "Cantidad" (valor da aposta), não "Beneficio" (lucro/prejuízo).
3. Se houver paginação visível (ex: "1/2", "Próximo"), avise em num_operacoes que pode
   estar incompleto.
4. Valores em outra moeda → converta pra USD aproximado se possível, senão null.
5. Não invente: se não consegue ler com clareza, prefira null e confianca="baixa".

Retorne SOMENTE JSON válido, sem texto antes ou depois."""


def _detect_mime_type(image_bytes: bytes) -> str:
    """Detecta o mime_type pela assinatura dos bytes."""
    if image_bytes.startswith(b"\x89PNG"):
        return "image/png"
    if image_bytes.startswith(b"\xFF\xD8"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF8"):
        return "image/gif"
    if image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def _extract_json(text: str) -> Optional[dict]:
    """Extrai o primeiro objeto JSON válido do texto retornado."""
    if not text:
        return None
    # Tenta parse direto
    try:
        return json.loads(text)
    except Exception:
        pass
    # Tenta extrair bloco JSON do texto
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


_ACCOUNT_SYSTEM_PROMPT = """Você é um EXTRATOR PRECISO de IDs de contas Quotex/QXBroker em screenshots.

PRIORIDADE #1: ENCONTRAR O ID DA CONTA. Procure EXAUSTIVAMENTE em toda a imagem.

Onde o ID aparece tipicamente:
- "ID: 12345678" (texto explícito)
- "#12345678" (depois de hashtag)
- "Alias #12345678" (campo Alias)
- Em campos de perfil/Datos personales
- Logo abaixo do email
- Ao lado do avatar do usuário
- Topo da tela em apps mobile
- Em URLs do navegador (qxbroker.com/...)

Características:
- 7 a 9 dígitos consecutivos (raramente menos, raramente mais)
- NUNCA confunda com: número de telefone (12+ dígitos), CPF (11 dígitos formatado), data, valor monetário
- Se vir vários números, escolha o que parece mais um identificador (geralmente perto da palavra "ID", "Alias", ou junto ao perfil)

Sua tarefa: extrair em JSON:

- id_conta: APENAS DÍGITOS do ID encontrado. Se não conseguir ver com certeza, retorne null. NUNCA invente.
- email: email visível ou null.
- saldo_real_usd: USD da CONTA REAL ("Cuenta real", "Real", "Live", "EN DIRECTO"). NÃO é a Demo. Aceita $0.00.
- saldo_demo_usd: USD da CONTA DEMO ("Cuenta demo", "Demo"). Geralmente $10.000.
- plataforma: "quotex", "qxbroker", "iqoption", "pocketoption", "outros" ou "desconhecida".
- confianca: "alta" (id_conta legível e indubitável) | "media" (id parcialmente legível ou tem dúvida entre 2 candidatos) | "baixa" (não conseguiu identificar id_conta).
- valido: true SE id_conta foi extraído OU saldo_real foi visível. False só se a imagem claramente NÃO é screenshot de conta de trading.

REGRAS CRÍTICAS:
1. Procure o ID em TODA a imagem antes de retornar null. Olhe topo, meio, base, sidebar, popups.
2. Se só vê o número claro mas sem rótulo "ID", ASSUMA que é o ID se encaixar no padrão (7-9 dígitos perto do perfil).
3. Cuenta real ≠ Cuenta demo — distinga sempre (bolinha, rótulo, posição).
4. saldo_real=$0.00 é válido (conta criada sem depósito).
5. Imagem com QUALIDADE BAIXA mas você consegue ler ID → confianca="media", id_conta=preenchido.
6. Imagem completamente irrelevante (foto de paisagem, etc) → valido=false, id_conta=null.

Retorne SOMENTE JSON válido, sem texto antes/depois."""


def analyze_account_screenshot(image_bytes: bytes, lead_id: Optional[int] = None) -> dict:
    """Analisa screenshot da tela de conta na plataforma (QXBroker, Quotex, etc.).

    Cache por SHA256 da imagem — se já analisamos antes, retorna o resultado salvo
    e adiciona "_cached": True. Não bate na API novamente.

    Retorna dict com:
      - id_conta: str | None (apenas dígitos)
      - email: str | None
      - saldo_real_usd: float | None
      - saldo_demo_usd: float | None
      - plataforma: str
      - confianca: "alta" | "media" | "baixa"
      - valido: bool
      - _cached: bool (True se veio do cache, ausente se rodou IA)
      - _image_hash: str (SHA256, pra anti-fraude)

    Em caso de erro: {"valido": False, "confianca": "baixa", "erro": ...}
    """
    if not image_bytes:
        return {"valido": False, "confianca": "baixa", "erro": "imagem vazia"}

    img_hash = _image_hash(image_bytes)

    # Cache hit?
    cached = _cache_lookup(img_hash, "account_screenshot")
    if cached:
        cached["_cached"] = True
        cached["_image_hash"] = img_hash
        _record_usage("anthropic", "cache", "analyze_account_screenshot", 0, 0, cached=True, lead_id=lead_id)
        logger.info("[account] cache HIT hash=%s...", img_hash[:12])
        return cached

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("[account] ANTHROPIC_API_KEY não configurada")
        return {"valido": False, "confianca": "baixa", "erro": "ANTHROPIC_API_KEY ausente",
                "_image_hash": img_hash}

    try:
        from anthropic import Anthropic
    except ImportError as e:
        return {"valido": False, "confianca": "baixa", "erro": f"anthropic não instalado: {e}",
                "_image_hash": img_hash}

    try:
        mime_type = _detect_mime_type(image_bytes)
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=600,
            temperature=0.0,
            system=_ACCOUNT_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analise esta captura de conta na plataforma de trading e retorne o JSON conforme instruído.",
                    },
                ],
            }],
        )

        parts = []
        for block in msg.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        raw = "".join(parts).strip()

        data = _extract_json(raw)
        if not data:
            logger.warning("[account] resposta sem JSON: %s", raw[:200])
            return {"valido": False, "confianca": "baixa", "erro": "JSON inválido", "raw": raw}

        # Normalização
        data.setdefault("id_conta", None)
        data.setdefault("email", None)
        data.setdefault("saldo_real_usd", None)
        data.setdefault("saldo_demo_usd", None)
        data.setdefault("plataforma", "desconhecida")
        data.setdefault("confianca", "baixa")
        data.setdefault("valido", False)

        # Coerção numérica
        for k in ("saldo_real_usd", "saldo_demo_usd"):
            v = data.get(k)
            if isinstance(v, str):
                try:
                    data[k] = float(v.replace(",", "").replace("$", "").strip())
                except Exception:
                    data[k] = None

        # Limpa id_conta — apenas dígitos
        if data.get("id_conta"):
            digits = "".join(ch for ch in str(data["id_conta"]) if ch.isdigit())
            data["id_conta"] = digits or None

        logger.info(
            "[account] vision haiku: valido=%s conf=%s id=%s real=%s demo=%s plat=%s",
            data.get("valido"), data.get("confianca"),
            data.get("id_conta"), data.get("saldo_real_usd"),
            data.get("saldo_demo_usd"), data.get("plataforma"),
        )

        # Telemetria
        usage = getattr(msg, "usage", None)
        in_t = getattr(usage, "input_tokens", 0) if usage else 0
        out_t = getattr(usage, "output_tokens", 0) if usage else 0
        _record_usage("anthropic", model, "analyze_account_screenshot", in_t, out_t, lead_id=lead_id)

        # ═══ FALLBACK SONNET ═══════════════════════════════════════════════
        # Se Haiku NÃO detectou id_conta MAS imagem parece válida (saldo
        # detectado ou plataforma reconhecida), tenta Sonnet — modelo mais
        # forte pra texto pequeno em screenshots. Custa mais (~$0.005/img)
        # mas evita validação manual desnecessária.
        haiku_failed_id = (
            not data.get("id_conta")
            and (
                data.get("saldo_real_usd") is not None
                or data.get("saldo_demo_usd") is not None
                or (data.get("plataforma") and data.get("plataforma") != "desconhecida")
            )
        )
        if haiku_failed_id:
            logger.info("[account] Haiku NÃO detectou id_conta mas imagem parece válida — tentando Sonnet")
            try:
                sonnet_model = os.getenv("ANTHROPIC_VISION_FALLBACK_MODEL", "claude-sonnet-4-5-20250929")
                msg2 = client.messages.create(
                    model=sonnet_model,
                    max_tokens=600,
                    temperature=0.0,
                    system=_ACCOUNT_SYSTEM_PROMPT,
                    messages=[{
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": mime_type,
                                    "data": b64,
                                },
                            },
                            {
                                "type": "text",
                                "text": "Analise esta captura de conta e EXTRAIA O ID. O modelo anterior falhou em detectar o ID — você é o backup, procure exaustivamente.",
                            },
                        ],
                    }],
                )
                parts2 = []
                for block in msg2.content:
                    if hasattr(block, "text"):
                        parts2.append(block.text)
                    elif isinstance(block, dict) and "text" in block:
                        parts2.append(block["text"])
                raw2 = "".join(parts2).strip()
                data2 = _extract_json(raw2)
                usage2 = getattr(msg2, "usage", None)
                in_t2 = getattr(usage2, "input_tokens", 0) if usage2 else 0
                out_t2 = getattr(usage2, "output_tokens", 0) if usage2 else 0
                _record_usage("anthropic", sonnet_model, "analyze_account_screenshot_fallback", in_t2, out_t2, lead_id=lead_id)

                if data2 and data2.get("id_conta"):
                    digits = "".join(ch for ch in str(data2["id_conta"]) if ch.isdigit())
                    if digits:
                        logger.info(
                            "[account] Sonnet RESGATOU id_conta=%s (Haiku tinha falhado)",
                            digits,
                        )
                        data["id_conta"] = digits
                        # Se Sonnet retornou outros campos com mais info, preenche também
                        for k in ("saldo_real_usd", "saldo_demo_usd", "email", "plataforma"):
                            if data2.get(k) is not None and not data.get(k):
                                data[k] = data2[k]
                        # Confiança vira 'media' (Haiku falhou mas Sonnet pegou)
                        data["confianca"] = "media"
                        data["valido"] = True
                        data["_fallback_used"] = "sonnet"
                else:
                    logger.warning("[account] Sonnet também não detectou id_conta")
            except Exception:
                logger.exception("[account] erro no fallback Sonnet")

        # Cache final (com possível atualização do Sonnet)
        _cache_store(img_hash, "account_screenshot", data, lead_id=lead_id)

        data["_cached"] = False
        data["_image_hash"] = img_hash
        return data
    except Exception as e:
        logger.exception("[account] erro chamando Claude Vision")
        return {"valido": False, "confianca": "baixa", "erro": str(e), "_image_hash": img_hash}


def analyze_proof_image(image_bytes: bytes, lead_id: Optional[int] = None) -> dict:
    """Analisa uma imagem de comprovante usando Claude Vision.

    Retorna dict com:
      - data_operacao: str | None  (formato YYYY-MM-DD)
      - valor_usd: float | None
      - id_conta: str | None
      - plataforma: str
      - confianca: "alta" | "media" | "baixa"
      - valido: bool

    Em caso de erro, retorna {"valido": False, "confianca": "baixa", "erro": ...}
    """
    if not image_bytes:
        return {"valido": False, "confianca": "baixa", "erro": "imagem vazia"}

    img_hash = _image_hash(image_bytes)

    # Cache hit?
    cached = _cache_lookup(img_hash, "proof_image")
    if cached:
        cached["_cached"] = True
        cached["_image_hash"] = img_hash
        _record_usage("anthropic", "cache", "analyze_proof_image", 0, 0, cached=True, lead_id=lead_id)
        logger.info("[proof] cache HIT hash=%s...", img_hash[:12])
        return cached

    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        logger.warning("[proof] ANTHROPIC_API_KEY não configurada — não é possível validar comprovante")
        return {"valido": False, "confianca": "baixa", "erro": "ANTHROPIC_API_KEY ausente",
                "_image_hash": img_hash}

    try:
        from anthropic import Anthropic
    except ImportError as e:
        return {"valido": False, "confianca": "baixa", "erro": f"anthropic não instalado: {e}",
                "_image_hash": img_hash}

    try:
        mime_type = _detect_mime_type(image_bytes)
        b64 = base64.standard_b64encode(image_bytes).decode("ascii")
        model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")

        client = Anthropic(api_key=api_key)
        msg = client.messages.create(
            model=model,
            max_tokens=600,
            temperature=0.0,
            system=_PROOF_SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": mime_type,
                            "data": b64,
                        },
                    },
                    {
                        "type": "text",
                        "text": "Analise este comprovante de operação de trading e retorne o JSON conforme instruído.",
                    },
                ],
            }],
        )

        parts = []
        for block in msg.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif isinstance(block, dict) and "text" in block:
                parts.append(block["text"])
        raw = "".join(parts).strip()

        data = _extract_json(raw)
        if not data:
            logger.warning("[proof] resposta sem JSON parseável: %s", raw[:200])
            return {"valido": False, "confianca": "baixa", "erro": "JSON inválido", "raw": raw}

        # Normaliza campos
        data.setdefault("data_operacao", None)
        data.setdefault("valor_usd", None)
        data.setdefault("num_operacoes", None)
        data.setdefault("id_conta", None)
        data.setdefault("plataforma", "desconhecida")
        data.setdefault("ip", None)
        data.setdefault("tipo_conta", None)
        data.setdefault("confianca", "baixa")
        data.setdefault("valido", False)

        # Coerção de valor_usd para float (vem string às vezes)
        v = data.get("valor_usd")
        if isinstance(v, str):
            try:
                data["valor_usd"] = float(v.replace(",", "").replace("$", "").strip())
            except Exception:
                data["valor_usd"] = None

        # Limpa id_conta — apenas dígitos
        if data.get("id_conta"):
            digits = "".join(ch for ch in str(data["id_conta"]) if ch.isdigit())
            data["id_conta"] = digits or None

        logger.info(
            "[proof] vision: valido=%s conf=%s tipo=%s valor=%s ops=%s id=%s ip=%s",
            data.get("valido"), data.get("confianca"), data.get("tipo_conta"),
            data.get("valor_usd"), data.get("num_operacoes"),
            data.get("id_conta"), data.get("ip"),
        )

        # Telemetria + cache
        usage = getattr(msg, "usage", None)
        in_t = getattr(usage, "input_tokens", 0) if usage else 0
        out_t = getattr(usage, "output_tokens", 0) if usage else 0
        _record_usage("anthropic", model, "analyze_proof_image", in_t, out_t, lead_id=lead_id)
        _cache_store(img_hash, "proof_image", data, lead_id=lead_id)

        data["_cached"] = False
        data["_image_hash"] = img_hash
        return data
    except Exception as e:
        logger.exception("[proof] erro chamando Claude Vision")
        return {"valido": False, "confianca": "baixa", "erro": str(e), "_image_hash": img_hash}
