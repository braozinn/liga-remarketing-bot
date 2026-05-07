"""Modelos do banco - dois modos:
   - "forward": encaminha mensagens existentes (drop_author=True)
   - "ai": envia texto gerado por IA, editável (variantes A/B)
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from .database import Base


class LeadStatus(str, PyEnum):
    PENDING = "pending"             # nunca recebeu mensagem
    CONTACTED = "contacted"         # recebeu script(s) — sem resposta
    REPLIED = "replied"             # respondeu (qualquer tipo)
    BLOCKED = "blocked"             # bloqueou ou pediu pra parar
    EXCLUDED = "excluded"           # já no grupo privado, nunca contactar


class CampaignStatus(str, PyEnum):
    DRAFT = "draft"
    SCHEDULED = "scheduled"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class SendStatus(str, PyEnum):
    QUEUED = "queued"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


class ScriptMode(str, PyEnum):
    FORWARD = "forward"   # encaminha mensagens
    AI = "ai"             # envia texto gerado/editado


class LigaState(str, PyEnum):
    NEW              = "new"
    ONBOARDING       = "onboarding"
    WAITING_ID       = "waiting_id"
    WAITING_DEPOSIT  = "waiting_deposit"
    WAITLIST         = "waitlist"        # depósito < $100
    ACTIVE           = "active"          # depósito >= $100, enviando provas
    AT_RISK          = "at_risk"         # abaixo de $100 no checkpoint
    ELIMINATED       = "eliminated"      # falhou checkpoint
    FINALIST         = "finalist"        # top 3 na validação final


class RemarketingStage(str, PyEnum):
    """Estado do lead no funil de remarketing — informacional.
    Bot atualiza automático quando você dispara scripts manualmente."""
    UNTOUCHED          = "untouched"           # nunca recebeu nada
    R1_SENT_COOLDOWN   = "r1_sent_cooldown"    # R1 enviada, em cooldown 24h
    R1_COLD            = "r1_cold"             # R1 expirou sem resposta — elegível R2
    R2_SENT_COOLDOWN   = "r2_sent_cooldown"    # R2 enviada, em cooldown 7d
    R2_COLD            = "r2_cold"             # R2 expirou — elegível R3
    R3_SENT_COOLDOWN   = "r3_sent_cooldown"    # R3 enviada, último cooldown 14d
    DISCARDED          = "discarded"           # 3 disparos sem resposta — arquivado
    REPLIED            = "replied"             # respondeu em algum momento
    CONVERTED          = "converted"           # entrou no grupo privado
    OPTED_OUT          = "opted_out"           # pediu pra parar


# ---------------------------------------------------------------------------
# Lead
# ---------------------------------------------------------------------------
class Lead(Base):
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True, index=True, nullable=False)
    username = Column(String(100), index=True)
    first_name = Column(String(200))
    last_name = Column(String(200))
    phone = Column(String(50))

    status = Column(String(30), default=LeadStatus.PENDING.value, index=True)
    source = Column(String(50), default="dm_history")  # dm_history | leads_group
    notes = Column(Text)

    last_dm_at = Column(DateTime)
    in_private_group = Column(Boolean, default=False, index=True)
    in_leads_group = Column(Boolean, default=False, index=True)

    # VIP / opt-out / re-warming
    is_vip_potential   = Column(Boolean, default=False, index=True)  # detectado por saldo/depósitos altos
    opted_out          = Column(Boolean, default=False, index=True)  # disse "stop", nunca enviar
    opted_out_at       = Column(DateTime)
    rewarm_candidate   = Column(Boolean, default=False, index=True)  # tinha saldo, perdeu — re-engajar
    last_revalidated_at = Column(DateTime)                            # última vez que partner bot foi consultado

    # Remarketing stage — funil de rodadas de disparo
    remarketing_stage  = Column(String(30), default="untouched", index=True)
    r1_sent_at         = Column(DateTime)
    r2_sent_at         = Column(DateTime)
    r3_sent_at         = Column(DateTime)
    is_fresh           = Column(Boolean, default=False, index=True)  # tem msg do lead nas últimas 24h
    discard_at         = Column(DateTime)                            # quando seria descartado se não responder

    # Liga — estado da jornada
    liga_state        = Column(String(30), default="new", index=True)
    liga_account_id   = Column(String(100))           # ID da conta na plataforma
    liga_id_status    = Column(String(20), index=True) # validated|invalid|needs_review|extracted|null
    liga_id_country   = Column(String(50))             # País (do @QuotexPartnerBot)
    liga_id_balance   = Column(Float)                  # Banca real (do partner bot)
    liga_id_deposits_sum = Column(Float)               # Total depositado (do partner bot)
    liga_id_turnover  = Column(Float)                  # Turnover (do partner bot)
    liga_id_validated_at = Column(DateTime)            # Quando foi validado pela última vez
    liga_id_partner_response = Column(Text)            # resposta crua do partner bot
    liga_balance      = Column(Float, default=0.0)     # saldo/banca declarada (screenshot)
    # Categoria de engajamento (pra decidir qual remarketing enviar)
    # Valores: first_contact_no_reply | account_no_deposit | remarketed_no_account
    #          | deposit_promised | deposited | None
    engagement_tag    = Column(String(40), index=True)
    engagement_tag_updated_at = Column(DateTime)
    engagement_evidence = Column(Text)  # JSON: {match: "...", date: "..."} pra explicar por que classificou
    proof_sent_today  = Column(Boolean, default=False) # reset todo dia
    lead_score        = Column(Integer, default=0)     # score de prioridade
    last_bot_action   = Column(String(100))            # última ação do bot
    conversation_ctx  = Column(Text)                   # JSON: últimas 5 msgs
    streak_days       = Column(Integer, default=0)     # dias consecutivos com prova

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sends = relationship("Send", back_populates="lead", cascade="all,delete")

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        full = f"{self.first_name or ''} {self.last_name or ''}".strip()
        return full or f"id:{self.telegram_id}"


# ---------------------------------------------------------------------------
# Script
# ---------------------------------------------------------------------------
class Script(Base):
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    mode = Column(String(20), default=ScriptMode.FORWARD.value, nullable=False)

    # Modo "forward": briefing_pt vira só descrição interna
    # Modo "ai": briefing_pt é o briefing pra IA gerar variantes em ES
    briefing_pt = Column(Text, default="")
    objective = Column(String(200))
    target_status = Column(String(30), default=LeadStatus.PENDING.value)
    target_remarketing_stage = Column(String(30))  # filtra leads pelo stage no envio
    target_engagement_tag = Column(String(40))     # filtra por engagement_tag (opcional)
    is_active = Column(Boolean, default=True)

    # Métricas agregadas
    sends_count = Column(Integer, default=0)
    replies_count = Column(Integer, default=0)
    positive_count = Column(Integer, default=0)
    conversions_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    sources = relationship(
        "ScriptSource", back_populates="script", cascade="all,delete",
        order_by="ScriptSource.order_index",
        lazy="selectin",  # carrega junto da query, evita DetachedInstanceError
    )
    variants = relationship(
        "ScriptVariant", back_populates="script", cascade="all,delete",
        order_by="ScriptVariant.id",
        lazy="selectin",
    )
    media_items = relationship(
        "ScriptMedia", back_populates="script", cascade="all,delete",
        order_by="ScriptMedia.order_index",
        lazy="selectin",
    )
    campaigns = relationship("Campaign", back_populates="script", cascade="all,delete")
    sends = relationship("Send", back_populates="script")

    @property
    def reply_rate(self) -> float:
        return (self.replies_count / self.sends_count) if self.sends_count else 0.0

    @property
    def positive_rate(self) -> float:
        return (self.positive_count / self.sends_count) if self.sends_count else 0.0

    @property
    def conversion_rate(self) -> float:
        return (self.conversions_count / self.sends_count) if self.sends_count else 0.0

    def score(self, w_reply=1.0, w_pos=2.0, w_conv=5.0) -> float:
        return (
            self.reply_rate * w_reply
            + self.positive_rate * w_pos
            + self.conversion_rate * w_conv
        )


# ---------------------------------------------------------------------------
# ScriptSource - mensagens fonte (modo forward)
# ---------------------------------------------------------------------------
class ScriptSource(Base):
    __tablename__ = "script_sources"

    id = Column(Integer, primary_key=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    source_link = Column(String(500))
    source_chat = Column(String(200), nullable=False)
    source_message_id = Column(Integer, nullable=False)
    drop_author = Column(Boolean, default=True)
    order_index = Column(Integer, default=0)
    note = Column(String(300))
    created_at = Column(DateTime, default=datetime.utcnow)

    script = relationship("Script", back_populates="sources")


# ---------------------------------------------------------------------------
# ScriptMedia - arquivos de mídia (vídeo/foto/áudio) anexados ao script
# ---------------------------------------------------------------------------
class ScriptMedia(Base):
    __tablename__ = "script_media"

    id = Column(Integer, primary_key=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    filename = Column(String(300), nullable=False)         # nome no disco (uuid + ext)
    original_name = Column(String(300))                    # nome original do upload
    mime_type = Column(String(100))
    kind = Column(String(20))                              # image | video | audio | document
    size_bytes = Column(Integer)
    caption = Column(Text)                                 # legenda opcional
    order_index = Column(Integer, default=0)               # ordem de envio
    send_before_text = Column(Boolean, default=False)      # se True, envia ANTES do texto
    video_note = Column(Boolean, default=False)            # vídeo bolinha (circular)
    created_at = Column(DateTime, default=datetime.utcnow)

    script = relationship("Script", back_populates="media_items")


# ---------------------------------------------------------------------------
# ScriptVariant - texto editável em ES (modo ai)
# ---------------------------------------------------------------------------
class ScriptVariant(Base):
    __tablename__ = "script_variants"

    id = Column(Integer, primary_key=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    label = Column(String(50), nullable=False)
    text_es = Column(Text, nullable=False)
    ai_provider = Column(String(20))  # anthropic | openai | manual
    ai_model = Column(String(80))
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Métricas por variante
    sends_count = Column(Integer, default=0)
    replies_count = Column(Integer, default=0)
    positive_count = Column(Integer, default=0)
    conversions_count = Column(Integer, default=0)

    script = relationship("Script", back_populates="variants")
    sends = relationship("Send", back_populates="variant")

    @property
    def reply_rate(self) -> float:
        return (self.replies_count / self.sends_count) if self.sends_count else 0.0

    @property
    def positive_rate(self) -> float:
        return (self.positive_count / self.sends_count) if self.sends_count else 0.0

    @property
    def conversion_rate(self) -> float:
        return (self.conversions_count / self.sends_count) if self.sends_count else 0.0

    def score(self, w_reply=1.0, w_pos=2.0, w_conv=5.0) -> float:
        return (
            self.reply_rate * w_reply
            + self.positive_rate * w_pos
            + self.conversion_rate * w_conv
        )


# ---------------------------------------------------------------------------
# Campaign
# ---------------------------------------------------------------------------
class Campaign(Base):
    __tablename__ = "campaigns"

    id = Column(Integer, primary_key=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False)
    name = Column(String(200))
    status = Column(String(30), default=CampaignStatus.DRAFT.value, index=True)
    scheduled_at = Column(DateTime)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)
    target_status = Column(String(30), default=LeadStatus.PENDING.value)
    max_leads = Column(Integer, default=0)
    # Em ai mode: rotate (round-robin) | best (a com melhor score)
    variant_strategy = Column(String(20), default="rotate")
    notes = Column(Text)
    # VIPs ficam fora dos disparos em massa por padrão — recebem tratamento manual
    # via /vip-outreach. Marca como False só em casos específicos.
    exclude_vips = Column(Boolean, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    script = relationship("Script", back_populates="campaigns", lazy="joined")
    sends = relationship("Send", back_populates="campaign", cascade="all,delete")


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------
class Send(Base):
    __tablename__ = "sends"

    id = Column(Integer, primary_key=True)
    campaign_id = Column(Integer, ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False)
    lead_id = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False)
    variant_id = Column(Integer, ForeignKey("script_variants.id"), nullable=True)  # só ai mode

    message_text = Column(Text)  # texto exato enviado (ai mode) ou null (forward)
    telegram_message_ids = Column(String(500))
    status = Column(String(20), default=SendStatus.QUEUED.value, index=True)
    error = Column(Text)
    sent_at = Column(DateTime)
    queued_at = Column(DateTime, default=datetime.utcnow)

    replied = Column(Boolean, default=False)
    replied_at = Column(DateTime)
    reply_text = Column(Text)
    reply_classification = Column(String(30))

    campaign = relationship("Campaign", back_populates="sends")
    lead = relationship("Lead", back_populates="sends", lazy="joined")
    script = relationship("Script", back_populates="sends", lazy="joined")
    variant = relationship("ScriptVariant", back_populates="sends", lazy="joined")


class Setting(Base):
    __tablename__ = "settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ---------------------------------------------------------------------------
# Liga — comprovantes, volume diário e objeções
# ---------------------------------------------------------------------------
class OperationProof(Base):
    __tablename__ = "operation_proofs"

    id              = Column(Integer, primary_key=True)
    lead_id         = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    proof_date      = Column(String(10), nullable=False)   # "YYYY-MM-DD"
    volume_usd      = Column(Float, default=0.0)
    account_id_raw  = Column(String(100))                  # extraído da imagem
    platform        = Column(String(100))
    confidence      = Column(String(10))                   # alta | media | baixa
    image_path      = Column(String(500))
    validated       = Column(Boolean, default=False)
    raw_ai_response = Column(Text)                         # JSON completo da IA
    created_at      = Column(DateTime, default=datetime.utcnow)

    # ---- Revisão humana (anti-perda durante torneio) ----
    needs_review    = Column(Boolean, default=False, index=True)  # entra em /verifications/pending
    review_reason   = Column(String(40))                          # vision_failed | low_confidence | duplicate_image | id_mismatch | manual_input
    validated_by    = Column(String(20))                          # ai | human | human_after_ai_fail
    validated_at    = Column(DateTime)                            # quando humano confirmou (manual)
    review_notes    = Column(Text)                                # anotações livres do admin

    lead = relationship("Lead", backref="proofs")


class DailyVolume(Base):
    __tablename__ = "daily_volume"

    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    date       = Column(String(10), nullable=False)    # "YYYY-MM-DD"
    volume_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead")


class Objection(Base):
    __tablename__ = "objections"

    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    text       = Column(Text)
    category   = Column(String(100))   # "preco" | "tempo" | "desconfianca" | "outro"
    response_used = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead")


# ---------------------------------------------------------------------------
# LeadMessage — histórico completo de DMs (in/out) por lead
# ---------------------------------------------------------------------------
class LeadMessage(Base):
    __tablename__ = "lead_messages"

    id              = Column(Integer, primary_key=True)
    lead_id         = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    direction       = Column(String(4), nullable=False, index=True)  # 'in' | 'out'
    kind            = Column(String(20), default="text")             # text|audio|image|document|sticker|video|other
    content         = Column(Text)                                    # texto da msg ou transcrição de áudio
    media_path      = Column(String(500))                             # se for áudio/imagem salvo no disco
    duration_sec    = Column(Integer)                                 # se for áudio
    telegram_msg_id = Column(Integer, index=True)
    classified_as   = Column(String(40))                              # opt_out|deposit_promised|question|null
    created_at      = Column(DateTime, default=datetime.utcnow, index=True)

    lead = relationship("Lead")


# ---------------------------------------------------------------------------
# BalanceSnapshot — histórico de saldo do lead ao longo do tempo
# ---------------------------------------------------------------------------
class BalanceSnapshot(Base):
    __tablename__ = "balance_snapshots"

    id              = Column(Integer, primary_key=True)
    lead_id         = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    balance         = Column(Float, default=0.0)
    deposits_sum    = Column(Float, default=0.0)
    turnover        = Column(Float, default=0.0)
    source          = Column(String(20))   # partner_bot | screenshot | manual
    snapshot_at     = Column(DateTime, default=datetime.utcnow, index=True)

    lead = relationship("Lead")


# ---------------------------------------------------------------------------
# AIUsage — telemetria de gasto com Claude / OpenAI
# ---------------------------------------------------------------------------
class AIUsage(Base):
    __tablename__ = "ai_usage"

    id            = Column(Integer, primary_key=True)
    provider      = Column(String(20))   # anthropic | openai
    model         = Column(String(60))
    operation     = Column(String(50))   # generate_completion | analyze_proof_image | analyze_account_screenshot
    input_tokens  = Column(Integer, default=0)
    output_tokens = Column(Integer, default=0)
    cost_usd      = Column(Float, default=0.0)
    cached        = Column(Boolean, default=False)  # se foi cache hit (token=0)
    lead_id       = Column(Integer, ForeignKey("leads.id"), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, index=True)


# ---------------------------------------------------------------------------
# ImageCache — cache de análise de imagem por hash (anti-fraude + economia)
# ---------------------------------------------------------------------------
class ImageCache(Base):
    __tablename__ = "image_cache"

    id            = Column(Integer, primary_key=True)
    image_hash    = Column(String(64), unique=True, index=True, nullable=False)  # SHA256 hex
    operation     = Column(String(50))   # account_screenshot | proof_image
    raw_response  = Column(Text)         # JSON da resposta da IA
    first_lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True)
    seen_count    = Column(Integer, default=1)  # quantas vezes essa imagem apareceu
    created_at    = Column(DateTime, default=datetime.utcnow)
    last_seen_at  = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# ScriptExcludedLead — exclusão manual de leads de um script específico
# ---------------------------------------------------------------------------
# Permite ao admin remover leads específicos do disparo (via /scripts/X/preview-dispatch)
# antes de confirmar a campanha. Persiste — se o lead for excluído, NUNCA recebe
# esse script via campanha ou disparar (até ser re-incluído manualmente).
class ScriptExcludedLead(Base):
    __tablename__ = "script_excluded_leads"

    id          = Column(Integer, primary_key=True)
    script_id   = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    lead_id     = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    reason      = Column(String(200))   # opcional: 'manual', 'vip', 'duplicate', etc
    created_at  = Column(DateTime, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# FollowUpRule — disparos automáticos por categoria de engajamento
# ---------------------------------------------------------------------------
class FollowUpRule(Base):
    __tablename__ = "follow_up_rules"

    id               = Column(Integer, primary_key=True)
    name             = Column(String(200), nullable=False)
    target_engagement_tag = Column(String(40), nullable=False, index=True)
    min_days_idle    = Column(Integer, default=3)   # cooldown antes de disparar
    script_id        = Column(Integer, ForeignKey("scripts.id"))
    max_sends_per_lead = Column(Integer, default=1) # quantas vezes pode disparar pro mesmo lead
    is_active        = Column(Boolean, default=True, index=True)
    last_run_at      = Column(DateTime)
    sent_count       = Column(Integer, default=0)
    created_at       = Column(DateTime, default=datetime.utcnow)

    script = relationship("Script")
