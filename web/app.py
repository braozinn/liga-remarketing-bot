"""Painel web FastAPI - PT-BR. Modos: forward + ai."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

BRT = ZoneInfo("America/Sao_Paulo")

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import uuid as _uuid
import mimetypes
from starlette.middleware.base import BaseHTTPMiddleware
from sqlalchemy import desc, Integer

from ai import generate_script_variants, regenerate_from_winner, AIError
from db import SessionLocal, init_db
from db.models import (
    AIUsage,
    Campaign,
    CampaignStatus,
    DailyVolume,
    FollowUpRule,
    ImageCache,
    Lead,
    LeadStatus,
    LigaState,
    Objection,
    OperationProof,
    Script,
    ScriptMedia,
    ScriptMode,
    ScriptSource,
    ScriptVariant,
    Send,
    SendStatus,
    Setting,
)
from utils import parse_telegram_link
from userbot.leads import sync_leads_auto, sync_leads_from_dm_history, sync_leads_from_group
from userbot.scheduler import schedule_campaign, cancel_campaign, cancel_all_campaigns
from userbot.sender import send_test_to_username

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "web" / "templates"
STATIC_DIR = ROOT / "web" / "static"


class SimpleAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        password = os.getenv("WEB_PASSWORD", "").strip()
        if not password:
            return await call_next(request)
        path = request.url.path
        if path == "/login" or path == "/api/login" or path.startswith("/static/"):
            return await call_next(request)
        token = request.cookies.get("session_token")
        if token == _expected_token(password):
            return await call_next(request)
        if path.startswith("/api/"):
            return JSONResponse({"error": "auth required"}, status_code=401)
        return RedirectResponse("/login")


class NoCacheStaticMiddleware(BaseHTTPMiddleware):
    """Em dev, força reload do CSS/JS — evita o sufoco do cache do navegador."""
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


def _expected_token(password: str) -> str:
    return hashlib.sha256(f"cowork-bot::{password}".encode()).hexdigest()


VALID_UI_MODES = ("torneio", "normal")


def get_ui_mode() -> str:
    """Lê o modo da UI da tabela settings. Default: 'torneio'."""
    try:
        with SessionLocal() as s:
            row = s.query(Setting).filter_by(key="ui_mode").one_or_none()
            if row and row.value in VALID_UI_MODES:
                return row.value
    except Exception:
        pass
    return "torneio"


def get_pending_verifications_count() -> int:
    """Retorna quantos comprovantes precisam revisão humana. Pra badge no menu."""
    try:
        with SessionLocal() as s:
            return s.query(OperationProof).filter(
                OperationProof.needs_review.is_(True)
            ).count()
    except Exception:
        return 0


def is_tournament_active_helper() -> bool:
    """Wrapper pra usar nos templates."""
    try:
        from liga.notifications import is_tournament_active
        return is_tournament_active()
    except Exception:
        return False


def set_ui_mode(mode: str) -> None:
    if mode not in VALID_UI_MODES:
        raise ValueError(f"Modo inválido: {mode}")
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key="ui_mode").one_or_none()
        if row:
            row.value = mode
        else:
            s.add(Setting(key="ui_mode", value=mode))
        s.commit()


def create_app() -> FastAPI:
    init_db()
    app = FastAPI(title="Bot de Remarketing Telegram", docs_url=None, redoc_url=None)
    app.add_middleware(NoCacheStaticMiddleware)
    app.add_middleware(SimpleAuthMiddleware)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    MEDIA_DIR = ROOT / "media"
    MEDIA_DIR.mkdir(exist_ok=True)
    app.mount("/media", StaticFiles(directory=str(MEDIA_DIR)), name="media")
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    # Disponibiliza helpers globais em todos os templates (usado em base.html)
    templates.env.globals["get_ui_mode"] = get_ui_mode
    templates.env.globals["get_pending_verifications_count"] = get_pending_verifications_count
    templates.env.globals["is_tournament_active"] = is_tournament_active_helper

    # Handler global de erro: mostra a causa real em vez de "Internal Server Error"
    import traceback as _tb
    from fastapi.exceptions import RequestValidationError
    from starlette.exceptions import HTTPException as StarletteHTTPException

    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc):
        if exc.status_code == 404:
            return HTMLResponse(
                f"<h1>404 - Não encontrado</h1><p>{exc.detail}</p><a href='/'>← Voltar</a>",
                status_code=404,
            )
        return HTMLResponse(
            f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>",
            status_code=exc.status_code,
        )

    @app.exception_handler(Exception)
    async def _global_exc(request: Request, exc: Exception):
        tb_text = _tb.format_exc()
        logger.exception("Erro 500 em %s %s", request.method, request.url.path)
        if str(request.url.path).startswith("/api/") or str(request.url.path).startswith("/testes/send/"):
            return JSONResponse(
                {"error": f"{type(exc).__name__}: {exc}", "traceback": tb_text[-2000:]},
                status_code=500,
            )
        # Página HTML legível com a causa do erro
        safe_tb = tb_text.replace("<", "&lt;").replace(">", "&gt;")
        body = f"""<!DOCTYPE html>
<html><head><title>Erro</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
</head><body class="p-4 bg-light">
<div class="container">
  <h2 class="text-danger">⚠️ Erro: {type(exc).__name__}</h2>
  <p class="lead">{str(exc)}</p>
  <p><strong>Rota:</strong> <code>{request.method} {request.url.path}</code></p>
  <details class="mt-4">
    <summary class="btn btn-outline-secondary btn-sm">Ver traceback completo</summary>
    <pre class="bg-dark text-light p-3 mt-2 rounded small" style="max-height:500px; overflow:auto;">{safe_tb}</pre>
  </details>
  <a href="/" class="btn btn-primary mt-4">← Voltar pro Dashboard</a>
  <p class="small text-muted mt-3">
    Esse erro também foi gravado nos logs do terminal onde o bot está rodando.
    Cola a mensagem da seção vermelha (<code>{type(exc).__name__}</code>) que ajusto.
  </p>
</div>
</body></html>"""
        return HTMLResponse(body, status_code=500)

    # ----------------------------- Auth
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        return templates.TemplateResponse("login.html", {"request": request})

    @app.post("/api/login")
    async def login_post(password: str = Form(...)):
        expected = os.getenv("WEB_PASSWORD", "").strip()
        if not expected or password != expected:
            return RedirectResponse("/login?err=1", status_code=302)
        resp = RedirectResponse("/", status_code=302)
        resp.set_cookie("session_token", _expected_token(expected), httponly=True, samesite="lax")
        return resp

    @app.get("/logout")
    async def logout():
        resp = RedirectResponse("/login")
        resp.delete_cookie("session_token")
        return resp

    # ----------------------------- Dashboard
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        from sqlalchemy import func
        mode = get_ui_mode()
        with SessionLocal() as s:
            stats = {
                "leads_total": s.query(Lead).count(),
                "leads_pending": s.query(Lead).filter(Lead.status == LeadStatus.PENDING.value).count(),
                "leads_contacted": s.query(Lead).filter(Lead.status == LeadStatus.CONTACTED.value).count(),
                "leads_replied": s.query(Lead).filter(Lead.status == LeadStatus.REPLIED.value).count(),
                "leads_in_private": s.query(Lead).filter(Lead.in_private_group.is_(True)).count(),
                "leads_excluded": s.query(Lead).filter(Lead.status == LeadStatus.EXCLUDED.value).count(),
                "leads_blocked": s.query(Lead).filter(Lead.status == LeadStatus.BLOCKED.value).count(),
                "scripts_total": s.query(Script).count(),
                "campaigns_running": s.query(Campaign).filter(Campaign.status == CampaignStatus.RUNNING.value).count(),
                "campaigns_scheduled": s.query(Campaign).filter(Campaign.status == CampaignStatus.SCHEDULED.value).count(),
                "sends_today": s.query(Send).filter(
                    Send.sent_at != None,  # noqa: E711
                    Send.sent_at >= datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0),
                ).count(),
            }
            recent_campaigns = s.query(Campaign).order_by(desc(Campaign.created_at)).limit(5).all()
            top_scripts = s.query(Script).filter(Script.sends_count > 0).all()
            top_scripts = sorted(top_scripts, key=lambda x: x.score(), reverse=True)[:5]

        if mode != "torneio":
            return templates.TemplateResponse(
                "dashboard.html",
                {"request": request, "stats": stats,
                 "recent_campaigns": recent_campaigns, "top_scripts": top_scripts},
            )

        # --- Modo torneio: agrega Liga + remarketing -----------------------
        with SessionLocal() as s:
            state_counts = dict(
                s.query(Lead.liga_state, func.count(Lead.id))
                .group_by(Lead.liga_state).all()
            )
            for st in ("new", "onboarding", "waiting_id", "waiting_deposit",
                       "waitlist", "active", "at_risk", "eliminated", "finalist"):
                state_counts.setdefault(st, 0)

            volume_total = float(s.query(func.sum(DailyVolume.volume_usd)).scalar() or 0.0)

            top_rows = (
                s.query(
                    Lead.id, Lead.first_name, Lead.last_name, Lead.username,
                    Lead.telegram_id, Lead.liga_state, Lead.streak_days,
                    func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).label("vol"),
                )
                .outerjoin(DailyVolume, DailyVolume.lead_id == Lead.id)
                .filter(Lead.liga_state.in_(["active", "at_risk", "finalist", "waitlist"]))
                .group_by(Lead.id)
                .order_by(func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).desc())
                .limit(5)
                .all()
            )
            top_ranking = []
            for r in top_rows:
                nome = (r.first_name or "") + (" " + r.last_name if r.last_name else "")
                nome = nome.strip() or (f"@{r.username}" if r.username else f"id:{r.telegram_id}")
                top_ranking.append({
                    "id": r.id, "name": nome, "username": r.username,
                    "state": r.liga_state, "streak": r.streak_days or 0,
                    "volume": float(r.vol or 0.0),
                })

            # Tags por categoria de lead (visão unificada)
            from liga.tags import get_liga_tag
            liga_active = (
                s.query(Lead).filter(Lead.liga_state.in_([
                    "active", "at_risk", "waitlist", "finalist",
                ])).all()
            )
            engaged_n = sum(1 for l in liga_active if get_liga_tag(l, s) == "engaged")
            slipping_n = sum(1 for l in liga_active if get_liga_tag(l, s) == "slipping")
            eliminated_n = state_counts["eliminated"]

            tags = {
                "🏆 VIP (já no privado)":     stats["leads_in_private"],
                "🔥 Indo bem (Liga)":         engaged_n,
                "⚠ Parando no meio (Liga)":   slipping_n,
                "❌ Eliminados (Liga)":       eliminated_n,
                "Aguardando ID/depósito":     state_counts["waiting_id"] + state_counts["waiting_deposit"],
                "Responderam":                stats["leads_replied"],
                "Contactados (sem resposta)": stats["leads_contacted"],
                "Pendentes (sem 1º contato)": stats["leads_pending"],
                "Bloqueados":                 stats["leads_blocked"],
            }

        target_million = 1_000_000.0
        progress_pct = min(100.0, (volume_total / target_million) * 100) if target_million else 0.0

        return templates.TemplateResponse(
            "dashboard_torneio.html",
            {
                "request": request,
                "stats": stats,
                "state_counts": state_counts,
                "volume_total": volume_total,
                "progress_pct": progress_pct,
                "top_ranking": top_ranking,
                "recent_campaigns": recent_campaigns,
                "top_scripts": top_scripts,
                "tags": tags,
                "liga_group": os.getenv("LIGA_GROUP", ""),
                "start_date": os.getenv("LIGA_START_DATE", ""),
                "end_date": os.getenv("LIGA_END_DATE", ""),
            },
        )

    @app.post("/api/ui-mode")
    async def api_set_ui_mode(mode: str = Form(...)):
        try:
            set_ui_mode(mode)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        return JSONResponse({"ok": True, "mode": mode})

    # ----------------------------- Scripts
    @app.get("/scripts", response_class=HTMLResponse)
    async def scripts_list(request: Request):
        with SessionLocal() as s:
            scripts = s.query(Script).order_by(desc(Script.updated_at)).all()
        return templates.TemplateResponse("scripts.html", {"request": request, "scripts": scripts})

    @app.post("/scripts")
    async def scripts_create(
        name: str = Form(...),
        mode: str = Form("ai"),
        objective: str = Form(""),
        briefing_pt: str = Form(""),
        message_text: str = Form(""),  # texto direto da mensagem (modo ai)
        target_remarketing_stage: str = Form(""),
        target_engagement_tag: str = Form(""),
    ):
        if mode not in (ScriptMode.FORWARD.value, ScriptMode.AI.value):
            mode = ScriptMode.AI.value
        with SessionLocal() as s:
            sc = Script(
                name=name.strip(),
                mode=mode,
                briefing_pt=briefing_pt.strip(),
                objective=objective.strip(),
                target_status=LeadStatus.PENDING.value,  # legacy default — ignorado
                target_remarketing_stage=(target_remarketing_stage or None),
                target_engagement_tag=(target_engagement_tag or None),
            )
            s.add(sc)
            s.commit()
            s.refresh(sc)
            # Se modo AI e usuário escreveu o texto direto, cria a 1ª variante
            if mode == ScriptMode.AI.value and message_text.strip():
                variant = ScriptVariant(
                    script_id=sc.id,
                    label="A",
                    text_es=message_text.strip(),
                    ai_provider="manual",
                )
                s.add(variant)
                s.commit()
        return RedirectResponse(f"/scripts/{sc.id}", status_code=302)

    @app.get("/scripts/{script_id}", response_class=HTMLResponse)
    async def script_detail(request: Request, script_id: int):
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            sources = sc.sources
            variants = sc.variants
        return templates.TemplateResponse(
            "script_detail.html",
            {"request": request, "script": sc, "sources": sources, "variants": variants},
        )

    @app.post("/scripts/{script_id}/edit")
    async def script_edit(
        script_id: int,
        briefing_pt: str = Form(""),
        objective: str = Form(""),
        target_remarketing_stage: str = Form(""),
        target_engagement_tag: str = Form(""),
    ):
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            sc.briefing_pt = briefing_pt.strip()
            sc.objective = objective.strip()
            sc.target_remarketing_stage = (target_remarketing_stage or None)
            sc.target_engagement_tag = (target_engagement_tag or None)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    # ----- Modo forward: sources -----
    @app.post("/scripts/{script_id}/source")
    async def script_add_source(
        script_id: int,
        link: str = Form(...),
        note: str = Form(""),
        drop_author: str = Form("on"),
    ):
        parsed = parse_telegram_link(link.strip())
        if not parsed:
            return JSONResponse(
                {"error": "Link inválido. Use https://t.me/c/.../123 ou https://t.me/usuario/123"},
                status_code=400,
            )
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            order_index = len(sc.sources)
            src = ScriptSource(
                script_id=script_id,
                source_link=parsed["raw_link"],
                source_chat=parsed["chat"],
                source_message_id=parsed["message_id"],
                drop_author=(drop_author == "on"),
                order_index=order_index,
                note=note.strip() or None,
            )
            s.add(src)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/source/{source_id}/delete")
    async def script_delete_source(script_id: int, source_id: int):
        with SessionLocal() as s:
            src = s.query(ScriptSource).get(source_id)
            if src and src.script_id == script_id:
                s.delete(src)
                s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    # ----- Modo ai: gerar/editar variantes -----
    @app.post("/scripts/{script_id}/generate")
    async def script_generate(
        script_id: int,
        n_variants: int = Form(3),
        tone: str = Form("amigável, próximo, persuasivo, sem ser invasivo"),
        target_length: int = Form(350),
    ):
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            briefing = sc.briefing_pt or ""
            objective = sc.objective or ""
            if not briefing.strip():
                return JSONResponse(
                    {"error": "Preencha o briefing antes de gerar variantes."},
                    status_code=400,
                )

        try:
            variants = await asyncio.to_thread(
                generate_script_variants,
                briefing, objective, n_variants, tone, target_length,
            )
        except AIError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        with SessionLocal() as s:
            for v in variants:
                sv = ScriptVariant(
                    script_id=script_id,
                    label=v["label"],
                    text_es=v["text"],
                    ai_provider=os.getenv("AI_PROVIDER", "anthropic"),
                    ai_model=(
                        os.getenv("ANTHROPIC_MODEL", "")
                        if os.getenv("AI_PROVIDER", "anthropic") == "anthropic"
                        else os.getenv("OPENAI_MODEL", "")
                    ),
                )
                s.add(sv)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/regenerate-from-winner")
    async def script_regen_winner(script_id: int, n_variants: int = Form(3)):
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            briefing = sc.briefing_pt or ""
            objective = sc.objective or ""
            scored = sorted(
                [v for v in sc.variants if v.sends_count > 0],
                key=lambda v: v.score(), reverse=True,
            )
            if not scored:
                return JSONResponse(
                    {"error": "Sem variantes com dados ainda. Rode uma campanha primeiro."},
                    status_code=400,
                )
            winner = scored[0]
            metrics = {
                "reply_rate": winner.reply_rate,
                "positive_rate": winner.positive_rate,
                "conversion_rate": winner.conversion_rate,
            }
            winner_text = winner.text_es

        try:
            variants = await asyncio.to_thread(
                regenerate_from_winner,
                briefing, winner_text, metrics, n_variants, objective,
            )
        except AIError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        with SessionLocal() as s:
            for v in variants:
                sv = ScriptVariant(
                    script_id=script_id,
                    label=f"R-{v['label']}",
                    text_es=v["text"],
                    ai_provider=os.getenv("AI_PROVIDER", "anthropic"),
                )
                s.add(sv)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/variant")
    async def script_add_variant(
        script_id: int,
        label: str = Form(...),
        text_es: str = Form(...),
    ):
        with SessionLocal() as s:
            sv = ScriptVariant(
                script_id=script_id,
                label=label.strip(),
                text_es=text_es.strip(),
                ai_provider="manual",
            )
            s.add(sv)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/variant/{variant_id}/edit")
    async def script_edit_variant(
        script_id: int,
        variant_id: int,
        label: str = Form(...),
        text_es: str = Form(...),
    ):
        with SessionLocal() as s:
            sv = s.query(ScriptVariant).get(variant_id)
            if sv and sv.script_id == script_id:
                sv.label = label.strip()
                sv.text_es = text_es.strip()
                s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/variant/{variant_id}/toggle")
    async def script_toggle_variant(script_id: int, variant_id: int):
        with SessionLocal() as s:
            sv = s.query(ScriptVariant).get(variant_id)
            if sv and sv.script_id == script_id:
                sv.is_active = not sv.is_active
                s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/variant/{variant_id}/delete")
    async def script_delete_variant(script_id: int, variant_id: int):
        with SessionLocal() as s:
            sv = s.query(ScriptVariant).get(variant_id)
            if sv and sv.script_id == script_id:
                s.delete(sv)
                s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/delete")
    async def script_delete(script_id: int):
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if sc:
                s.delete(sc)
                s.commit()
        return RedirectResponse("/scripts", status_code=302)

    # ----- Upload de mídia -----
    @app.post("/scripts/{script_id}/media/upload")
    async def script_upload_media(
        script_id: int,
        file: UploadFile = File(...),
        caption: str = Form(""),
        send_before_text: str = Form(""),
        video_note: str = Form(""),
    ):
        media_dir = ROOT / "media"
        media_dir.mkdir(exist_ok=True)
        ext = Path(file.filename or "file.bin").suffix or ".bin"
        new_name = f"{_uuid.uuid4().hex}{ext}"
        target = media_dir / new_name

        size = 0
        with open(target, "wb") as f:
            while chunk := await file.read(1024 * 1024):
                f.write(chunk)
                size += len(chunk)

        mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
        if mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "document"

        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            order = len(sc.media_items)
            m = ScriptMedia(
                script_id=script_id,
                filename=new_name,
                original_name=file.filename,
                mime_type=mime,
                kind=kind,
                size_bytes=size,
                caption=caption.strip(),
                order_index=order,
                send_before_text=(send_before_text == "on"),
                video_note=(video_note == "on" and kind == "video"),
            )
            s.add(m)
            s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    @app.post("/scripts/{script_id}/media/{media_id}/delete")
    async def script_delete_media(script_id: int, media_id: int):
        with SessionLocal() as s:
            m = s.query(ScriptMedia).get(media_id)
            if m and m.script_id == script_id:
                # Apaga arquivo do disco
                try:
                    p = ROOT / "media" / m.filename
                    if p.exists():
                        p.unlink()
                except Exception:
                    pass
                s.delete(m)
                s.commit()
        return RedirectResponse(f"/scripts/{script_id}", status_code=302)

    # ----------------------------- Leads
    @app.get("/leads", response_class=HTMLResponse)
    async def leads_list(
        request: Request,
        status_filter: str = "",
        liga_tag_filter: str = "",
        engagement_filter: str = "",
        stage_filter: str = "",
        only_vip: str = "",
        only_rewarm: str = "",
        only_eligible: str = "",  # filtro rápido: leads elegíveis pra disparo
        q: str = "",
        page: int = 1,
        per_page: int = 200,
    ):
        from sqlalchemy import or_
        from liga.tags import get_liga_tag, LIGA_TAGS
        from liga.remarketing_stage import STAGE_LABELS, STAGE_COLORS, count_by_stage
        from userbot.categorizer import ENGAGEMENT_TAGS, ENGAGEMENT_TAG_LABELS, ENGAGEMENT_TAG_COLORS
        per_page = max(50, min(2000, per_page))
        page = max(1, page)
        search = (q or "").strip()
        with SessionLocal() as s:
            query = s.query(Lead)
            if status_filter:
                query = query.filter(Lead.status == status_filter)
            if search:
                like = f"%{search}%"
                # Aceita @username, nome, ID
                if search.lstrip("-").isdigit():
                    query = query.filter(
                        or_(
                            Lead.telegram_id == int(search.lstrip("-")),
                            Lead.username.ilike(like),
                            Lead.first_name.ilike(like),
                            Lead.last_name.ilike(like),
                        )
                    )
                else:
                    s_clean = search.lstrip("@")
                    like_clean = f"%{s_clean}%"
                    query = query.filter(
                        or_(
                            Lead.username.ilike(like_clean),
                            Lead.first_name.ilike(like),
                            Lead.last_name.ilike(like),
                        )
                    )

            # Filtros VIP / rewarm
            if only_vip == "1":
                query = query.filter(Lead.is_vip_potential.is_(True))
            if only_rewarm == "1":
                query = query.filter(Lead.rewarm_candidate.is_(True))

            # Filtro por remarketing_stage
            if stage_filter:
                query = query.filter(Lead.remarketing_stage == stage_filter)

            # Filtro rápido: elegíveis pra disparo
            # untouched + r1_cold + r2_cold (não em cooldown, não terminal)
            # E NÃO is_fresh (não respondeu nas últimas 24h)
            if only_eligible == "r1":
                query = query.filter(Lead.remarketing_stage == "untouched")
                query = query.filter(Lead.is_fresh.is_(False))
                query = query.filter(Lead.opted_out.is_(False))
                query = query.filter(Lead.in_private_group.is_(False))
            elif only_eligible == "r2":
                query = query.filter(Lead.remarketing_stage == "r1_cold")
                query = query.filter(Lead.is_fresh.is_(False))
                query = query.filter(Lead.opted_out.is_(False))
                query = query.filter(Lead.in_private_group.is_(False))
            elif only_eligible == "r3":
                query = query.filter(Lead.remarketing_stage == "r2_cold")
                query = query.filter(Lead.is_fresh.is_(False))
                query = query.filter(Lead.opted_out.is_(False))
                query = query.filter(Lead.in_private_group.is_(False))

            # Filtro por engagement_tag (SQL — rápido)
            if engagement_filter:
                if engagement_filter == "_none":
                    query = query.filter(Lead.engagement_tag.is_(None))
                elif engagement_filter in ENGAGEMENT_TAGS:
                    query = query.filter(Lead.engagement_tag == engagement_filter)

            # Pré-filtra por liga_tag se solicitado (em Python — set pequeno)
            if liga_tag_filter:
                # Pula direto pra leads que estão em estados Liga relevantes
                if liga_tag_filter == "eliminated":
                    query = query.filter(Lead.liga_state == "eliminated")
                elif liga_tag_filter in ("engaged", "slipping", "not_started"):
                    query = query.filter(Lead.liga_state.in_([
                        "active", "at_risk", "finalist", "waitlist",
                    ]))

            total_filtered = query.count()
            leads = (
                query.order_by(desc(Lead.last_dm_at))
                .offset((page - 1) * per_page)
                .limit(per_page)
                .all()
            )

            # Refina filtro liga_tag: como engaged/slipping dependem do último DailyVolume,
            # verificamos cada lead com get_liga_tag() e filtramos in-memory
            if liga_tag_filter and liga_tag_filter in LIGA_TAGS:
                leads = [l for l in leads if get_liga_tag(l, s) == liga_tag_filter]
                total_filtered = len(leads)  # ajusta (best-effort, não exato pra paginação)

            # Mapa lead.id -> tag pra exibir na coluna
            lead_tags = {l.id: get_liga_tag(l, s) for l in leads}

            statuses = [st.value for st in LeadStatus]

            # Contadores por status (visão completa, sem filtro)
            counts = {}
            for st in LeadStatus:
                counts[st.value] = (
                    s.query(Lead).filter(Lead.status == st.value).count()
                )
            counts["total"] = s.query(Lead).count()

            using_group = bool(os.getenv("LEADS_SOURCE_GROUP", "").strip())

            # Set de IDs de leads que tiveram tentativa de troca de conta
            mismatch_ids = {
                row[0] for row in (
                    s.query(OperationProof.lead_id)
                    .filter(OperationProof.validated.is_(False))
                    .filter(OperationProof.raw_ai_response.like("%id_mismatch%"))
                    .distinct()
                    .all()
                )
            }

        # Counts por engagement_tag (visão geral)
        with SessionLocal() as s:
            from sqlalchemy import func as _func
            engagement_counts = dict(
                s.query(Lead.engagement_tag, _func.count(Lead.id))
                .group_by(Lead.engagement_tag)
                .all()
            )

        total_pages = max(1, (total_filtered + per_page - 1) // per_page)
        return templates.TemplateResponse(
            "leads.html",
            {
                "request": request,
                "leads": leads,
                "statuses": statuses,
                "status_filter": status_filter,
                "liga_tags": list(LIGA_TAGS),
                "liga_tag_filter": liga_tag_filter,
                "lead_tags": lead_tags,
                "engagement_tags": list(ENGAGEMENT_TAGS),
                "engagement_labels": ENGAGEMENT_TAG_LABELS,
                "engagement_colors": ENGAGEMENT_TAG_COLORS,
                "engagement_filter": engagement_filter,
                "engagement_counts": engagement_counts,
                "stage_filter": stage_filter,
                "stage_labels": STAGE_LABELS,
                "stage_colors": STAGE_COLORS,
                "stage_counts": count_by_stage(),
                "only_vip": only_vip,
                "only_rewarm": only_rewarm,
                "only_eligible": only_eligible,
                "search": search,
                "using_group": using_group,
                "counts": counts,
                "total_filtered": total_filtered,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "mismatch_ids": mismatch_ids,
            },
        )

    @app.post("/api/leads/sync")
    async def leads_sync(source: str = Form(""), scan_images: str = Form("0")):
        """Sync de leads. Por default, NÃO escaneia imagens (caro).
        Use scan_images=1 só quando quiser forçar Vision (admin).

        O scan incremental automático (cron 5min) já cuida de imagens novas.
        """
        scan_imgs = scan_images == "1"
        try:
            if source == "group":
                result = await sync_leads_from_group()
            elif source == "dm":
                result = await sync_leads_from_dm_history(scan_images=scan_imgs)
            else:
                # auto mode roda DM history; passamos scan_images
                from userbot.leads import sync_leads_from_dm_history as _sync
                result = await _sync(scan_images=scan_imgs)
            return JSONResponse(result)
        except Exception as e:
            logger.exception("Erro sync")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/leads/{lead_id}/status")
    async def lead_set_status(lead_id: int, new_status: str = Form(...)):
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if lead:
                lead.status = new_status
                s.commit()
        return RedirectResponse("/leads", status_code=302)

    # ----------------------------- Campanhas
    @app.get("/campaigns", response_class=HTMLResponse)
    async def campaigns_list(request: Request):
        with SessionLocal() as s:
            campaigns = s.query(Campaign).order_by(desc(Campaign.created_at)).all()
            scripts = s.query(Script).filter(Script.is_active.is_(True)).all()
            statuses = [st.value for st in LeadStatus]
        return templates.TemplateResponse(
            "campaigns.html",
            {"request": request, "campaigns": campaigns, "scripts": scripts, "statuses": statuses},
        )

    @app.post("/campaigns")
    async def campaigns_create(
        name: str = Form(...),
        script_id: int = Form(...),
        target_status: str = Form(LeadStatus.PENDING.value),
        max_leads: int = Form(0),
        variant_strategy: str = Form("rotate"),
        scheduled_at: str = Form(""),
        run_now: str = Form(""),
        include_vips: str = Form(""),  # se marcado, NÃO exclui VIPs
    ):
        when: Optional[datetime] = None
        if scheduled_at and not run_now:
            try:
                when = datetime.fromisoformat(scheduled_at)
            except ValueError:
                pass
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                return JSONResponse({"error": "Script não encontrado"}, status_code=400)
            mode = (sc.mode or "forward").lower()
            if mode == "forward" and not sc.sources:
                return JSONResponse(
                    {"error": "Script forward precisa ter pelo menos 1 mensagem fonte."},
                    status_code=400,
                )
            if mode == "ai" and not [v for v in sc.variants if v.is_active]:
                return JSONResponse(
                    {"error": "Script AI precisa ter pelo menos 1 variante ativa."},
                    status_code=400,
                )
            campaign = Campaign(
                name=name.strip(),
                script_id=script_id,
                target_status=target_status,
                max_leads=max_leads,
                variant_strategy=variant_strategy,
                status=CampaignStatus.DRAFT.value,
                exclude_vips=(include_vips != "1"),  # default True (exclui)
            )
            s.add(campaign)
            s.commit()
            s.refresh(campaign)
            cid = campaign.id
        schedule_campaign(cid, when)
        return RedirectResponse("/campaigns", status_code=302)

    @app.post("/campaigns/{campaign_id}/cancel")
    async def campaigns_cancel(campaign_id: int):
        cancel_campaign(campaign_id)
        return RedirectResponse("/campaigns", status_code=302)

    @app.post("/campaigns/{campaign_id}/process-queue")
    async def campaigns_process_queue(campaign_id: int):
        """Força processar todos os sends 'queued' dessa campanha.

        Use quando a campanha aparece como completed/running mas tem sends
        que ficaram pra trás (bug raro de scheduler/restart do bot).
        """
        from userbot.sender import execute_send_record
        with SessionLocal() as s:
            campaign = s.query(Campaign).get(campaign_id)
            if not campaign:
                return JSONResponse({"error": "Campanha não encontrada"}, status_code=404)
            stuck = (
                s.query(Send)
                .filter(Send.campaign_id == campaign_id)
                .filter(Send.status == SendStatus.QUEUED.value)
                .order_by(Send.queued_at.asc())
                .all()
            )
            send_ids = [sd.id for sd in stuck]

        if not send_ids:
            return JSONResponse({"ok": True, "processed": 0, "message": "Nenhum send em queue."})

        # Roda em background — pode demorar minutos
        import asyncio as _asyncio
        async def _process_all():
            for sid in send_ids:
                try:
                    await execute_send_record(sid)
                except Exception:
                    logger.exception("[process_queue] erro send %s", sid)
        _asyncio.create_task(_process_all())

        return JSONResponse({
            "ok": True,
            "started": True,
            "queued": len(send_ids),
            "message": f"Processando {len(send_ids)} sends em background",
        })

    @app.post("/api/parar-tudo")
    async def parar_tudo():
        """🛑 PARADA DE EMERGÊNCIA — cancela TODAS as campanhas ativas."""
        try:
            result = cancel_all_campaigns()
            return JSONResponse({
                "ok": True,
                "message": f"🛑 {result['cancelled']} campanha(s) cancelada(s)",
                "ids": result["ids"],
            })
        except Exception as e:
            logger.exception("Erro parar-tudo")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    async def campaign_detail(request: Request, campaign_id: int):
        with SessionLocal() as s:
            campaign = s.query(Campaign).get(campaign_id)
            if not campaign:
                raise HTTPException(404)
            sends = (
                s.query(Send).filter(Send.campaign_id == campaign_id)
                .order_by(desc(Send.queued_at)).limit(500).all()
            )
        return templates.TemplateResponse(
            "campaign_detail.html",
            {"request": request, "campaign": campaign, "sends": sends},
        )

    # ----------------------------- Métricas
    @app.get("/metrics", response_class=HTMLResponse)
    async def metrics_page(request: Request):
        with SessionLocal() as s:
            scripts = s.query(Script).all()
            ranked = []
            for sc in scripts:
                variants_data = []
                for v in sc.variants:
                    variants_data.append({
                        "id": v.id, "label": v.label, "text": v.text_es,
                        "is_active": v.is_active,
                        "sends": v.sends_count or 0,
                        "replies": v.replies_count or 0,
                        "positives": v.positive_count or 0,
                        "conversions": v.conversions_count or 0,
                        "score": v.score(),
                    })
                variants_data.sort(key=lambda x: x["score"], reverse=True)
                ranked.append({
                    "id": sc.id,
                    "name": sc.name,
                    "mode": sc.mode,
                    "objective": sc.objective,
                    "sources_count": len(sc.sources),
                    "sends": sc.sends_count or 0,
                    "replies": sc.replies_count or 0,
                    "positives": sc.positive_count or 0,
                    "conversions": sc.conversions_count or 0,
                    "reply_rate": sc.reply_rate,
                    "positive_rate": sc.positive_rate,
                    "conversion_rate": sc.conversion_rate,
                    "score": sc.score(),
                    "variants": variants_data,
                })
            ranked.sort(key=lambda x: x["score"], reverse=True)
        return templates.TemplateResponse(
            "metrics.html", {"request": request, "ranked": ranked}
        )

    # ----------------------------- Testes (múltiplos @usernames)
    def _normalize_username(u: str) -> str:
        u = (u or "").strip()
        if not u:
            return ""
        if not u.startswith("@") and not u.lstrip("-").isdigit():
            u = "@" + u
        return u

    def _get_test_usernames() -> list[str]:
        """Lista de @usernames pra teste. Une .env (TEST_USERNAME) + Settings."""
        result = []
        env_val = os.getenv("TEST_USERNAME", "").strip()
        if env_val:
            for u in env_val.split(","):
                un = _normalize_username(u)
                if un and un not in result:
                    result.append(un)
        with SessionLocal() as s:
            rows = s.query(Setting).filter(Setting.key.like("test_username:%")).all()
            for r in rows:
                un = _normalize_username((r.value or "").strip())
                if un and un not in result:
                    result.append(un)
        return result

    def _add_test_username(username: str) -> None:
        un = _normalize_username(username)
        if not un:
            return
        if un in _get_test_usernames():
            return
        with SessionLocal() as s:
            key = f"test_username:{un}"
            existing = s.query(Setting).get(key)
            if not existing:
                s.add(Setting(key=key, value=un))
                s.commit()

    def _remove_test_username(username: str) -> None:
        un = _normalize_username(username)
        if not un:
            return
        with SessionLocal() as s:
            row = s.query(Setting).get(f"test_username:{un}")
            if row:
                s.delete(row)
                s.commit()

    @app.get("/testes", response_class=HTMLResponse)
    async def testes_page(request: Request):
        scripts_data = []
        history = []
        with SessionLocal() as s:
            scripts = (
                s.query(Script)
                .filter(Script.is_active.is_(True))
                .order_by(desc(Script.updated_at))
                .all()
            )
            for sc in scripts:
                scripts_data.append({
                    "id": sc.id,
                    "name": sc.name,
                    "mode": sc.mode or "ai",
                    "objective": sc.objective or "",
                    "variants_count": len(sc.variants),
                    "sources_count": len(sc.sources),
                    "ready": (
                        bool(sc.sources) if (sc.mode or "ai") == "forward"
                        else any(v.is_active for v in sc.variants)
                    ),
                })
            history_rows = s.query(Setting).filter(Setting.key.like("test_history:%")).all()
            for r in history_rows:
                try:
                    parts = (r.value or "").split("||")
                    history.append({
                        "ts": parts[0] if len(parts) > 0 else "",
                        "username": parts[1] if len(parts) > 1 else "",
                        "script": parts[2] if len(parts) > 2 else "",
                        "result": parts[3] if len(parts) > 3 else "",
                    })
                except Exception:
                    pass
        history.sort(key=lambda h: h["ts"], reverse=True)
        history = history[:30]
        return templates.TemplateResponse(
            "testes.html",
            {
                "request": request,
                "scripts": scripts_data,
                "test_usernames": _get_test_usernames(),
                "history": history,
            },
        )

    @app.post("/testes/username/add")
    async def testes_add_username(username: str = Form(...)):
        _add_test_username(username)
        return RedirectResponse("/testes", status_code=302)

    @app.post("/testes/username/remove")
    async def testes_remove_username(username: str = Form(...)):
        _remove_test_username(username)
        return RedirectResponse("/testes", status_code=302)

    async def _execute_test_blast(script_id: int) -> dict:
        """Roda o teste pra todos os @usernames cadastrados.
        Usado tanto pelo botão "Enviar agora" quanto pelo agendador.
        """
        usernames = _get_test_usernames()
        results = []
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            script_name = sc.name if sc else f"#{script_id}"
        for username in usernames:
            try:
                r = await send_test_to_username(username, script_id)
                results.append((username, r))
            except Exception as e:
                from userbot.sender import SendResult
                results.append((username, SendResult(False, error=f"{type(e).__name__}: {e}")))

        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with SessionLocal() as s:
                for username, r in results:
                    outcome = "OK" if r.success else f"ERRO: {r.error or r.skipped_reason or '?'}"
                    key = f"test_history:{ts}:{script_id}:{username}"
                    s.add(Setting(key=key, value=f"{ts}||{username}||{script_name}||{outcome}"))
                s.commit()
        except Exception as e:
            logger.warning("Falha gravando histórico: %s", e)
        return {"results": results, "ts": ts, "script_name": script_name}

    @app.post("/testes/send/{script_id}")
    async def testes_send(script_id: int):
        usernames = _get_test_usernames()
        if not usernames:
            return JSONResponse(
                {"error": "Cadastre pelo menos um @username de teste primeiro."},
                status_code=400,
            )

        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                return JSONResponse({"error": "Script não encontrado"}, status_code=404)
            script_name = sc.name
            mode = (sc.mode or "ai").lower()
            sources_count = len(sc.sources)
            active_variants = [v for v in sc.variants if v.is_active]
            variants_count = len(active_variants)

        if mode == "forward" and sources_count == 0:
            return JSONResponse(
                {"error": "Esse script (forward) ainda não tem mensagens fonte. Mude pro modo AI ou adicione links."},
                status_code=400,
            )
        if mode == "ai" and variants_count == 0:
            return JSONResponse(
                {"error": "Esse script não tem mensagem cadastrada. Abra o script e escreva o texto."},
                status_code=400,
            )

        out = await _execute_test_blast(script_id)
        results = out["results"]
        ok_list = [u for u, r in results if r.success]
        fail_list = [(u, r) for u, r in results if not r.success]

        if not fail_list:
            return JSONResponse({
                "ok": True,
                "message": f"Teste enviado com sucesso pra {len(ok_list)} contas: {', '.join(ok_list)}",
            })
        return JSONResponse({
            "ok": len(ok_list) > 0,
            "message": f"OK em {len(ok_list)} ({', '.join(ok_list) or '-'})",
            "error": "Falhas: " + "; ".join(f"{u}: {r.error or r.skipped_reason}" for u, r in fail_list),
        }, status_code=200 if ok_list else 500)

    @app.post("/testes/agendar/{script_id}")
    async def testes_agendar(
        script_id: int,
        scheduled_at: str = Form(""),
    ):
        from userbot.scheduler import scheduler, BRT
        from apscheduler.triggers.date import DateTrigger
        from zoneinfo import ZoneInfo

        usernames = _get_test_usernames()
        if not usernames:
            return _err_html(
                "Cadastre pelo menos um @username de teste antes de agendar.",
                "/testes",
            )
        if not scheduled_at:
            return _err_html("Você não preencheu data/hora.", "/testes")
        try:
            when = datetime.fromisoformat(scheduled_at)
        except ValueError:
            return _err_html("Data/hora inválida.", "/testes")
        when = when.replace(tzinfo=BRT)
        if when < datetime.now(BRT):
            return _err_html("Data/hora no passado. Escolha algo no futuro.", "/testes")

        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                return _err_html("Script não encontrado.", "/testes")
            script_name = sc.name
            mode = (sc.mode or "ai").lower()
            if mode == "ai" and not [v for v in sc.variants if v.is_active]:
                return _err_html("Script sem mensagem cadastrada.", f"/scripts/{script_id}")
            if mode == "forward" and not sc.sources:
                return _err_html("Script forward sem mensagens fonte.", f"/scripts/{script_id}")

        # Agenda o blast
        job_id = f"test-{script_id}-{when.isoformat()}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
        scheduler.add_job(
            _execute_test_blast,
            trigger=DateTrigger(run_date=when, timezone=BRT),
            args=[script_id],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=300,
        )

        # Registra no histórico como "agendado"
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        try:
            with SessionLocal() as s:
                key = f"test_history:{ts}:{script_id}:scheduled"
                when_brt_str = when.strftime("%d/%m %H:%M")
                s.add(Setting(
                    key=key,
                    value=f"{ts}||(agendado pra {when_brt_str} BRT)||{script_name}||AGENDADO",
                ))
                s.commit()
        except Exception as e:
            logger.warning("Falha histórico agendado: %s", e)

        return RedirectResponse("/testes", status_code=302)

    # ----------------------------- Disparo/Agendamento direto pelo script
    def _err_html(msg: str, back: str = "/scripts"):
        body = f"""<!DOCTYPE html><html><head>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css">
</head><body class="p-4 bg-light"><div class="container">
<div class="alert alert-danger">{msg}</div>
<a href="{back}" class="btn btn-primary">← Voltar</a>
</div></body></html>"""
        return HTMLResponse(body, status_code=400)

    @app.post("/scripts/{script_id}/disparar")
    async def script_disparar(
        script_id: int,
        action: str = Form("now"),  # now | schedule
        scheduled_at: str = Form(""),
        target_status: str = Form(LeadStatus.PENDING.value),
        target_remarketing_stage: str = Form(""),
        target_engagement_tag: str = Form(""),
        max_leads: int = Form(0),
        name: str = Form(""),
    ):
        # Validação de bounds
        if max_leads < 0:
            return _err_html(
                f"Máx. leads não pode ser negativo (recebido: {max_leads}).",
                f"/scripts/{script_id}",
            )
        if max_leads > 5000:
            return _err_html(
                f"Máx. leads = {max_leads} é muito alto. Limite máximo: 5000 por execução.",
                f"/scripts/{script_id}",
            )
        when: Optional[datetime] = None
        if action == "schedule":
            if not scheduled_at:
                return _err_html(
                    "Você clicou em Agendar mas não preencheu data/hora.",
                    f"/scripts/{script_id}",
                )
            try:
                when = datetime.fromisoformat(scheduled_at)
            except ValueError:
                return _err_html(
                    "Data/hora inválida. Use o seletor.",
                    f"/scripts/{script_id}",
                )
            # form datetime-local vem naive, mas representa horário de Brasília
            when = when.replace(tzinfo=BRT)
            if when < datetime.now(BRT):
                return _err_html(
                    f"Data agendada ({when.strftime('%d/%m %H:%M')} BRT) está no passado. "
                    f"Agora é {datetime.now(BRT).strftime('%d/%m %H:%M')} BRT.",
                    f"/scripts/{script_id}",
                )

        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                raise HTTPException(404)
            mode = (sc.mode or "ai").lower()
            if mode == "forward" and not sc.sources:
                return _err_html(
                    "Script (forward) sem mensagens fonte. Adicione antes de disparar.",
                    f"/scripts/{script_id}",
                )
            if mode == "ai" and not [v for v in sc.variants if v.is_active]:
                return _err_html(
                    "Script sem mensagem cadastrada. Escreva o texto da mensagem antes.",
                    f"/scripts/{script_id}",
                )

            campaign_name = (name or "").strip() or f"{sc.name} - {datetime.utcnow().strftime('%d/%m %H:%M')}"
            campaign = Campaign(
                name=campaign_name,
                script_id=script_id,
                target_status=target_status,
                target_remarketing_stage=(target_remarketing_stage or None),
                target_engagement_tag=(target_engagement_tag or None),
                max_leads=max_leads,
                status=CampaignStatus.DRAFT.value,
            )
            s.add(campaign)
            s.commit()
            s.refresh(campaign)
            cid = campaign.id
        schedule_campaign(cid, when)
        return RedirectResponse(f"/campaigns/{cid}", status_code=302)

    # ----------------------------- Settings
    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request):
        env = {
            "PRIVATE_GROUP": os.getenv("PRIVATE_GROUP", ""),
            "LEADS_SOURCE_GROUP": os.getenv("LEADS_SOURCE_GROUP", ""),
            "TELEGRAM_PHONE": os.getenv("TELEGRAM_PHONE", ""),
            "AI_PROVIDER": os.getenv("AI_PROVIDER", "anthropic"),
            "ANTHROPIC_MODEL": os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001"),
            "ANTHROPIC_KEY_SET": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
            "OPENAI_MODEL": os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
            "OPENAI_KEY_SET": bool(os.getenv("OPENAI_API_KEY", "").strip()),
            "MAX_SENDS_PER_HOUR": os.getenv("MAX_SENDS_PER_HOUR", "120"),
            "SEND_DELAY_MIN": os.getenv("SEND_DELAY_MIN", "15"),
            "SEND_DELAY_MAX": os.getenv("SEND_DELAY_MAX", "40"),
            "LONG_PAUSE_EVERY": os.getenv("LONG_PAUSE_EVERY", "80"),
            "LONG_PAUSE_SECONDS": os.getenv("LONG_PAUSE_SECONDS", "180"),
        }
        return templates.TemplateResponse("settings.html", {"request": request, "env": env})

    # ----------------------------- Liga
    @app.get("/liga", response_class=HTMLResponse)
    async def liga_dashboard(request: Request):
        from sqlalchemy import func
        from liga.scoring import get_lead_tier
        try:
            from zoneinfo import ZoneInfo
            BA = ZoneInfo("America/Argentina/Buenos_Aires")
        except Exception:
            BA = None

        with SessionLocal() as s:
            # Contagem por estado
            state_counts = dict(
                s.query(Lead.liga_state, func.count(Lead.id))
                .group_by(Lead.liga_state).all()
            )
            for st in ("new", "onboarding", "waiting_id", "waiting_deposit",
                       "waitlist", "active", "at_risk", "eliminated", "finalist"):
                state_counts.setdefault(st, 0)

            volume_total = float(s.query(func.sum(DailyVolume.volume_usd)).scalar() or 0.0)

            # Top 10 ranking por volume acumulado
            top_rows = (
                s.query(
                    Lead.id, Lead.first_name, Lead.last_name, Lead.username,
                    Lead.telegram_id, Lead.liga_state, Lead.liga_balance,
                    Lead.streak_days, Lead.lead_score,
                    func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).label("vol"),
                )
                .outerjoin(DailyVolume, DailyVolume.lead_id == Lead.id)
                .filter(Lead.liga_state.in_([
                    "active", "at_risk", "finalist", "waitlist",
                ]))
                .group_by(Lead.id)
                .order_by(func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).desc())
                .limit(10)
                .all()
            )
            ranking = []
            for r in top_rows:
                nome = (r.first_name or "") + (" " + r.last_name if r.last_name else "")
                nome = nome.strip() or (f"@{r.username}" if r.username else f"id:{r.telegram_id}")
                ranking.append({
                    "id": r.id, "name": nome, "username": r.username,
                    "state": r.liga_state, "balance": float(r.liga_balance or 0.0),
                    "streak": r.streak_days or 0, "score": r.lead_score or 0,
                    "tier": get_lead_tier(r.lead_score or 0),
                    "volume": float(r.vol or 0.0),
                })

            # Volume diário últimos 14 dias
            from datetime import datetime, timedelta
            now = datetime.now(BA) if BA else datetime.utcnow()
            days = []
            for i in range(13, -1, -1):
                d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
                v = float(
                    s.query(func.sum(DailyVolume.volume_usd))
                    .filter(DailyVolume.date == d).scalar() or 0.0
                )
                days.append({"date": d, "volume": v})
            max_day = max((d["volume"] for d in days), default=0.0) or 1.0

            # Comprovantes recentes
            import json as _json
            recent_proofs = (
                s.query(OperationProof, Lead)
                .join(Lead, Lead.id == OperationProof.lead_id)
                .order_by(OperationProof.created_at.desc())
                .limit(20).all()
            )
            proofs_view = []
            for p, l in recent_proofs:
                nome = l.first_name or (f"@{l.username}" if l.username else f"id:{l.telegram_id}")
                reason = None
                try:
                    if p.raw_ai_response:
                        reason = (_json.loads(p.raw_ai_response) or {}).get("rejected_reason")
                except Exception:
                    pass
                proofs_view.append({
                    "id": p.id, "lead_id": l.id, "lead_name": nome,
                    "date": p.proof_date, "volume": float(p.volume_usd or 0.0),
                    "platform": p.platform or "—",
                    "confidence": p.confidence or "—",
                    "validated": bool(p.validated),
                    "rejected_reason": reason,
                    "created_at": p.created_at,
                })

            # Comprovantes aguardando revisão manual
            review_pending = (
                s.query(OperationProof)
                .filter(OperationProof.validated.is_(False))
                .filter(OperationProof.raw_ai_response.like("%low_confidence%"))
                .count()
            )

            # ID mismatches — auditoria de tentativas de troca de conta
            mismatch_rows = (
                s.query(OperationProof, Lead)
                .join(Lead, Lead.id == OperationProof.lead_id)
                .filter(OperationProof.validated.is_(False))
                .filter(OperationProof.raw_ai_response.like("%id_mismatch%"))
                .order_by(OperationProof.created_at.desc())
                .limit(20).all()
            )
            mismatches = []
            for p, l in mismatch_rows:
                nome = l.first_name or (f"@{l.username}" if l.username else f"id:{l.telegram_id}")
                mismatches.append({
                    "lead_id": l.id, "lead_name": nome,
                    "registered_id": l.liga_account_id or "—",
                    "attempted_id": p.account_id_raw or "—",
                    "attempted_balance": float(p.volume_usd or 0.0),
                    "platform": p.platform or "—",
                    "created_at": p.created_at,
                })
            mismatches_total = len(mismatches)

            # Distribuição por tier
            tier_counts = {"vip": 0, "hot": 0, "warm": 0, "cold": 0}
            scores = s.query(Lead.lead_score).filter(Lead.liga_state.in_([
                "active", "at_risk", "finalist", "waitlist",
            ])).all()
            for (sc,) in scores:
                tier_counts[get_lead_tier(sc or 0)] += 1

            # Distribuição por tag de engajamento (engaged/slipping/eliminated)
            from liga.tags import get_liga_tag
            tag_counts = {"engaged": 0, "slipping": 0, "eliminated": state_counts["eliminated"], "not_started": 0}
            in_competition = s.query(Lead).filter(Lead.liga_state.in_([
                "active", "at_risk", "finalist", "waitlist",
            ])).all()
            for l in in_competition:
                t = get_liga_tag(l, s)
                if t in tag_counts:
                    tag_counts[t] += 1

        target_million = 1_000_000.0
        progress_pct = min(100.0, (volume_total / target_million) * 100) if target_million else 0.0

        return templates.TemplateResponse(
            "liga.html",
            {
                "request": request,
                "state_counts": state_counts,
                "volume_total": volume_total,
                "target_million": target_million,
                "progress_pct": progress_pct,
                "ranking": ranking,
                "days": days,
                "max_day": max_day,
                "proofs": proofs_view,
                "tier_counts": tier_counts,
                "tag_counts": tag_counts,
                "mismatches": mismatches,
                "mismatches_total": mismatches_total,
                "review_pending": review_pending,
                "liga_group": os.getenv("LIGA_GROUP", ""),
                "admin_id": os.getenv("ADMIN_TELEGRAM_ID", ""),
                "start_date": os.getenv("LIGA_START_DATE", ""),
                "end_date": os.getenv("LIGA_END_DATE", ""),
            },
        )

    @app.get("/liga/lead/{lead_id}", response_class=HTMLResponse)
    async def liga_lead_detail(request: Request, lead_id: int):
        import json as _json
        from sqlalchemy import func
        from liga.scoring import calc_lead_score, get_lead_tier
        from liga.tags import get_liga_tag
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")

            proof_rows = (
                s.query(OperationProof)
                .filter(OperationProof.lead_id == lead.id)
                .order_by(OperationProof.created_at.desc())
                .all()
            )
            # Enriquece cada proof com motivo de rejeição (se houver)
            proofs = []
            mismatch_attempts = []
            for p in proof_rows:
                reason = None
                try:
                    if p.raw_ai_response:
                        reason = (_json.loads(p.raw_ai_response) or {}).get("rejected_reason")
                except Exception:
                    pass
                proofs.append({
                    "id": p.id,
                    "proof_date": p.proof_date,
                    "volume_usd": float(p.volume_usd or 0.0),
                    "account_id_raw": p.account_id_raw,
                    "platform": p.platform,
                    "confidence": p.confidence,
                    "validated": bool(p.validated),
                    "created_at": p.created_at,
                    "rejected_reason": reason,
                    "needs_review": bool(getattr(p, "needs_review", False)),
                    "review_reason": getattr(p, "review_reason", None),
                    "validated_by": getattr(p, "validated_by", None),
                    "review_notes": getattr(p, "review_notes", None),
                })
                if reason == "id_mismatch":
                    mismatch_attempts.append(p)

            volumes = (
                s.query(DailyVolume)
                .filter(DailyVolume.lead_id == lead.id)
                .order_by(DailyVolume.date.desc())
                .limit(30).all()
            )
            objections = (
                s.query(Objection)
                .filter(Objection.lead_id == lead.id)
                .order_by(Objection.created_at.desc())
                .limit(10).all()
            )
            total_volume = float(
                s.query(func.sum(DailyVolume.volume_usd))
                .filter(DailyVolume.lead_id == lead.id).scalar() or 0.0
            )
            current_score = calc_lead_score(lead, s)
            tier = get_lead_tier(current_score)
            current_tag = get_liga_tag(lead, s)

            from liga.remarketing_stage import (
                STAGE_LABELS, STAGE_COLORS, next_action_for, is_eligible_for_dispatch,
            )
            stage_label = STAGE_LABELS.get(lead.remarketing_stage or "untouched", "—")
            stage_color = STAGE_COLORS.get(lead.remarketing_stage or "untouched", "secondary")
            next_action = next_action_for(lead)
            eligible, eligibility_reason = is_eligible_for_dispatch(lead)

        return templates.TemplateResponse(
            "liga_lead.html",
            {
                "request": request,
                "lead": lead,
                "proofs": proofs,
                "mismatch_count": len(mismatch_attempts),
                "volumes": volumes,
                "objections": objections,
                "total_volume": total_volume,
                "current_score": current_score,
                "tier": tier,
                "current_tag": current_tag,
                "all_states": [st.value for st in LigaState],
                "stage_label": stage_label,
                "stage_color": stage_color,
                "next_action": next_action,
                "eligible": eligible,
                "eligibility_reason": eligibility_reason,
            },
        )

    @app.post("/liga/lead/{lead_id}/state")
    async def liga_lead_change_state(lead_id: int, new_state: str = Form(...)):
        valid = {st.value for st in LigaState}
        if new_state not in valid:
            raise HTTPException(400, f"Estado inválido: {new_state}")
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")
            lead.liga_state = new_state
            lead.last_bot_action = "manual_state_change"
            s.commit()
        return RedirectResponse(f"/liga/lead/{lead_id}", status_code=302)

    @app.post("/liga/run/{job}")
    async def liga_run_job(job: str):
        from liga import scheduler as liga_sched
        # Garante que o scheduler tem cliente
        if liga_sched._client is None:
            try:
                from userbot.client import get_client
                liga_sched._client = await get_client()
            except Exception as e:
                return JSONResponse({"error": f"sem cliente Telegram: {e}"}, status_code=500)

        try:
            if job == "reminder":
                await liga_sched.task_daily_reminder()
            elif job == "ranking":
                await liga_sched.task_daily_ranking()
            elif job == "reset":
                await liga_sched.task_daily_reset()
            elif job == "weekly":
                await liga_sched.task_weekly_report()
            elif job == "checkpoint1":
                await liga_sched.task_checkpoint(1, is_final=False)
            elif job == "checkpoint2":
                await liga_sched.task_checkpoint(2, is_final=False)
            elif job == "checkpoint3":
                await liga_sched.task_checkpoint(3, is_final=False)
            elif job == "final":
                await liga_sched.task_checkpoint(4, is_final=True)
            else:
                return JSONResponse({"error": f"job desconhecido: {job}"}, status_code=400)
        except Exception as e:
            logger.exception("[liga] erro rodando job %s", job)
            return JSONResponse({"error": str(e)}, status_code=500)
        return JSONResponse({"ok": True, "job": job})

    @app.get("/liga/review", response_class=HTMLResponse)
    async def liga_review(request: Request):
        """Fila de revisão manual: prints que a IA não conseguiu ler com confiança."""
        import json as _json
        with SessionLocal() as s:
            rows = (
                s.query(OperationProof, Lead)
                .join(Lead, Lead.id == OperationProof.lead_id)
                .filter(OperationProof.validated.is_(False))
                .filter(OperationProof.raw_ai_response.like("%low_confidence%"))
                .order_by(OperationProof.created_at.desc())
                .all()
            )
            items = []
            for p, l in rows:
                # Parse raw_ai_response pra mostrar o que a IA conseguiu ler
                ai = {}
                try:
                    ai = _json.loads(p.raw_ai_response or "{}")
                except Exception:
                    pass
                items.append({
                    "id": p.id,
                    "lead_id": l.id,
                    "lead_name": (l.first_name or "") + (" " + l.last_name if l.last_name else ""),
                    "lead_username": l.username,
                    "lead_telegram_id": l.telegram_id,
                    "lead_state": l.liga_state or "new",
                    "proof_date": p.proof_date,
                    "volume_attempt": float(p.volume_usd or 0.0),
                    "platform": p.platform or "—",
                    "confidence": p.confidence or "—",
                    "image_path": p.image_path,
                    "created_at": p.created_at,
                    "ai_data": ai,
                })
        return templates.TemplateResponse(
            "liga_review.html",
            {"request": request, "items": items},
        )

    @app.post("/liga/review/{proof_id}/approve")
    async def liga_review_approve(proof_id: int, volume: float = Form(...)):
        """Aprova um proof manualmente, atualiza DailyVolume + estado do lead."""
        import json as _json
        try:
            volume = float(volume)
        except Exception:
            return JSONResponse({"error": "volume inválido"}, status_code=400)
        if volume < 0:
            return JSONResponse({"error": "volume não pode ser negativo"}, status_code=400)

        with SessionLocal() as s:
            p = s.query(OperationProof).get(proof_id)
            if not p:
                raise HTTPException(404, "Proof não encontrado")
            lead = s.query(Lead).get(p.lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")

            # Atualiza o proof — vira validado, com motivo "manual_approval"
            ai = {}
            try:
                ai = _json.loads(p.raw_ai_response or "{}")
            except Exception:
                pass
            ai["manual_approved_at"] = datetime.utcnow().isoformat()
            ai["manual_approved_volume"] = volume
            ai.pop("rejected_reason", None)
            p.validated = True
            p.volume_usd = volume
            p.raw_ai_response = _json.dumps(ai, ensure_ascii=False)

            # Atualiza/cria DailyVolume usando a data do proof
            today_str = p.proof_date or datetime.utcnow().strftime("%Y-%m-%d")
            dv = (
                s.query(DailyVolume)
                .filter(DailyVolume.lead_id == lead.id, DailyVolume.date == today_str)
                .one_or_none()
            )
            if dv:
                dv.volume_usd = (dv.volume_usd or 0.0) + volume
            else:
                dv = DailyVolume(lead_id=lead.id, date=today_str, volume_usd=volume)
                s.add(dv)

            # Marca o dia como enviado se ainda não estava
            if not lead.proof_sent_today:
                lead.streak_days = (lead.streak_days or 0) + 1
                lead.proof_sent_today = True
            lead.last_bot_action = f"manual_approved_${volume:.2f}"

            try:
                from liga.scoring import calc_lead_score
                s.flush()
                lead.lead_score = calc_lead_score(lead, s)
            except Exception:
                pass

            s.commit()
            logger.info(
                "[liga] proof %d aprovado manualmente lead=%s volume=$%.2f",
                proof_id, lead.display_name, volume,
            )
        return RedirectResponse("/liga/review", status_code=302)

    @app.post("/liga/review/{proof_id}/reject")
    async def liga_review_reject(proof_id: int):
        """Rejeita um proof manualmente — fica registrado mas some da fila."""
        import json as _json
        with SessionLocal() as s:
            p = s.query(OperationProof).get(proof_id)
            if not p:
                raise HTTPException(404, "Proof não encontrado")
            ai = {}
            try:
                ai = _json.loads(p.raw_ai_response or "{}")
            except Exception:
                pass
            ai["rejected_reason"] = "manual_reject"
            ai["manual_rejected_at"] = datetime.utcnow().isoformat()
            p.raw_ai_response = _json.dumps(ai, ensure_ascii=False)
            s.commit()
            logger.info("[liga] proof %d rejeitado manualmente", proof_id)
        return RedirectResponse("/liga/review", status_code=302)

    @app.post("/proofs/{proof_id}/delete")
    async def proof_delete(proof_id: int):
        """Apaga um comprovante (caso anotação errada / valor digitado errado).

        Hard delete — se foi o único FTD do lead, recalcula engagement_tag.
        Use o botão 🗑 ao lado de cada comprovante no /liga/lead/X.
        """
        from datetime import datetime as _dt
        with SessionLocal() as s:
            proof = s.query(OperationProof).get(proof_id)
            if not proof:
                raise HTTPException(404, "Comprovante não encontrado")

            lead_id = proof.lead_id
            was_validated = proof.validated
            volume = proof.volume_usd or 0.0

            s.delete(proof)
            s.flush()

            # Se foi o último depósito validado do lead, recalcula engagement_tag
            if was_validated:
                lead = s.query(Lead).get(lead_id)
                if lead:
                    remaining_validated = (
                        s.query(OperationProof)
                        .filter(OperationProof.lead_id == lead_id)
                        .filter(OperationProof.validated.is_(True))
                        .count()
                    )
                    if remaining_validated == 0 and lead.engagement_tag == "deposited":
                        # Volta pra "account_no_deposit" se tinha conta validada,
                        # senão "first_contact_no_reply"
                        if lead.liga_id_status == "validated":
                            lead.engagement_tag = "account_no_deposit"
                        else:
                            lead.engagement_tag = None
                        lead.engagement_tag_updated_at = _dt.utcnow()
                        # Liga state volta pra waiting_deposit se estava active/waitlist
                        if lead.liga_state in ("active", "waitlist"):
                            lead.liga_state = "waiting_deposit"
                        logger.info(
                            "[proof_delete] lead=%s sem mais FTDs validados — voltou pra %s",
                            lead.display_name, lead.engagement_tag,
                        )

            s.commit()
            logger.info("[proof_delete] proof_id=%d (lead %d, $%.2f) excluído", proof_id, lead_id, volume)

        return RedirectResponse(f"/liga/lead/{lead_id}", status_code=303)

    @app.get("/liga/id-review", response_class=HTMLResponse)
    async def liga_id_review(request: Request):
        """Lista leads cujo ID na plataforma precisa de input/validação manual."""
        with SessionLocal() as s:
            # Leads precisando de revisão de ID:
            # - needs_review: bot não achou candidato
            # - invalid: candidato achado, mas @QuotexPartnerBot rejeitou
            # - extracted: achou mas validação falhou (timeout/erro)
            rows = (
                s.query(Lead)
                .filter(Lead.liga_id_status.in_(["needs_review", "invalid", "extracted"]))
                .order_by(desc(Lead.last_dm_at))
                .limit(200)
                .all()
            )
            items = []
            for l in rows:
                nome = (l.first_name or "") + (" " + l.last_name if l.last_name else "")
                nome = nome.strip() or (f"@{l.username}" if l.username else f"id:{l.telegram_id}")
                items.append({
                    "id": l.id,
                    "telegram_id": l.telegram_id,
                    "name": nome,
                    "username": l.username,
                    "candidate_id": l.liga_account_id,
                    "id_status": l.liga_id_status,
                    "last_dm_at": l.last_dm_at,
                    "partner_response": l.liga_id_partner_response,
                })
            counts = {
                "needs_review": s.query(Lead).filter(Lead.liga_id_status == "needs_review").count(),
                "invalid": s.query(Lead).filter(Lead.liga_id_status == "invalid").count(),
                "extracted": s.query(Lead).filter(Lead.liga_id_status == "extracted").count(),
                "validated": s.query(Lead).filter(Lead.liga_id_status == "validated").count(),
            }
        return templates.TemplateResponse(
            "liga_id_review.html",
            {"request": request, "items": items, "counts": counts},
        )

    @app.post("/liga/id-review/{lead_id}/set")
    async def liga_id_review_set(
        lead_id: int,
        manual_id: str = Form(...),
        run_validation: str = Form("0", alias="validate"),
    ):
        """Salva um ID inserido manualmente. Se validate=1, roda o partner bot na hora."""
        from userbot.leads import _looks_like_valid_id, ID_MIN_DIGITS, ID_MAX_DIGITS
        manual_id = "".join(ch for ch in (manual_id or "") if ch.isdigit())
        if not manual_id:
            return JSONResponse({"error": "ID precisa ser apenas dígitos"}, status_code=400)
        if not _looks_like_valid_id(manual_id):
            return JSONResponse(
                {"error": f"ID precisa ter entre {ID_MIN_DIGITS} e {ID_MAX_DIGITS} dígitos (recebeu {len(manual_id)})"},
                status_code=400,
            )

        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")
            lead.liga_account_id = manual_id[:100]

            if run_validation == "1":
                from userbot.client import get_client
                from userbot.leads import validate_id_via_partner_bot
                try:
                    client = await get_client()
                    val = await validate_id_via_partner_bot(client, manual_id)
                    lead.liga_id_partner_response = (val.get("raw") or "")[:4000]
                    if val.get("status") == "validated":
                        lead.liga_id_status = "validated"
                        lead.liga_id_country = (val.get("country") or "")[:50]
                        lead.liga_id_balance = val.get("balance")
                        lead.liga_id_deposits_sum = val.get("deposits_sum")
                        lead.liga_id_turnover = val.get("turnover")
                        lead.liga_id_validated_at = datetime.utcnow()
                    elif val.get("status") == "invalid":
                        lead.liga_id_status = "invalid"
                    else:
                        lead.liga_id_status = "extracted"
                except Exception as e:
                    logger.exception("[partner_bot] erro na validação manual")
                    lead.liga_id_status = "extracted"
                    return JSONResponse({"error": str(e)[:200]}, status_code=500)
            else:
                # Sem validação automática — admin marca como "extracted" pra revisão
                lead.liga_id_status = "extracted"

            s.commit()
            return JSONResponse({
                "ok": True,
                "lead_id": lead.id,
                "id_status": lead.liga_id_status,
                "country": lead.liga_id_country,
                "balance": lead.liga_id_balance,
                "deposits_sum": lead.liga_id_deposits_sum,
                "turnover": lead.liga_id_turnover,
            })

    @app.post("/liga/id-review/{lead_id}/skip")
    async def liga_id_review_skip(lead_id: int):
        """Remove o lead da fila marcando como 'skipped' (mantém em validated p/ não voltar)."""
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")
            lead.liga_id_status = "skipped"
            s.commit()
        return JSONResponse({"ok": True})

    # ----------------------------- Notas livres por lead
    @app.post("/leads/{lead_id}/quick-verify")
    async def lead_quick_verify(lead_id: int, scan_images: str = Form("1")):
        """Verificação rápida: extrai ID das últimas DMs (texto + 1 imagem)
        e valida no @QuotexPartnerBot. Roda só pra ESSE lead.

        Tempo esperado: 5-10s.
        """
        from userbot.client import get_client
        from userbot.leads import (
            find_recent_account_id_in_dms,
            validate_id_via_partner_bot,
            _looks_like_valid_id,
        )
        from datetime import datetime as _dt
        try:
            client = await get_client()
            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                if not lead:
                    raise HTTPException(404, "Lead não encontrado")
                tg_id = lead.telegram_id
                already_id = (lead.liga_account_id or "").strip()

            # Extrai
            cand = await find_recent_account_id_in_dms(
                client, tg_id, max_messages=30,
                scan_images=(scan_images == "1"),
                max_images=2,
            )
            cand_id = (cand.get("id") or "").strip()

            if not cand_id:
                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if lead and not lead.liga_id_status:
                        lead.liga_id_status = "needs_review"
                    s.commit()
                return JSONResponse({
                    "ok": True,
                    "found": False,
                    "message": "Nenhum ID encontrado nas últimas 30 DMs",
                })

            if not _looks_like_valid_id(cand_id):
                return JSONResponse({
                    "ok": True,
                    "found": True,
                    "valid_format": False,
                    "candidate_id": cand_id,
                    "message": f"Candidato '{cand_id}' fora da faixa 7-9 dígitos. Vai pra revisão manual.",
                })

            # Mismatch?
            if already_id and already_id != cand_id:
                return JSONResponse({
                    "ok": True,
                    "found": True,
                    "mismatch": True,
                    "registered_id": already_id,
                    "candidate_id": cand_id,
                    "message": f"⚠ ID divergente. Registrado: {already_id}, Novo: {cand_id}. Revise manualmente.",
                })

            # Valida via partner bot
            val = await validate_id_via_partner_bot(client, cand_id)

            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                if not lead:
                    raise HTTPException(404)
                lead.liga_account_id = cand_id[:100]
                lead.liga_id_partner_response = (val.get("raw") or "")[:4000]
                if val.get("status") == "validated":
                    lead.liga_id_status = "validated"
                    lead.liga_id_country = (val.get("country") or "")[:50]
                    lead.liga_id_balance = val.get("balance")
                    lead.liga_id_deposits_sum = val.get("deposits_sum")
                    lead.liga_id_turnover = val.get("turnover")
                    lead.liga_id_validated_at = _dt.utcnow()
                    try:
                        from liga.automation import _maybe_flag_vip
                        _maybe_flag_vip(lead)
                    except Exception:
                        pass
                elif val.get("status") == "invalid":
                    lead.liga_id_status = "invalid"
                else:
                    lead.liga_id_status = "extracted"
                s.commit()
                # Snapshot pra response
                response = {
                    "ok": True,
                    "found": True,
                    "valid_format": True,
                    "candidate_id": cand_id,
                    "source": cand.get("source"),
                    "partner_status": val.get("status"),
                    "country": lead.liga_id_country,
                    "balance": lead.liga_id_balance,
                    "deposits_sum": lead.liga_id_deposits_sum,
                    "turnover": lead.liga_id_turnover,
                    "is_vip": lead.is_vip_potential,
                    "id_status": lead.liga_id_status,
                }
            return JSONResponse(response)
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("[quick_verify] erro lead %s", lead_id)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/leads/{lead_id}/analyze")
    async def analyze_lead_now(lead_id: int):
        """Roda análise contextual via Haiku usando vault como cérebro."""
        from liga.contextual_analysis import analyze_lead_with_obsidian_context
        try:
            res = analyze_lead_with_obsidian_context(lead_id)
            return JSONResponse(res)
        except Exception as e:
            logger.exception("[analise] erro lead %s", lead_id)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/leads/{lead_id}/notes")
    async def update_lead_notes(lead_id: int, notes: str = Form("")):
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")
            lead.notes = (notes or "")[:5000]
            s.commit()
        return JSONResponse({"ok": True})

    # ------------------------- Anotação manual de depósito ---------------------
    @app.post("/leads/{lead_id}/manual-deposit")
    async def manual_deposit(
        lead_id: int,
        volume_usd: float = Form(...),
        proof_date: str = Form(...),
        platform: str = Form("Quotex"),
        account_id_raw: str = Form(""),
        review_notes: str = Form(""),
    ):
        """Anota depósito validado manualmente pelo admin.

        Use quando IA falhou em ler print, ou quando lead confirmou valor por outro canal.
        Conta como FTD validado e atualiza engagement_tag pro estado correto.
        """
        from datetime import datetime as _dt
        if volume_usd <= 0:
            return JSONResponse({"error": "Valor precisa ser > 0"}, status_code=400)
        try:
            # Valida formato data
            _dt.strptime(proof_date, "%Y-%m-%d")
        except ValueError:
            return JSONResponse({"error": "Data inválida (use YYYY-MM-DD)"}, status_code=400)

        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                raise HTTPException(404, "Lead não encontrado")

            # Cria OperationProof validado humanamente
            proof = OperationProof(
                lead_id=lead.id,
                proof_date=proof_date,
                volume_usd=float(volume_usd),
                account_id_raw=(account_id_raw or "").strip()[:100] or None,
                platform=(platform or "Quotex").strip()[:100],
                confidence="alta",  # validação humana = alta confiança
                validated=True,
                needs_review=False,
                review_reason="manual_input",
                validated_by="human",
                validated_at=_dt.utcnow(),
                review_notes=(review_notes or "").strip()[:2000] or None,
            )
            s.add(proof)

            # Atualiza engagement tag pra "deposited"
            lead.engagement_tag = "deposited"
            lead.engagement_tag_updated_at = _dt.utcnow()

            # Liga state: se ainda waiting_deposit / waitlist, vira active
            if lead.liga_state in ("waiting_deposit", "waitlist", "new"):
                if float(volume_usd) >= 100:
                    lead.liga_state = "active"
                else:
                    lead.liga_state = "waitlist"

            # Se tem ID de conta novo e lead ainda não tem registrado, registra
            if account_id_raw and not lead.liga_account_id:
                lead.liga_account_id = (account_id_raw or "").strip()[:100]

            # Marca todos os comprovantes desse lead que estavam needs_review como resolvidos
            # (a ação manual encerra a fila desse lead)
            pending = (
                s.query(OperationProof)
                .filter(OperationProof.lead_id == lead.id)
                .filter(OperationProof.needs_review.is_(True))
                .filter(OperationProof.id != proof.id)
                .all()
            )
            for old in pending:
                old.needs_review = False
                old.review_notes = (old.review_notes or "") + f" [resolvido por manual_deposit em {_dt.utcnow().strftime('%Y-%m-%d %H:%M')}]"

            s.commit()
            logger.info(
                "[manual_deposit] lead=%s valor=$%.2f data=%s by=human",
                lead.display_name, volume_usd, proof_date,
            )

        # Redireciona de volta pro detalhe do lead
        return RedirectResponse(f"/liga/lead/{lead_id}", status_code=303)

    # ------------------------- Verificações pendentes (fila revisão) ----------
    @app.get("/verifications/pending", response_class=HTMLResponse)
    async def verifications_pending(request: Request):
        """Lista de comprovantes que precisam revisão humana antes de contar.

        Inclui:
        - vision_failed (IA não leu valor/ID)
        - duplicate_image (anti-fraude)
        - id_mismatch (lead mandou ID diferente)
        - low_confidence (IA leu mas com confiança baixa)
        """
        from datetime import timedelta as _td
        cutoff = datetime.utcnow() - _td(days=14)  # mostra últimos 14 dias

        with SessionLocal() as s:
            pending = (
                s.query(OperationProof, Lead)
                .join(Lead, Lead.id == OperationProof.lead_id)
                .filter(OperationProof.needs_review.is_(True))
                .filter(OperationProof.created_at >= cutoff)
                .order_by(OperationProof.created_at.desc())
                .limit(200)
                .all()
            )
            # Conta por motivo
            from sqlalchemy import func as _func
            by_reason = (
                s.query(OperationProof.review_reason, _func.count(OperationProof.id))
                .filter(OperationProof.needs_review.is_(True))
                .group_by(OperationProof.review_reason)
                .all()
            )

            items = []
            for proof, lead in pending:
                items.append({
                    "proof_id": proof.id,
                    "lead_id": lead.id,
                    "lead_name": lead.display_name,
                    "lead_country": getattr(lead, "liga_id_country", None),
                    "lead_balance": getattr(lead, "liga_id_balance", None),
                    "review_reason": proof.review_reason or "outro",
                    "confidence": proof.confidence,
                    "volume_usd": float(proof.volume_usd or 0.0),
                    "proof_date": proof.proof_date,
                    "created_at": proof.created_at,
                    "review_notes": proof.review_notes,
                })

        return templates.TemplateResponse(
            "verifications_pending.html",
            {
                "request": request,
                "items": items,
                "by_reason": dict(by_reason),
                "total": len(items),
            },
        )

    @app.post("/verifications/{proof_id}/resolve")
    async def verifications_resolve(proof_id: int, action: str = Form("dismiss"), volume_usd: float = Form(0.0)):
        """Resolve um item da fila — 'dismiss' descarta, 'validate' aprova com valor."""
        from datetime import datetime as _dt
        with SessionLocal() as s:
            proof = s.query(OperationProof).get(proof_id)
            if not proof:
                raise HTTPException(404, "Comprovante não encontrado")

            proof.needs_review = False
            proof.validated_at = _dt.utcnow()

            if action == "validate":
                if volume_usd <= 0:
                    return JSONResponse({"error": "valor precisa > 0 pra validar"}, status_code=400)
                proof.volume_usd = float(volume_usd)
                proof.validated = True
                proof.validated_by = "human_after_ai_fail"
                # Atualiza lead
                lead = s.query(Lead).get(proof.lead_id)
                if lead:
                    lead.engagement_tag = "deposited"
                    lead.engagement_tag_updated_at = _dt.utcnow()
                    if lead.liga_state in ("waiting_deposit", "waitlist", "new"):
                        lead.liga_state = "active" if float(volume_usd) >= 100 else "waitlist"
            else:
                proof.validated = False
                proof.validated_by = "human"
                proof.review_notes = (proof.review_notes or "") + f" [dismissed em {_dt.utcnow().strftime('%Y-%m-%d %H:%M')}]"

            s.commit()
        return JSONResponse({"ok": True, "action": action})

    # ----------------------------- Bulk-actions em /leads
    @app.post("/leads/bulk")
    async def leads_bulk(
        action: str = Form(...),
        lead_ids: str = Form(...),
        value: str = Form(""),
    ):
        ids = [int(x) for x in lead_ids.split(",") if x.strip().isdigit()]
        if not ids:
            return JSONResponse({"error": "nenhum lead selecionado"}, status_code=400)

        valid_statuses = {st.value for st in LeadStatus}
        affected = 0
        with SessionLocal() as s:
            leads = s.query(Lead).filter(Lead.id.in_(ids)).all()
            for lead in leads:
                if action == "set_status":
                    if value in valid_statuses:
                        lead.status = value
                        affected += 1
                elif action == "set_engagement":
                    lead.engagement_tag = value or None
                    affected += 1
                elif action == "set_blocked":
                    lead.status = LeadStatus.BLOCKED.value
                    lead.opted_out = True
                    lead.opted_out_at = datetime.utcnow()
                    affected += 1
                elif action == "flag_vip":
                    lead.is_vip_potential = True
                    affected += 1
                elif action == "unflag_vip":
                    lead.is_vip_potential = False
                    affected += 1
                elif action == "mark_rewarm":
                    lead.rewarm_candidate = True
                    affected += 1
                elif action == "delete_engagement":
                    lead.engagement_tag = None
                    affected += 1
            s.commit()
        return JSONResponse({"ok": True, "affected": affected, "action": action})

    # ----------------------------- Cost dashboard
    @app.get("/metrics/ai", response_class=HTMLResponse)
    async def metrics_ai(request: Request):
        from sqlalchemy import func as _func
        from datetime import timedelta as _td
        with SessionLocal() as s:
            # Marco zero configurável — clique em "Zerar contadores" no painel
            # estabelece a data de início. Default: 30 dias atrás.
            baseline_row = s.query(Setting).filter_by(key="roi_baseline_date").one_or_none()
            if baseline_row and baseline_row.value:
                try:
                    cutoff = datetime.fromisoformat(baseline_row.value)
                    baseline_explicit = True
                except Exception:
                    cutoff = datetime.utcnow() - _td(days=30)
                    baseline_explicit = False
            else:
                cutoff = datetime.utcnow() - _td(days=30)
                baseline_explicit = False

            # Histórico TOTAL (todo o tempo, antes do reset) — só pra exibir contexto
            total_all_time = s.query(_func.coalesce(_func.sum(AIUsage.cost_usd), 0.0)).scalar() or 0.0
            total_in_all = s.query(_func.coalesce(_func.sum(AIUsage.input_tokens), 0)).scalar() or 0
            total_out_all = s.query(_func.coalesce(_func.sum(AIUsage.output_tokens), 0)).scalar() or 0

            # Total a partir do marco zero (substitui "total" antigo)
            total = s.query(_func.coalesce(_func.sum(AIUsage.cost_usd), 0.0)).filter(
                AIUsage.created_at >= cutoff
            ).scalar() or 0.0
            total_in = s.query(_func.coalesce(_func.sum(AIUsage.input_tokens), 0)).filter(
                AIUsage.created_at >= cutoff
            ).scalar() or 0
            total_out = s.query(_func.coalesce(_func.sum(AIUsage.output_tokens), 0)).filter(
                AIUsage.created_at >= cutoff
            ).scalar() or 0
            cached_calls = s.query(_func.count(AIUsage.id)).filter(
                AIUsage.cached.is_(True),
                AIUsage.created_at >= cutoff,
            ).scalar() or 0
            real_calls = s.query(_func.count(AIUsage.id)).filter(
                AIUsage.cached.is_(False),
                AIUsage.created_at >= cutoff,
            ).scalar() or 0

            # Cache savings — economia estimada com cache de imagem (a partir do marco)
            cache_savings = s.query(
                _func.coalesce(_func.sum(AIUsage.cost_usd), 0.0)
            ).filter(
                AIUsage.cached.is_(False),
                AIUsage.created_at >= cutoff,
                AIUsage.operation.in_(["analyze_account_screenshot", "analyze_proof_image"]),
            ).scalar() or 0.0
            avg_vision_cost = (cache_savings / max(real_calls, 1)) if real_calls > 0 else 0.0
            estimated_savings = avg_vision_cost * cached_calls
            by_op = (
                s.query(
                    AIUsage.operation,
                    _func.count(AIUsage.id),
                    _func.coalesce(_func.sum(AIUsage.cost_usd), 0.0),
                    _func.coalesce(_func.sum(AIUsage.input_tokens), 0),
                    _func.coalesce(_func.sum(AIUsage.output_tokens), 0),
                )
                .filter(AIUsage.created_at >= cutoff)
                .group_by(AIUsage.operation)
                .all()
            )
            # Por dia (últimos 30d)
            by_day = (
                s.query(
                    _func.date(AIUsage.created_at).label("d"),
                    _func.coalesce(_func.sum(AIUsage.cost_usd), 0.0),
                    _func.count(AIUsage.id),
                )
                .filter(AIUsage.created_at >= cutoff)
                .group_by(_func.date(AIUsage.created_at))
                .order_by(_func.date(AIUsage.created_at).desc())
                .limit(30).all()
            )

            # ========== ROI / Cost per conversion (a partir do marco zero) ==========
            # Conversões a partir do marco — preferimos OperationProof validados
            # (deposits manuais ou via IA validada) + entrada no grupo privado
            ftd_count_period = s.query(_func.count(_func.distinct(OperationProof.lead_id))).filter(
                OperationProof.validated.is_(True),
                OperationProof.created_at >= cutoff,
            ).scalar() or 0

            group_joins_period = s.query(_func.count(Lead.id)).filter(
                Lead.in_private_group.is_(True),
                Lead.updated_at >= cutoff,
            ).scalar() or 0

            # Conversões = max das duas (FTD validado OU entrada no grupo)
            # Pessoas que fizeram FTD geralmente entram no grupo, então pega o maior
            conversions_30d = max(ftd_count_period, group_joins_period)

            # Custo no mesmo período (a partir do marco)
            cost_30d = total  # já calculado acima usando cutoff

            cost_per_conversion = (cost_30d / conversions_30d) if conversions_30d > 0 else 0.0

            # Custo por sends (alternativa: leads que receberam alguma DM)
            sends_30d = s.query(_func.count(Send.id)).filter(
                Send.sent_at >= cutoff,
                Send.status == SendStatus.SENT.value,
            ).scalar() or 0
            cost_per_send = (cost_30d / sends_30d) if sends_30d > 0 else 0.0

            # Custo por lead processado (entradas em LeadMessage)
            from db.models import LeadMessage
            leads_processed_30d = s.query(_func.count(_func.distinct(LeadMessage.lead_id))).filter(
                LeadMessage.created_at >= cutoff
            ).scalar() or 0
            cost_per_lead = (cost_30d / leads_processed_30d) if leads_processed_30d > 0 else 0.0

            # ROI estimado em BRL — comissão por FTD (afiliado Quotex)
            # Range pessimista (BRL_MIN) → otimista (BRL_MAX)
            commission_brl_min = float(os.getenv("AVG_COMMISSION_BRL_MIN", "5"))
            commission_brl_max = float(os.getenv("AVG_COMMISSION_BRL_MAX", "8"))
            usd_brl_rate = float(os.getenv("USD_BRL_RATE", "5.0"))

            # Converte custo Anthropic (USD) pra BRL pra comparar mesma moeda
            cost_30d_brl = cost_30d * usd_brl_rate

            # Receita estimada em BRL (range)
            revenue_brl_min = conversions_30d * commission_brl_min
            revenue_brl_max = conversions_30d * commission_brl_max

            # ROI multiplier (em BRL, divisão direta)
            roi_min = (revenue_brl_min / cost_30d_brl) if cost_30d_brl > 0 else 0.0
            roi_max = (revenue_brl_max / cost_30d_brl) if cost_30d_brl > 0 else 0.0

            # Lucro líquido estimado em BRL
            profit_brl_min = revenue_brl_min - cost_30d_brl
            profit_brl_max = revenue_brl_max - cost_30d_brl

            # Custo por conversão em BRL
            cost_per_conversion_brl = (cost_30d_brl / conversions_30d) if conversions_30d > 0 else 0.0

        # FTDs validados no período (humano + IA)
        with SessionLocal() as s:
            ftd_human = s.query(_func.count(OperationProof.id)).filter(
                OperationProof.validated.is_(True),
                OperationProof.validated_by.in_(["human", "human_after_ai_fail"]),
                OperationProof.created_at >= cutoff,
            ).scalar() or 0
            ftd_ai = s.query(_func.count(OperationProof.id)).filter(
                OperationProof.validated.is_(True),
                OperationProof.validated_by == "ai",
                OperationProof.created_at >= cutoff,
            ).scalar() or 0
            ftd_total_volume_usd = float(s.query(
                _func.coalesce(_func.sum(OperationProof.volume_usd), 0.0)
            ).filter(
                OperationProof.validated.is_(True),
                OperationProof.created_at >= cutoff,
            ).scalar() or 0.0)

        # Dias decorridos desde o marco
        days_since = max(1, int((datetime.utcnow() - cutoff).total_seconds() / 86400))

        return templates.TemplateResponse(
            "metrics_ai.html",
            {
                "request": request,
                "total": total, "total_in": total_in, "total_out": total_out,
                "total_all_time": total_all_time,
                "total_in_all": total_in_all, "total_out_all": total_out_all,
                "cached_calls": cached_calls, "real_calls": real_calls,
                "estimated_savings": estimated_savings,
                "by_op": [{"op": r[0], "calls": r[1], "cost": float(r[2]), "in_tokens": r[3], "out_tokens": r[4]} for r in by_op],
                "by_day": [{"date": r[0], "cost": float(r[1]), "calls": r[2]} for r in by_day],
                # Marco zero
                "baseline_explicit": baseline_explicit,
                "baseline_date": cutoff,
                "days_since_baseline": days_since,
                # FTDs validados
                "ftd_count_period": ftd_count_period,
                "ftd_human": ftd_human,
                "ftd_ai": ftd_ai,
                "ftd_total_volume_usd": ftd_total_volume_usd,
                "group_joins_period": group_joins_period,
                # ROI
                "conversions_30d": conversions_30d,
                "cost_30d": cost_30d,
                "cost_per_conversion": cost_per_conversion,
                "cost_per_send": cost_per_send,
                "cost_per_lead": cost_per_lead,
                "leads_processed_30d": leads_processed_30d,
                "sends_30d": sends_30d,
                # ROI em BRL
                "commission_brl_min": commission_brl_min,
                "commission_brl_max": commission_brl_max,
                "usd_brl_rate": usd_brl_rate,
                "cost_30d_brl": cost_30d_brl,
                "revenue_brl_min": revenue_brl_min,
                "revenue_brl_max": revenue_brl_max,
                "roi_min": roi_min,
                "roi_max": roi_max,
                "profit_brl_min": profit_brl_min,
                "profit_brl_max": profit_brl_max,
                "cost_per_conversion_brl": cost_per_conversion_brl,
            },
        )

    @app.post("/metrics/ai/reset")
    async def metrics_ai_reset():
        """Marca o ponto-zero pra ROI a partir de AGORA.

        Não apaga histórico — apenas estabelece um cutoff. Tudo antes vira
        'histórico antigo' e tudo depois conta pro ROI atual.
        """
        from datetime import datetime as _dt
        with SessionLocal() as s:
            now_iso = _dt.utcnow().isoformat()
            row = s.query(Setting).filter_by(key="roi_baseline_date").one_or_none()
            if row:
                row.value = now_iso
            else:
                s.add(Setting(key="roi_baseline_date", value=now_iso))
            s.commit()
        return RedirectResponse("/metrics/ai", status_code=303)

    @app.post("/metrics/ai/clear-baseline")
    async def metrics_ai_clear_baseline():
        """Remove o marco zero — volta pro default (últimos 30 dias)."""
        with SessionLocal() as s:
            s.query(Setting).filter_by(key="roi_baseline_date").delete()
            s.commit()
        return RedirectResponse("/metrics/ai", status_code=303)

    # ----------------------------- Análise razão de não-deposit
    @app.get("/metrics/no-deposit-reasons", response_class=HTMLResponse)
    async def metrics_no_deposit(request: Request):
        from liga.no_deposit_analysis import get_cached_analysis
        analysis = get_cached_analysis()
        return templates.TemplateResponse(
            "metrics_no_deposit.html",
            {"request": request, "analysis": analysis},
        )

    @app.post("/metrics/no-deposit-reasons/run")
    async def run_no_deposit_analysis():
        from liga.no_deposit_analysis import task_analyze_no_deposit_reasons
        try:
            res = await task_analyze_no_deposit_reasons()
            return JSONResponse(res)
        except Exception as e:
            logger.exception("[no_deposit] erro")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ----------------------------- Review pré-disparo em massa
    @app.get("/scripts/{script_id}/preview-dispatch", response_class=HTMLResponse)
    async def script_preview_dispatch(
        request: Request,
        script_id: int,
        page: int = 1,
        per_page: int = 100,
        max_leads: int = 0,           # cap aplicado igual ao disparo (0 = sem cap)
        stage_override: str = "",      # stage filter passado do form (pode override script.target)
    ):
        """Preview de quem receberia se você disparasse esse script agora.

        Aplica os filtros do form (max_leads, stage) + filtros do script
        (target_remarketing_stage + target_engagement_tag).

        Lista paginada com checkboxes pra excluir leads específicos do disparo.
        Ordenação: last_dm_at DESC (mesmo do disparo real).
        """
        from sqlalchemy import func as _func
        from liga.remarketing_stage import is_eligible_for_dispatch
        from db.models import ScriptExcludedLead

        with SessionLocal() as s:
            script = s.query(Script).get(script_id)
            if not script:
                raise HTTPException(404, "Script não encontrado")

            # IDs já excluídos manualmente desse script
            excluded_ids = set(
                row[0] for row in s.query(ScriptExcludedLead.lead_id)
                .filter(ScriptExcludedLead.script_id == script_id).all()
            )

            # Constrói filtro — aplicando MESMOS filtros do disparo real
            q = s.query(Lead).filter(
                Lead.opted_out.is_(False),
                Lead.in_private_group.is_(False),
                Lead.status.notin_([
                    LeadStatus.BLOCKED.value, LeadStatus.EXCLUDED.value,
                ]),
            )
            # Stage: prioridade pro override do form, senão usa target do script
            effective_stage = (stage_override or "").strip() or script.target_remarketing_stage
            if effective_stage:
                q = q.filter(Lead.remarketing_stage == effective_stage)
            if script.target_engagement_tag:
                q = q.filter(Lead.engagement_tag == script.target_engagement_tag)

            # Ordena igual ao disparo real (last_dm_at DESC)
            from sqlalchemy import desc as _desc
            q = q.order_by(_desc(Lead.last_dm_at))

            # Cap inicial alto pra ter espaço pra filtrar fresh/cooldown
            # depois aplica max_leads sobre os elegíveis
            all_candidates = q.limit(max(2000, max_leads * 3 if max_leads > 0 else 2000)).all()

            # Separa em 4 buckets:
            # - eligible_active: vai receber
            # - eligible_excluded: marcado como excluído manualmente
            # - skipped_fresh: msg recente (<24h)
            # - skipped_other: cooldown / opt-out / etc
            eligible_active = []
            eligible_excluded = []
            skipped_fresh = []
            skipped_other = []
            for l in all_candidates:
                ok, reason = is_eligible_for_dispatch(l)
                if ok:
                    if l.id in excluded_ids:
                        eligible_excluded.append(l)
                    else:
                        eligible_active.append(l)
                elif "fresh" in reason or l.is_fresh:
                    skipped_fresh.append(l)
                else:
                    skipped_other.append((l, reason))

            # Aplica cap max_leads sobre os elegíveis ativos (mesmo cap do disparo)
            total_eligible_before_cap = len(eligible_active)
            capped_by_max = False
            if max_leads and max_leads > 0 and len(eligible_active) > max_leads:
                eligible_active = eligible_active[:max_leads]
                capped_by_max = True

            # Breakdown por país
            from collections import Counter
            countries = Counter()
            for l in eligible_active:
                countries[l.liga_id_country or "(desconhecido)"] += 1

            # Stage breakdown (em quais stages estão os elegíveis ativos)
            stages = Counter()
            for l in eligible_active:
                stages[l.remarketing_stage or "untouched"] += 1

            # VIPs
            vips = [l for l in eligible_active if l.is_vip_potential]
            rewarms = [l for l in eligible_active if l.rewarm_candidate]

            # Preview do texto de uma variante (primeira ativa)
            variant = (
                s.query(ScriptVariant)
                .filter(ScriptVariant.script_id == script_id)
                .filter(ScriptVariant.is_active.is_(True))
                .first()
            )
            preview_text = (variant.text_es if variant else None) or "_(sem variante ativa)_"

            # Paginação da lista detalhada
            per_page = max(20, min(500, per_page))
            page = max(1, page)
            total_pages = max(1, (len(eligible_active) + per_page - 1) // per_page)
            page = min(page, total_pages)
            start_idx = (page - 1) * per_page
            end_idx = start_idx + per_page

            # Enriquece os leads da página atual pra exibição
            page_leads = []
            for l in eligible_active[start_idx:end_idx]:
                page_leads.append({
                    "id": l.id,
                    "display_name": l.display_name,
                    "country": l.liga_id_country,
                    "balance": l.liga_id_balance,
                    "deposits_sum": l.liga_id_deposits_sum,
                    "stage": l.remarketing_stage or "untouched",
                    "engagement_tag": l.engagement_tag,
                    "is_vip": bool(l.is_vip_potential),
                    "rewarm": bool(l.rewarm_candidate),
                    "last_dm_at": l.last_dm_at,
                })

            # Leads excluídos (mostra todos, sem paginar — geralmente poucos)
            excluded_leads_data = []
            for l in eligible_excluded:
                excluded_leads_data.append({
                    "id": l.id,
                    "display_name": l.display_name,
                    "country": l.liga_id_country,
                    "is_vip": bool(l.is_vip_potential),
                    "stage": l.remarketing_stage or "untouched",
                })

        return templates.TemplateResponse(
            "preview_dispatch.html",
            {
                "request": request,
                "script": script,
                "eligible_count": len(eligible_active),
                "excluded_count": len(eligible_excluded),
                "skipped_fresh_count": len(skipped_fresh),
                "skipped_other_count": len(skipped_other),
                "skipped_other": skipped_other[:10],
                "skipped_fresh": skipped_fresh[:5],
                "by_country": dict(countries.most_common()),
                "by_stage": dict(stages),
                "vips_count": len(vips),
                "rewarms_count": len(rewarms),
                "preview_text": preview_text,
                "variant_label": variant.label if variant else None,
                # Listas detalhadas + paginação
                "page_leads": page_leads,
                "page": page,
                "per_page": per_page,
                "total_pages": total_pages,
                "excluded_leads": excluded_leads_data,
                # Filtros aplicados (preserva entre paginação)
                "max_leads": max_leads,
                "stage_override": stage_override,
                "effective_stage": effective_stage,
                "capped_by_max": capped_by_max,
                "total_eligible_before_cap": total_eligible_before_cap,
            },
        )

    @app.post("/scripts/{script_id}/exclude-leads")
    async def script_exclude_leads(script_id: int, lead_ids: str = Form(...), reason: str = Form("manual")):
        """Marca leads pra serem excluídos do próximo disparo desse script.

        lead_ids: CSV de ints (ex: "1,2,3")
        reason: 'manual' / 'vip' / 'duplicate' / etc
        """
        from db.models import ScriptExcludedLead
        ids = [int(x) for x in (lead_ids or "").split(",") if x.strip().isdigit()]
        if not ids:
            return JSONResponse({"error": "nenhum lead selecionado"}, status_code=400)

        added = 0
        with SessionLocal() as s:
            sc = s.query(Script).get(script_id)
            if not sc:
                return JSONResponse({"error": "Script não encontrado"}, status_code=404)
            existing = set(
                row[0] for row in s.query(ScriptExcludedLead.lead_id)
                .filter(ScriptExcludedLead.script_id == script_id)
                .filter(ScriptExcludedLead.lead_id.in_(ids)).all()
            )
            for lid in ids:
                if lid in existing:
                    continue
                s.add(ScriptExcludedLead(
                    script_id=script_id, lead_id=lid,
                    reason=(reason or "manual")[:200],
                ))
                added += 1
            s.commit()
        return JSONResponse({"ok": True, "excluded": added, "already_excluded": len(ids) - added})

    @app.post("/scripts/{script_id}/include-leads")
    async def script_include_leads(script_id: int, lead_ids: str = Form(...)):
        """Re-inclui leads previamente excluídos (remove da tabela ScriptExcludedLead)."""
        from db.models import ScriptExcludedLead
        ids = [int(x) for x in (lead_ids or "").split(",") if x.strip().isdigit()]
        if not ids:
            return JSONResponse({"error": "nenhum lead selecionado"}, status_code=400)

        with SessionLocal() as s:
            removed = (
                s.query(ScriptExcludedLead)
                .filter(ScriptExcludedLead.script_id == script_id)
                .filter(ScriptExcludedLead.lead_id.in_(ids))
                .delete(synchronize_session=False)
            )
            s.commit()
        return JSONResponse({"ok": True, "removed": removed})

    # ----------------------------- A/B test estatístico
    @app.get("/scripts/{script_id}/ab-test")
    async def script_ab_test(script_id: int):
        """Retorna análise A/B entre todas as variantes ativas do script."""
        from liga.ab_test import all_ab_tests_for_script
        try:
            results = all_ab_tests_for_script(script_id)
            return JSONResponse({"ok": True, "comparisons": results})
        except Exception as e:
            logger.exception("[ab_test] erro")
            return JSONResponse({"error": str(e)}, status_code=500)

    # ----------------------------- Library de scripts vencedores
    @app.get("/script-winners", response_class=HTMLResponse)
    async def scripts_winners(
        request: Request,
        min_sends: int = 30,
        days: int = 90,
        country_filter: str = "",
        stage_filter: str = "",
        engagement_filter: str = "",
    ):
        """Top scripts/variantes ordenados por reply rate."""
        from sqlalchemy import func as _func, and_, or_
        from datetime import timedelta as _td

        cutoff = datetime.utcnow() - _td(days=days)

        with SessionLocal() as s:
            # Filtros aplicados às Sends
            sends_q = s.query(
                Send.script_id,
                Send.variant_id,
                _func.count(Send.id).label("sends"),
                _func.sum(_func.cast(Send.replied, Integer)).label("replies"),
            ).filter(
                Send.status == SendStatus.SENT.value,
                Send.sent_at >= cutoff,
            )

            if country_filter or stage_filter or engagement_filter:
                sends_q = sends_q.join(Lead, Lead.id == Send.lead_id)
                if country_filter:
                    sends_q = sends_q.filter(Lead.liga_id_country == country_filter)
                if stage_filter:
                    sends_q = sends_q.filter(Lead.remarketing_stage == stage_filter)
                if engagement_filter:
                    sends_q = sends_q.filter(Lead.engagement_tag == engagement_filter)

            sends_q = sends_q.group_by(Send.script_id, Send.variant_id).having(
                _func.count(Send.id) >= min_sends
            )

            rows = sends_q.all()

            # Constrói lista enriquecida com nome do script + variante
            winners = []
            for r in rows:
                sc = s.query(Script).get(r.script_id) if r.script_id else None
                var = s.query(ScriptVariant).get(r.variant_id) if r.variant_id else None
                if not sc:
                    continue
                replies = int(r.replies or 0)
                reply_rate = (replies / r.sends) if r.sends else 0
                # Conversões (lead virou in_private_group depois desse send)
                conv_count = (
                    s.query(_func.count(Send.id))
                    .filter(Send.script_id == r.script_id)
                    .filter(Send.variant_id == r.variant_id)
                    .filter(Send.reply_classification == "conversion")
                    .filter(Send.sent_at >= cutoff)
                    .scalar() or 0
                )
                conv_rate = (conv_count / r.sends) if r.sends else 0
                winners.append({
                    "script_id": sc.id,
                    "script_name": sc.name,
                    "variant_label": var.label if var else "—",
                    "variant_id": var.id if var else None,
                    "preview": ((var.text_es if var else "") or sc.briefing_pt or "")[:140],
                    "sends": r.sends,
                    "replies": replies,
                    "reply_rate": reply_rate,
                    "conversions": conv_count,
                    "conv_rate": conv_rate,
                    "stage": sc.target_remarketing_stage,
                    "engagement": sc.target_engagement_tag,
                })

            winners.sort(key=lambda x: (x["reply_rate"], x["conversions"]), reverse=True)

            # Países disponíveis pra filtro
            countries = [r[0] for r in s.query(Lead.liga_id_country).filter(
                Lead.liga_id_country.isnot(None)
            ).distinct().all() if r[0]]

        return templates.TemplateResponse(
            "scripts_winners.html",
            {
                "request": request, "winners": winners,
                "min_sends": min_sends, "days": days,
                "country_filter": country_filter,
                "stage_filter": stage_filter,
                "engagement_filter": engagement_filter,
                "countries": sorted(countries),
            },
        )

    # ----------------------------- Calendário de campanhas
    @app.get("/campaign-calendar", response_class=HTMLResponse)
    async def campaigns_calendar(request: Request, year: int = 0, month: int = 0):
        """Visualização mensal de campanhas + eventos da Liga (checkpoints)."""
        from datetime import date as _date, timedelta as _td
        import calendar as _cal

        today = datetime.utcnow().date()
        if not year:
            year = today.year
        if not month:
            month = today.month

        first_day = _date(year, month, 1)
        days_in_month = _cal.monthrange(year, month)[1]
        last_day = _date(year, month, days_in_month)

        # Eventos do mês
        events = {}  # data → lista de eventos

        with SessionLocal() as s:
            # Campanhas agendadas
            camps = (
                s.query(Campaign)
                .filter(Campaign.scheduled_at >= first_day)
                .filter(Campaign.scheduled_at < last_day + _td(days=1))
                .all()
            )
            for c in camps:
                d = c.scheduled_at.date()
                events.setdefault(d, []).append({
                    "type": "campaign",
                    "icon": "✉",
                    "title": (c.name or f"Camp #{c.id}")[:40],
                    "subtitle": f"status: {c.status}",
                    "url": f"/campaigns/{c.id}",
                    "color": "primary" if c.status == "running" else "secondary",
                })

            # Campanhas que JÁ rodaram no mês (started_at)
            past_camps = (
                s.query(Campaign)
                .filter(Campaign.started_at >= first_day)
                .filter(Campaign.started_at < last_day + _td(days=1))
                .filter(Campaign.scheduled_at.is_(None))
                .all()
            )
            for c in past_camps:
                d = c.started_at.date()
                events.setdefault(d, []).append({
                    "type": "campaign_done",
                    "icon": "✅",
                    "title": (c.name or f"Camp #{c.id}")[:40],
                    "subtitle": f"executada · {c.status}",
                    "url": f"/campaigns/{c.id}",
                    "color": "success",
                })

        # Eventos da Liga (checkpoints + datas marco)
        liga_start = os.getenv("LIGA_START_DATE", "").strip()
        liga_end = os.getenv("LIGA_END_DATE", "").strip()

        def _try_date(s):
            try:
                return datetime.strptime(s, "%Y-%m-%d").date()
            except Exception:
                return None

        from datetime import timedelta as _td2
        if liga_start and _try_date(liga_start):
            d = _try_date(liga_start)
            if first_day <= d <= last_day:
                events.setdefault(d, []).append({
                    "type": "liga", "icon": "🏆", "title": "Liga · INÍCIO",
                    "subtitle": "torneio começa", "color": "warning", "url": "/liga",
                })
            # Checkpoints CP1=+6d CP2=+13d CP3=+20d
            for offset, name in [(6, "CP #1"), (13, "CP #2"), (20, "CP #3")]:
                cp = d + _td2(days=offset)
                if first_day <= cp <= last_day:
                    events.setdefault(cp, []).append({
                        "type": "liga", "icon": "🏁",
                        "title": f"Liga · {name}", "subtitle": "checkpoint",
                        "color": "warning", "url": "/liga",
                    })

        if liga_end and _try_date(liga_end):
            d = _try_date(liga_end)
            if first_day <= d <= last_day:
                events.setdefault(d, []).append({
                    "type": "liga", "icon": "🏆", "title": "Liga · FINAL",
                    "subtitle": "corte final", "color": "danger", "url": "/liga",
                })

        # Constrói grade de calendário
        cal_obj = _cal.Calendar(firstweekday=0)  # segunda
        weeks = cal_obj.monthdatescalendar(year, month)

        # Navegação prev/next
        if month == 1:
            prev_y, prev_m = year - 1, 12
        else:
            prev_y, prev_m = year, month - 1
        if month == 12:
            next_y, next_m = year + 1, 1
        else:
            next_y, next_m = year, month + 1

        month_names = ["Janeiro","Fevereiro","Março","Abril","Maio","Junho",
                       "Julho","Agosto","Setembro","Outubro","Novembro","Dezembro"]

        return templates.TemplateResponse(
            "campaigns_calendar.html",
            {
                "request": request, "year": year, "month": month,
                "month_name": month_names[month - 1],
                "weeks": weeks, "events": events, "today": today,
                "prev_y": prev_y, "prev_m": prev_m,
                "next_y": next_y, "next_m": next_m,
                "first_day": first_day, "last_day": last_day,
            },
        )

    # ----------------------------- Account warming meter (JSON pra UI)
    @app.get("/api/health")
    async def api_health():
        from liga.automation import get_account_health
        return JSONResponse(get_account_health())

    # ----------------------------- Trigger manual dos novos crons
    # ===== FUNIL AUTOMATIZADO + AGENTE ====================================
    @app.get("/automation", response_class=HTMLResponse)
    async def automation_dashboard(request: Request):
        """Painel principal — funis + agente, todos juntos."""
        from db.models import Funnel, AgentSuggestion
        from liga.agent.learning import get_learning_stats, AGENT_CATEGORIES
        from liga.agent.suggester import is_agent_active

        with SessionLocal() as s:
            funnels = s.query(Funnel).order_by(Funnel.created_at.desc()).all()
            funnel_data = [{
                "id": f.id, "name": f.name, "description": f.description,
                "is_active": f.is_active, "is_dry_run": f.is_dry_run,
                "steps_count": len(f.steps),
            } for f in funnels]

            pending_suggestions = s.query(AgentSuggestion).filter(
                AgentSuggestion.status == "pending"
            ).count()

        learning_stats = get_learning_stats()
        agent_active = is_agent_active()

        return templates.TemplateResponse(
            "automation_dashboard.html",
            {
                "request": request,
                "funnels": funnel_data,
                "learning_stats": learning_stats,
                "agent_categories": AGENT_CATEGORIES,
                "agent_active": agent_active,
                "pending_suggestions": pending_suggestions,
            },
        )

    @app.get("/funnel", response_class=HTMLResponse)
    async def funnel_list(request: Request):
        return RedirectResponse("/automation", status_code=302)

    @app.post("/funnel/new")
    async def funnel_new(name: str = Form(...), description: str = Form("")):
        from db.models import Funnel
        with SessionLocal() as s:
            f = Funnel(name=name.strip(), description=description.strip() or None,
                       is_active=False, is_dry_run=True)
            s.add(f)
            s.commit()
            s.refresh(f)
            fid = f.id
        return RedirectResponse(f"/funnel/{fid}", status_code=303)

    @app.post("/funnel/create-template/vip-aquisicao")
    async def funnel_template_vip(force: str = Form("")):
        """Cria funil VIP de aquisição completo com 1 clique.

        Se 'force=1' no body, APAGA funil existente e recria com template atual
        (use após atualização do template_vip).

        Idempotente sem force: se já existir, redireciona pro existente.
        """
        from liga.funnel.templates import create_vip_aquisicao_funnel
        try:
            force_recreate = force == "1"
            res = create_vip_aquisicao_funnel(force_recreate=force_recreate)
            if res.get("ok"):
                return RedirectResponse(f"/funnel/{res['funnel_id']}?created=template", status_code=303)
            return JSONResponse({"error": "falha ao criar template"}, status_code=500)
        except Exception as e:
            logger.exception("[template vip] erro")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/funnel/update-template-texts/vip-aquisicao")
    async def funnel_update_vip_texts():
        """Atualiza só os textos das variantes do template VIP no banco
        (preserva etapas, mídia, config). Use depois de mudar textos no
        template_vip pra propagar pra um funil já existente.
        """
        from liga.funnel.templates import update_vip_aquisicao_texts
        try:
            res = update_vip_aquisicao_texts()
            return JSONResponse(res, status_code=200 if res.get("ok") else 400)
        except Exception as e:
            logger.exception("[template vip] erro ao atualizar textos")
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/funnel/{funnel_id}", response_class=HTMLResponse)
    async def funnel_edit(request: Request, funnel_id: int):
        from db.models import Funnel, FunnelStep, Script, ScriptVariant, ScriptMedia
        import json as _json

        with SessionLocal() as s:
            f = s.query(Funnel).get(funnel_id)
            if not f:
                raise HTTPException(404, "Funnel não encontrado")

            steps_data = []
            for step in f.steps:
                script_ids = []
                media_ids = []
                try:
                    script_ids = _json.loads(step.script_ids_json or "[]")
                except Exception:
                    pass
                try:
                    media_ids = _json.loads(step.media_ids_json or "[]")
                except Exception:
                    pass
                # Resolve scripts e mídias pra mostrar nomes
                scripts_resolved = []
                for sid in script_ids:
                    v = s.query(ScriptVariant).get(sid)
                    if v:
                        scripts_resolved.append({
                            "id": v.id, "label": v.label,
                            "text_preview": (v.text_es or "")[:80],
                            "text_full": v.text_es or "",  # texto completo pra editor inline
                            "script_name": v.script.name if v.script else "?",
                            "script_id": v.script_id,
                        })
                medias_resolved = []
                for mid in media_ids:
                    m = s.query(ScriptMedia).get(mid)
                    if m:
                        medias_resolved.append({
                            "id": m.id, "filename": m.filename,
                            "kind": m.kind, "video_note": m.video_note,
                        })
                steps_data.append({
                    "id": step.id,
                    "source_state": step.source_state,
                    "trigger_intent": step.trigger_intent,
                    "target_state": step.target_state,
                    "delay_min": step.delay_min, "delay_max": step.delay_max,
                    "delay_between_min": step.delay_between_min,
                    "delay_between_max": step.delay_between_max,
                    "extra_action": step.extra_action,
                    "media_position": step.media_position or "before",
                    "order_index": step.order_index,
                    "scripts": scripts_resolved,
                    "medias": medias_resolved,
                })

            # Lista de scripts/variantes/mídia disponíveis pra adicionar
            all_variants = s.query(ScriptVariant).filter(ScriptVariant.is_active.is_(True)).all()
            variants_options = [{
                "id": v.id, "label": v.label,
                "script_name": v.script.name if v.script else "?",
                "text_preview": (v.text_es or "")[:80],
            } for v in all_variants]

            all_medias = s.query(ScriptMedia).all()
            medias_options = [{
                "id": m.id, "filename": m.filename, "kind": m.kind,
                "video_note": bool(m.video_note),
                "script_name": m.script.name if m.script else "?",
            } for m in all_medias]

            # Detecta se está em modo teste (config tem test_mode_username)
            import json as _json_funnel
            try:
                _cfg = _json_funnel.loads(f.config_json or "{}")
            except Exception:
                _cfg = {}
            test_mode_user = (_cfg.get("test_mode_username") or "").strip()

            funnel_data = {
                "id": f.id, "name": f.name, "description": f.description,
                "is_active": f.is_active, "is_dry_run": f.is_dry_run,
                "config_json": f.config_json or "",
                "test_mode_username": test_mode_user,
            }

        # States e intents disponíveis
        from db.models import LigaState
        from liga.funnel.classifier import ALL_INTENTS, INTENTS_BY_STATE
        all_states = [st.value for st in LigaState]

        return templates.TemplateResponse(
            "funnel_edit.html",
            {
                "request": request,
                "funnel": funnel_data,
                "steps": steps_data,
                "variants_options": variants_options,
                "medias_options": medias_options,
                "all_states": all_states,
                "all_intents": ALL_INTENTS,
                "intents_by_state": INTENTS_BY_STATE,
            },
        )

    @app.post("/funnel/{funnel_id}/toggle")
    async def funnel_toggle(funnel_id: int):
        from db.models import Funnel
        with SessionLocal() as s:
            # Desativa todos os outros (apenas 1 ativo por vez)
            f = s.query(Funnel).get(funnel_id)
            if not f:
                raise HTTPException(404, "Funnel não encontrado")
            new_state = not bool(f.is_active)
            if new_state:
                # Desativa os outros
                s.query(Funnel).filter(Funnel.id != funnel_id).update(
                    {Funnel.is_active: False}, synchronize_session=False
                )
            f.is_active = new_state
            s.commit()
        return RedirectResponse(f"/funnel/{funnel_id}", status_code=303)

    @app.post("/funnel/{funnel_id}/dry-run-toggle")
    async def funnel_dry_run_toggle(funnel_id: int):
        from db.models import Funnel
        with SessionLocal() as s:
            f = s.query(Funnel).get(funnel_id)
            if not f:
                raise HTTPException(404)
            f.is_dry_run = not bool(f.is_dry_run)
            s.commit()
        return RedirectResponse(f"/funnel/{funnel_id}", status_code=303)

    @app.post("/funnel/{funnel_id}/edit")
    async def funnel_save(funnel_id: int, name: str = Form(...), description: str = Form(""),
                          config_json: str = Form("")):
        from db.models import Funnel
        import json as _json
        # Valida JSON
        if config_json.strip():
            try:
                _json.loads(config_json)
            except Exception:
                return _err_html("config_json inválido (deve ser JSON válido)", f"/funnel/{funnel_id}")
        with SessionLocal() as s:
            f = s.query(Funnel).get(funnel_id)
            if not f:
                raise HTTPException(404)
            f.name = name.strip()
            f.description = description.strip() or None
            f.config_json = config_json.strip() or None
            s.commit()
        return RedirectResponse(f"/funnel/{funnel_id}", status_code=303)

    @app.post("/funnel/{funnel_id}/delete")
    async def funnel_delete(funnel_id: int):
        from db.models import Funnel
        with SessionLocal() as s:
            f = s.query(Funnel).get(funnel_id)
            if f:
                s.delete(f)
                s.commit()
        return RedirectResponse("/automation", status_code=303)

    @app.post("/funnel/{funnel_id}/step/new")
    async def funnel_step_new(
        funnel_id: int,
        source_state: str = Form(...),
        trigger_intent: str = Form(...),
        target_state: str = Form(...),
        script_ids_csv: str = Form(""),
        media_ids_csv: str = Form(""),
        delay_min: int = Form(8),
        delay_max: int = Form(20),
        delay_between_min: int = Form(1),
        delay_between_max: int = Form(5),
        extra_action: str = Form(""),
    ):
        from db.models import FunnelStep
        import json as _json
        script_ids = [int(x) for x in script_ids_csv.split(",") if x.strip().isdigit()]
        media_ids = [int(x) for x in media_ids_csv.split(",") if x.strip().isdigit()]
        with SessionLocal() as s:
            max_order = (
                s.query(_func.coalesce(_func.max(FunnelStep.order_index), 0))
                .filter(FunnelStep.funnel_id == funnel_id).scalar() or 0
            ) if False else 0
            # Simpler: count existentes
            count = s.query(FunnelStep).filter(FunnelStep.funnel_id == funnel_id).count()
            step = FunnelStep(
                funnel_id=funnel_id,
                source_state=source_state.strip(),
                trigger_intent=trigger_intent.strip(),
                target_state=target_state.strip(),
                script_ids_json=_json.dumps(script_ids),
                media_ids_json=_json.dumps(media_ids),
                delay_min=delay_min, delay_max=delay_max,
                delay_between_min=delay_between_min,
                delay_between_max=delay_between_max,
                extra_action=(extra_action or "").strip() or None,
                order_index=count,
            )
            s.add(step)
            s.commit()
        return RedirectResponse(f"/funnel/{funnel_id}", status_code=303)

    @app.post("/funnel/step/{step_id}/delete")
    async def funnel_step_delete(step_id: int):
        from db.models import FunnelStep
        with SessionLocal() as s:
            step = s.query(FunnelStep).get(step_id)
            if step:
                fid = step.funnel_id
                s.delete(step)
                s.commit()
                return RedirectResponse(f"/funnel/{fid}", status_code=303)
        return RedirectResponse("/automation", status_code=303)

    @app.post("/funnel/{funnel_id}/test-mode-toggle")
    async def funnel_test_mode_toggle(funnel_id: int):
        """Liga/desliga 'Modo teste real': funil ativo + dry_run OFF +
        config.test_mode_username setado pro TEST_USERNAME.

        Quando ligado: bot processa DMs em tempo real APENAS pro @braozin.
        Quando desligado: comportamento normal (todos os leads OU desativado).
        """
        from db.models import Funnel
        import json as _json

        test_user = os.getenv("TEST_USERNAME", "").strip().lstrip("@")
        if not test_user:
            return JSONResponse({
                "error": "TEST_USERNAME não configurado no .env",
            }, status_code=400)

        with SessionLocal() as s:
            funnel = s.query(Funnel).get(funnel_id)
            if not funnel:
                return JSONResponse({"error": "Funnel não encontrado"}, status_code=404)

            try:
                config = _json.loads(funnel.config_json or "{}")
            except Exception:
                config = {}
            currently_test_mode = (config.get("test_mode_username") or "").lower() == test_user.lower()

            if currently_test_mode:
                # Desliga: tira config + desativa
                config.pop("test_mode_username", None)
                funnel.config_json = _json.dumps(config) if config else None
                funnel.is_active = False
                funnel.is_dry_run = True
                action = "off"
            else:
                # Liga: seta config + ativa + sai do dry_run
                # Desativa outros funis primeiro (só 1 ativo por vez)
                s.query(Funnel).filter(Funnel.id != funnel_id).update(
                    {Funnel.is_active: False}, synchronize_session=False,
                )
                config["test_mode_username"] = test_user
                funnel.config_json = _json.dumps(config)
                funnel.is_active = True
                funnel.is_dry_run = False
                action = "on"

            s.commit()

        return JSONResponse({
            "ok": True,
            "action": action,
            "test_username": test_user,
            "message": (
                f"✓ Modo teste real LIGADO. Funil ativo, processa DMs em tempo real APENAS pro @{test_user}. Outros leads ignorados."
                if action == "on"
                else "✓ Modo teste real DESLIGADO. Funil voltou pra DRY RUN inativo."
            ),
        })

    @app.post("/funnel/reset-test-lead")
    async def funnel_reset_test_lead():
        """Reseta o Lead do TEST_USERNAME pra estado 'new' pra poder testar
        o fluxo do zero de novo. Não apaga o lead, só zera o liga_state.
        """
        test_username = os.getenv("TEST_USERNAME", "").strip().lstrip("@")
        if not test_username:
            return JSONResponse({
                "error": "TEST_USERNAME não configurado no .env",
            }, status_code=400)

        with SessionLocal() as s:
            lead = (
                s.query(Lead)
                .filter(Lead.username.ilike(test_username))
                .first()
            )
            if not lead:
                return JSONResponse({
                    "error": f"Lead @{test_username} ainda não existe no banco. "
                             f"Manda 1 DM pelo @{test_username} primeiro pra catalogar, depois reseta.",
                }, status_code=404)

            old_state = lead.liga_state
            lead.liga_state = "new"
            # Limpa quotex_id se houver pra forçar revalidação
            if hasattr(lead, "quotex_id"):
                lead.quotex_id = None
            s.commit()

        return JSONResponse({
            "ok": True,
            "lead_id": lead.id,
            "test_username": test_username,
            "old_state": old_state,
            "new_state": "new",
            "message": f"✓ Lead @{test_username} resetado: {old_state} → new. Manda DM 'quiero entrar al VIP' de novo pra começar o fluxo do zero.",
        })

    @app.post("/funnel/{funnel_id}/test")
    async def funnel_test_run(funnel_id: int, message: str = Form("quiero entrar al VIP")):
        """Testa um funil enviando a primeira etapa pro TEST_USERNAME (@braozin).

        - Pega TEST_USERNAME do .env
        - Cria/atualiza Lead pra esse user com state='new'
        - Roda dispatch com force_send=True (ignora dry_run) e force_funnel_id
        - Mensagem default: 'quiero entrar al VIP' (dispara intent quer_entrar_vip)
        """
        from db.models import Funnel
        test_username = os.getenv("TEST_USERNAME", "").strip().lstrip("@")
        if not test_username:
            return JSONResponse({
                "error": "TEST_USERNAME não configurado no .env. Adicione TEST_USERNAME=seu_username e reinicie.",
            }, status_code=400)

        with SessionLocal() as s:
            funnel = s.query(Funnel).get(funnel_id)
            if not funnel:
                return JSONResponse({"error": "Funnel não encontrado"}, status_code=404)

        try:
            from userbot.client import get_client as _gc
            client = await _gc()
            entity = await client.get_entity(f"@{test_username}")
        except Exception as e:
            return JSONResponse({
                "error": f"Não consegui resolver @{test_username}: {e}",
            }, status_code=500)

        # Cria/atualiza Lead
        with SessionLocal() as s:
            lead = s.query(Lead).filter_by(telegram_id=entity.id).one_or_none()
            if not lead:
                lead = Lead(
                    telegram_id=entity.id,
                    username=test_username,
                    first_name=getattr(entity, "first_name", None) or test_username,
                    last_name=getattr(entity, "last_name", None) or "",
                    liga_state="new",
                    source="test_funnel",
                    last_dm_at=datetime.utcnow(),
                )
                s.add(lead)
            else:
                # Reset estado pra new (re-dispara o teste)
                lead.liga_state = "new"
                lead.last_dm_at = datetime.utcnow()
            s.commit()
            s.refresh(lead)
            lead_id = lead.id

        # Roda dispatch com force_funnel_id (testa esse funil mesmo se inativo)
        # E force_send (ignora dry_run pra teste real)
        try:
            from liga.funnel.dispatcher import dispatch as _funnel_dispatch
            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                result = await _funnel_dispatch(
                    client, lead, message,
                    is_image=False,
                    force_funnel_id=funnel_id,
                    force_send=True,
                )
            return JSONResponse({
                "ok": True,
                "test_username": test_username,
                "funnel_id": funnel_id,
                "simulated_message": message,
                "result": result,
                "message": f"✓ Teste enviado pro @{test_username}. Verifica o Telegram.",
            })
        except Exception as e:
            logger.exception("[funnel test] erro")
            return JSONResponse({"error": f"erro no dispatch: {e}"}, status_code=500)

    @app.post("/funnel/step/{step_id}/upload-media")
    async def funnel_step_upload_media(
        step_id: int,
        file: UploadFile = File(...),
        is_video_note: str = Form("1"),
        caption: str = Form(""),
    ):
        """Upload de mídia (.mp4 bolinha, foto, etc) direto pra etapa.

        Cria ScriptMedia no script container do funil, com video_note se for bolinha,
        e adiciona o ID ao media_ids_json do step.
        """
        from db.models import FunnelStep, ScriptMedia, Script
        from pathlib import Path as _P
        import uuid as _uuid
        import json as _json

        with SessionLocal() as s:
            step = s.query(FunnelStep).get(step_id)
            if not step:
                return JSONResponse({"error": "Etapa não encontrada"}, status_code=404)
            funnel_id = step.funnel_id
            funnel = step.funnel

            # Pega Script container desse funil — usa o "Funil VIP - Aquisição (auto)"
            # Se não existir, cria um genérico
            script_name = "Funil VIP - Aquisição (auto)" if "VIP" in (funnel.name or "") else f"Funil {funnel_id} - mídia"
            script = s.query(Script).filter(Script.name == script_name).first()
            if not script:
                script = Script(
                    name=script_name,
                    mode="ai",
                    briefing_pt=f"Container de mídia do funnel {funnel_id} ({funnel.name}).",
                    is_active=True,
                )
                s.add(script)
                s.commit()
                s.refresh(script)
            script_id_local = script.id

        # Salva arquivo no disco
        ROOT = Path(__file__).resolve().parent.parent
        media_dir = ROOT / "media"
        media_dir.mkdir(exist_ok=True)

        ext = _P(file.filename or "media.mp4").suffix.lower() or ".mp4"
        unique_name = f"{_uuid.uuid4().hex}{ext}"
        target_path = media_dir / unique_name

        try:
            content = await file.read()
            target_path.write_bytes(content)
            size_bytes = len(content)
        except Exception as e:
            return JSONResponse({"error": f"erro salvando arquivo: {e}"}, status_code=500)

        # Detecta tipo
        mime = (file.content_type or "").lower()
        if mime.startswith("video/"):
            kind = "video"
        elif mime.startswith("image/"):
            kind = "image"
        elif mime.startswith("audio/"):
            kind = "audio"
        else:
            kind = "document"

        video_note_flag = (is_video_note == "1") and kind == "video"

        # Cria ScriptMedia
        with SessionLocal() as s:
            media = ScriptMedia(
                script_id=script_id_local,
                filename=unique_name,
                original_name=file.filename or unique_name,
                mime_type=mime or "application/octet-stream",
                kind=kind,
                size_bytes=size_bytes,
                caption=caption.strip()[:500] or None,
                video_note=video_note_flag,
                send_before_text=False,
                order_index=0,
            )
            s.add(media)
            s.commit()
            s.refresh(media)
            media_id = media.id

            # Adiciona ao media_ids_json do step
            step = s.query(FunnelStep).get(step_id)
            current_media = []
            try:
                current_media = _json.loads(step.media_ids_json or "[]") or []
            except Exception:
                pass
            if media_id not in current_media:
                current_media.append(media_id)
            step.media_ids_json = _json.dumps(current_media)
            s.commit()

        logger.info(
            "[funnel upload] step=%d media=%d (%s, %d bytes, video_note=%s)",
            step_id, media_id, unique_name, size_bytes, video_note_flag,
        )
        return JSONResponse({
            "ok": True,
            "media_id": media_id,
            "filename": unique_name,
            "size_bytes": size_bytes,
            "kind": kind,
            "video_note": video_note_flag,
        })

    @app.post("/funnel/step/{step_id}/remove-media/{media_id}")
    async def funnel_step_remove_media(step_id: int, media_id: int):
        """Remove uma mídia do media_ids_json da etapa (não apaga o arquivo)."""
        from db.models import FunnelStep
        import json as _json
        with SessionLocal() as s:
            step = s.query(FunnelStep).get(step_id)
            if not step:
                return JSONResponse({"error": "Etapa não encontrada"}, status_code=404)
            current = []
            try:
                current = _json.loads(step.media_ids_json or "[]") or []
            except Exception:
                pass
            current = [m for m in current if m != media_id]
            step.media_ids_json = _json.dumps(current)
            s.commit()
        return JSONResponse({"ok": True})

    @app.post("/funnel/step/{step_id}/media-position")
    async def funnel_step_media_position(step_id: int, position: str = Form(...)):
        """Atualiza media_position (before/after/replace) de uma etapa."""
        from db.models import FunnelStep
        if position not in ("before", "after", "replace"):
            return JSONResponse({"error": "posição inválida"}, status_code=400)
        with SessionLocal() as s:
            step = s.query(FunnelStep).get(step_id)
            if not step:
                return JSONResponse({"error": "Etapa não encontrada"}, status_code=404)
            step.media_position = position
            s.commit()
        return JSONResponse({"ok": True, "position": position})

    @app.post("/funnel/variant/{variant_id}/edit-inline")
    async def funnel_variant_edit_inline(variant_id: int, text_es: str = Form(...)):
        """Edita o texto de uma ScriptVariant direto da página do funil.

        Retorna JSON pra UI atualizar sem reload.
        """
        if not text_es or not text_es.strip():
            return JSONResponse({"error": "texto vazio"}, status_code=400)
        with SessionLocal() as s:
            v = s.query(ScriptVariant).get(variant_id)
            if not v:
                return JSONResponse({"error": "variante não encontrada"}, status_code=404)
            v.text_es = text_es.strip()[:10000]
            s.commit()
            return JSONResponse({
                "ok": True, "variant_id": v.id,
                "text_preview": (v.text_es or "")[:120],
                "full_text": v.text_es,
            })

    # ----------------------------- Pause global de emergência ---------------
    @app.post("/automation/pause-all")
    async def automation_pause_all():
        """🛑 PARADA DE EMERGÊNCIA: desliga TODOS os funis ativos + agente.

        - Funis: is_active=False em todos
        - Agente Modo 3 (sugestões ativas): off
        - Agente Modo 2 (passivo/aprendiz): MANTÉM (não envia nada, só estuda)
        - Campanhas tradicionais: NÃO afeta (use 'PARAR TUDO' em /campaigns)
        """
        from db.models import Funnel
        from liga.agent.suggester import is_agent_active, set_agent_active

        funnels_was_active = 0
        agent_was_active = is_agent_active()

        with SessionLocal() as s:
            funnels_was_active = s.query(Funnel).filter(Funnel.is_active.is_(True)).count()
            s.query(Funnel).filter(Funnel.is_active.is_(True)).update(
                {Funnel.is_active: False}, synchronize_session=False,
            )
            s.commit()

        if agent_was_active:
            set_agent_active(False)

        logger.warning(
            "[automation] 🛑 PAUSE-ALL acionado: %d funil(eis) desativado(s), agente=%s",
            funnels_was_active, "off" if agent_was_active else "já off",
        )
        return JSONResponse({
            "ok": True,
            "funnels_deactivated": funnels_was_active,
            "agent_was_active": agent_was_active,
            "agent_now": False,
            "message": f"🛑 Pausado: {funnels_was_active} funil(eis){' + agente ativo' if agent_was_active else ''}. Modo 2 (aprendiz) continua estudando — não envia nada.",
        })

    # ----- Agente -----
    @app.post("/agent/active-toggle")
    async def agent_active_toggle():
        from liga.agent.suggester import is_agent_active, set_agent_active
        set_agent_active(not is_agent_active())
        return RedirectResponse("/automation", status_code=303)

    @app.post("/agent/bootstrap")
    async def agent_bootstrap_run(days_back: int = Form(90), classify_each: int = Form(1)):
        """Roda bootstrap em background (pode demorar minutos)."""
        from liga.agent.bootstrap import bootstrap_voice_profile
        import asyncio as _aio
        async def _run():
            try:
                bootstrap_voice_profile(
                    days_back=days_back,
                    classify_each=bool(classify_each),
                )
            except Exception:
                logger.exception("[bootstrap] falhou")
        _aio.create_task(_aio.to_thread(
            bootstrap_voice_profile,
            days_back=days_back,
            classify_each=bool(classify_each),
        ))
        return JSONResponse({"ok": True, "message": "Bootstrap rodando em background. Demora ~30 min."})

    @app.post("/agent/consolidate-vault")
    async def agent_consolidate_vault():
        from liga.agent.learning import consolidate_vault
        res = consolidate_vault()
        return JSONResponse(res)

    @app.get("/agent/queue", response_class=HTMLResponse)
    async def agent_queue(request: Request):
        from db.models import AgentSuggestion
        with SessionLocal() as s:
            pending = (
                s.query(AgentSuggestion)
                .filter(AgentSuggestion.status == "pending")
                .order_by(AgentSuggestion.created_at.desc())
                .limit(50).all()
            )
            recent = (
                s.query(AgentSuggestion)
                .filter(AgentSuggestion.status != "pending")
                .order_by(AgentSuggestion.created_at.desc())
                .limit(30).all()
            )
            pending_data = [{
                "id": p.id, "lead_id": p.lead_id,
                "lead_name": p.lead.display_name if p.lead else "?",
                "lead_country": p.lead.liga_id_country if p.lead else None,
                "dm_text": p.dm_text, "category": p.category,
                "confidence": p.confidence,
                "suggested_response": p.suggested_response,
                "created_at": p.created_at,
            } for p in pending]
            recent_data = [{
                "id": p.id, "lead_id": p.lead_id,
                "lead_name": p.lead.display_name if p.lead else "?",
                "category": p.category, "status": p.status,
                "suggested_response": (p.suggested_response or "")[:100],
                "final_response": (p.final_response or "")[:100],
                "decided_at": p.decided_at,
            } for p in recent]
        return templates.TemplateResponse(
            "agent_queue.html",
            {"request": request, "pending": pending_data, "recent": recent_data},
        )

    @app.post("/agent/suggestion/{sug_id}/decide")
    async def agent_suggestion_decide(
        sug_id: int,
        action: str = Form(...),  # approve | edit | reject
        final_response: str = Form(""),
    ):
        from db.models import AgentSuggestion
        from datetime import datetime as _dt
        with SessionLocal() as s:
            sug = s.query(AgentSuggestion).get(sug_id)
            if not sug:
                return JSONResponse({"error": "não encontrada"}, status_code=404)
            sug.decided_at = _dt.utcnow()

            if action == "approve":
                sug.status = "approved"
                sug.final_response = sug.suggested_response
            elif action == "edit":
                if not final_response.strip():
                    return JSONResponse({"error": "edição vazia"}, status_code=400)
                sug.status = "edited"
                sug.final_response = final_response.strip()[:5000]
            elif action == "reject":
                sug.status = "rejected"
            else:
                return JSONResponse({"error": "ação inválida"}, status_code=400)

            lead_telegram_id = sug.lead.telegram_id if sug.lead else None
            text_to_send = sug.final_response if sug.status in ("approved", "edited") else None
            sug_id_local = sug.id
            s.commit()

        # Envia se aprovou/editou
        if text_to_send and lead_telegram_id:
            try:
                from userbot.client import get_client as _gc
                client = await _gc()
                await client.send_message(lead_telegram_id, text_to_send)
                with SessionLocal() as s:
                    sug2 = s.query(AgentSuggestion).get(sug_id_local)
                    if sug2:
                        sug2.status = "sent"
                        sug2.sent_at = _dt.utcnow()
                        s.commit()
                # Aproveita pra registrar no learning
                try:
                    from liga.agent.learning import register_dm_pair
                    register_dm_pair(sug.lead_id if sug else 0, text_to_send)
                except Exception:
                    pass
            except Exception as e:
                logger.exception("[agent] erro enviando sugestão %s", sug_id)
                with SessionLocal() as s:
                    sug2 = s.query(AgentSuggestion).get(sug_id_local)
                    if sug2:
                        sug2.status = "error"
                        s.commit()
                return JSONResponse({"error": f"erro enviando: {e}"}, status_code=500)

        return JSONResponse({"ok": True, "status": "sent" if text_to_send else "rejected"})

    # ----------------------------- Disparos especiais pra VIPs --------------
    @app.get("/vip-outreach", response_class=HTMLResponse)
    async def vip_outreach(request: Request):
        """Lista VIPs que precisam de atenção manual.

        Categorias:
        - 🚨 At risk: liga_state = at_risk OU sem print há > 5 dias estando active
        - 💤 Sumido: VIP sem DM há > 7 dias
        - 💎 Ativo: VIP enviando prints normalmente (referência)
        """
        from datetime import timedelta as _td
        from sqlalchemy import func as _func, and_, or_

        now = datetime.utcnow()
        cutoff_5d = now - _td(days=5)
        cutoff_7d = now - _td(days=7)

        with SessionLocal() as s:
            base = s.query(Lead).filter(
                Lead.is_vip_potential.is_(True),
                Lead.opted_out.is_(False),
            )

            # 🚨 At risk
            at_risk = (
                base.filter(
                    or_(
                        Lead.liga_state == "at_risk",
                        and_(
                            Lead.liga_state == "active",
                            Lead.last_dm_at < cutoff_5d,
                        ),
                    )
                )
                .order_by(Lead.last_dm_at.asc())
                .limit(50)
                .all()
            )

            # 💤 Sumido (sem DM há > 7 dias, qualquer estado liga)
            sumido = (
                base.filter(
                    Lead.last_dm_at < cutoff_7d,
                    Lead.liga_state.notin_(["at_risk", "eliminated"]),
                )
                .order_by(Lead.last_dm_at.asc())
                .limit(30)
                .all()
            )

            # 💎 Ativo (referência — VIP com DM recente)
            ativos_count = (
                base.filter(Lead.last_dm_at >= cutoff_5d)
                .filter(Lead.liga_state == "active")
                .count()
            )

            # Lista IDs já em outras categorias pra não duplicar
            at_risk_ids = {l.id for l in at_risk}
            sumido = [l for l in sumido if l.id not in at_risk_ids]

            def _enrich(lead):
                # Busca último proof e dias desde DM
                last_proof = (
                    s.query(OperationProof)
                    .filter(OperationProof.lead_id == lead.id)
                    .filter(OperationProof.validated.is_(True))
                    .order_by(OperationProof.created_at.desc())
                    .first()
                )
                days_since_dm = None
                if lead.last_dm_at:
                    days_since_dm = (now - lead.last_dm_at).days
                days_since_proof = None
                if last_proof:
                    days_since_proof = (now - last_proof.created_at).days
                return {
                    "id": lead.id,
                    "display_name": lead.display_name,
                    "liga_state": lead.liga_state,
                    "country": lead.liga_id_country,
                    "balance": lead.liga_id_balance,
                    "deposits_sum": lead.liga_id_deposits_sum,
                    "last_dm_at": lead.last_dm_at,
                    "days_since_dm": days_since_dm,
                    "days_since_proof": days_since_proof,
                    "engagement_tag": lead.engagement_tag,
                }

            at_risk_data = [_enrich(l) for l in at_risk]
            sumido_data = [_enrich(l) for l in sumido]

        return templates.TemplateResponse(
            "vip_outreach.html",
            {
                "request": request,
                "at_risk": at_risk_data,
                "sumido": sumido_data,
                "ativos_count": ativos_count,
            },
        )

    @app.post("/vip-outreach/{lead_id}/send")
    async def vip_outreach_send(lead_id: int, message: str = Form(...), template: str = Form("custom")):
        """Envia DM personalizada pra um VIP. Sempre manual, sempre 1 lead."""
        from userbot.client import get_client as _gc
        from datetime import datetime as _dt

        msg = (message or "").strip()
        if not msg:
            return JSONResponse({"error": "mensagem vazia"}, status_code=400)
        if len(msg) > 2000:
            return JSONResponse({"error": "mensagem muito longa (max 2000 chars)"}, status_code=400)

        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                return JSONResponse({"error": "lead não encontrado"}, status_code=404)
            telegram_id = lead.telegram_id
            display_name = lead.display_name

        try:
            client = await _gc()
            await client.send_message(telegram_id, msg)
        except Exception as e:
            logger.exception("[vip_outreach] erro enviando pra %s", display_name)
            return JSONResponse({"error": f"falha no envio: {e}"}, status_code=500)

        # Registra como LeadMessage out + nota
        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if lead:
                lead.last_bot_action = f"vip_outreach:{template}:{_dt.utcnow().strftime('%Y-%m-%d %H:%M')}"
                s.add(LeadMessage(
                    lead_id=lead.id,
                    direction="out",
                    kind="text",
                    content=msg[:5000],
                    classified_as=f"vip_outreach_{template}",
                ))
                s.commit()

        logger.info("[vip_outreach] DM enviada pra %s (template=%s, %d chars)",
                    display_name, template, len(msg))
        return JSONResponse({"ok": True, "sent_to": display_name})

    # ----------------------------- Deep scan de ID em 1 conversa -------------
    @app.post("/liga/id-review/{lead_id}/scan")
    async def liga_id_review_scan(lead_id: int):
        """Deep scan na conversa de 1 lead: lê últimas 50 DMs (texto + Vision em até 2 prints)
        procurando ID. Se achar válido, valida automaticamente no @QuotexPartnerBot.

        Retorna JSON com status atualizado.
        """
        from datetime import datetime as _dt
        from userbot.client import get_client as _gc
        from userbot.leads import (
            find_recent_account_id_in_dms,
            validate_id_via_partner_bot,
            _looks_like_valid_id,
        )
        from liga.automation import _maybe_flag_vip

        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                return JSONResponse({"error": "Lead não encontrado"}, status_code=404)
            telegram_id = lead.telegram_id

        try:
            client = await _gc()
            cand = await find_recent_account_id_in_dms(
                client, telegram_id,
                max_messages=50,        # mais profundo que scan default (30)
                scan_images=True,        # com Vision
                max_images=2,            # cap pra não estourar custo
                return_all=True,         # cascata: pega texto + imagem, tenta cada um
            )
        except Exception as e:
            logger.exception("[id_scan] erro lead %d", lead_id)
            return JSONResponse({"error": f"erro: {e}"}, status_code=500)

        candidates_list = cand.get("candidates") or []

        # Caso 1: nada encontrado
        if not candidates_list:
            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                if lead:
                    lead.last_revalidated_at = _dt.utcnow()
                    if lead.liga_id_status not in ("validated", "invalid"):
                        lead.liga_id_status = "needs_review"
                    lead.liga_id_partner_response = (
                        f"[deep_scan {_dt.utcnow().strftime('%Y-%m-%d %H:%M')}] "
                        "nenhum ID encontrado em 50 msgs (texto + 2 imagens)"
                    )
                    s.commit()
            return JSONResponse({
                "ok": True,
                "found": False,
                "message": "Nenhum ID encontrado nas últimas 50 mensagens",
            })

        # Caso 2: tenta cada candidato até achar 1 validated
        import asyncio as _asyncio
        last_val = None
        last_cand_id = None
        last_source = None
        attempts_log = []

        for c in candidates_list:
            test_id = c["id"]
            test_source = c.get("source", "?")
            try:
                val = await validate_id_via_partner_bot(client, test_id)
            except Exception as e:
                logger.exception("[id_scan] erro validando %s", test_id)
                continue
            last_val = val
            last_cand_id = test_id
            last_source = test_source
            attempts_log.append(f"{test_id}({test_source}):{val.get('status')}")

            if val.get("status") == "validated":
                break
            # delay curto entre múltiplas tentativas
            if len(candidates_list) > 1:
                await _asyncio.sleep(2.0)

        if last_val is None:
            return JSONResponse({"error": "scan OK mas validação falhou em todos candidatos"}, status_code=500)

        with SessionLocal() as s:
            lead = s.query(Lead).get(lead_id)
            if not lead:
                return JSONResponse({"error": "Lead sumiu"}, status_code=404)

            lead.liga_account_id = last_cand_id
            lead.last_revalidated_at = _dt.utcnow()
            lead.liga_id_partner_response = (last_val.get("raw") or "")[:4000]

            if last_val.get("status") == "validated":
                lead.liga_id_status = "validated"
                lead.liga_id_country = (last_val.get("country") or "")[:50]
                lead.liga_id_balance = last_val.get("balance")
                lead.liga_id_deposits_sum = last_val.get("deposits_sum")
                lead.liga_id_turnover = last_val.get("turnover")
                lead.liga_id_validated_at = _dt.utcnow()
                _maybe_flag_vip(lead)
                logger.info(
                    "[id_scan] ✓ lead=%s id=%s país=%s saldo=$%.2f (testou %d candidato(s): %s)",
                    lead.display_name, last_cand_id, last_val.get("country"),
                    last_val.get("balance") or 0, len(candidates_list), ", ".join(attempts_log),
                )
            elif last_val.get("status") == "invalid":
                lead.liga_id_status = "invalid"
            else:
                lead.liga_id_status = "extracted"

            country = lead.liga_id_country
            balance = lead.liga_id_balance
            deposits = lead.liga_id_deposits_sum
            id_status = lead.liga_id_status
            s.commit()

        return JSONResponse({
            "ok": True,
            "found": True,
            "id": last_cand_id,
            "source": last_source,
            "id_status": id_status,
            "country": country,
            "balance": balance,
            "deposits_sum": deposits,
            "candidates_tested": len(candidates_list),
            "attempts": attempts_log,
        })

    # ----------------------------- Validação de IDs pendentes ----------------
    @app.get("/automation/validate-pending-ids/status")
    async def validate_pending_status():
        """Retorna status atual da validação de IDs pendentes (pra polling da UI)."""
        from liga.automation import get_pending_validation_status
        return JSONResponse(get_pending_validation_status())

    @app.post("/automation/validate-pending-ids/stop")
    async def validate_pending_stop():
        """Solicita parada da validação em andamento. Próximo lead vira o último."""
        from liga.automation import stop_pending_validation, is_pending_validation_running
        if not is_pending_validation_running():
            return JSONResponse({"ok": False, "error": "não está rodando"}, status_code=400)
        stop_pending_validation()
        return JSONResponse({"ok": True, "stopping": True})

    # --------------------- Scan all pending DMs (deep scan batch) -----------
    @app.get("/automation/scan-all/status")
    async def scan_all_status():
        from liga.automation import get_scan_all_status
        return JSONResponse(get_scan_all_status())

    @app.post("/automation/scan-all/stop")
    async def scan_all_stop():
        from liga.automation import stop_scan_all, is_scan_all_running
        if not is_scan_all_running():
            return JSONResponse({"ok": False, "error": "não está rodando"}, status_code=400)
        stop_scan_all()
        return JSONResponse({"ok": True, "stopping": True})

    @app.post("/automation/run/{job}")
    async def run_automation(job: str):
        from userbot.client import get_client as _gc
        from liga import automation as _auto
        try:
            # Jobs que NÃO precisam do Telegram client (rodam só no DB / disco)
            if job == "obsidian_sync":
                from liga.obsidian_export import export_all_leads, export_daily_insight
                r1 = export_all_leads(only_active=False)
                r2 = export_daily_insight()
                return JSONResponse({
                    "ok": True,
                    "result": {"leads": r1, "insight_path": str(r2) if r2 else None},
                })
            if job == "recalc_vips":
                res = await _auto.task_recalculate_vips()
                return JSONResponse({"ok": True, "result": res})
            if job == "contextual_analysis":
                from liga.contextual_analysis import task_weekly_contextual_analysis
                res = await task_weekly_contextual_analysis(max_leads=50)
                return JSONResponse({"ok": True, "result": res})

            # Jobs que precisam do Telegram client
            client = await _gc()
            if job == "backup":
                res = await _auto.task_daily_backup(client)
            elif job == "digest":
                res = await _auto.task_daily_digest(client)
            elif job == "revalidation":
                res = await _auto.task_weekly_revalidation(client)
            elif job == "follow_ups":
                res = await _auto.task_run_follow_ups(client)
            elif job == "incremental":
                res = await _auto.task_incremental_dm_scan(client)
            elif job == "group_members_check":
                res = await _auto.task_check_private_group_members(client)
            elif job == "validate_pending_ids":
                # Roda em background e retorna imediatamente — UI faz polling
                import asyncio as _asyncio
                _asyncio.create_task(_auto.task_validate_pending_ids(client, force_full=False))
                return JSONResponse({"ok": True, "started": True, "background": True})
            elif job == "validate_pending_ids_full":
                # Validação completa — ignora backoff de 24h
                import asyncio as _asyncio
                _asyncio.create_task(_auto.task_validate_pending_ids(client, force_full=True))
                return JSONResponse({"ok": True, "started": True, "background": True, "force_full": True})
            elif job == "scan_all_pending_dms":
                import asyncio as _asyncio
                _asyncio.create_task(_auto.task_scan_all_pending_dms(client))
                return JSONResponse({"ok": True, "started": True, "background": True})
            elif job == "rescue_stuck_sends":
                res = await _auto.task_rescue_stuck_sends()
                return JSONResponse({"ok": True, "result": res})
            elif job == "tournament_backup":
                res = await _auto.task_tournament_backup(client)
            else:
                return JSONResponse({"error": f"job desconhecido: {job}"}, status_code=400)
            return JSONResponse({"ok": True, "result": res})
        except Exception as e:
            logger.exception("[automation] erro rodando %s", job)
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.post("/leads/recategorize")
    async def leads_recategorize(scan_messages: str = Form("1")):
        """Reclassifica todos os leads em background. Pode demorar muito."""
        from userbot.categorizer import recategorize_all_leads, CategorizationProgress
        if CategorizationProgress.running:
            return JSONResponse({"error": "Já está rodando"}, status_code=409)
        scan = scan_messages == "1"
        # Roda em background — não bloqueia a request
        asyncio.create_task(recategorize_all_leads(scan_messages=scan))
        return JSONResponse({"ok": True, "started": True, "scan_messages": scan})

    @app.get("/leads/recategorize/status")
    async def leads_recategorize_status():
        from userbot.categorizer import CategorizationProgress
        return JSONResponse(CategorizationProgress.snapshot())

    @app.post("/liga/recalc-scores")
    async def liga_recalc_scores():
        from liga.scoring import calc_lead_score
        updated = 0
        with SessionLocal() as s:
            leads = s.query(Lead).filter(Lead.liga_state != "new").all()
            for l in leads:
                l.lead_score = calc_lead_score(l, s)
                updated += 1
            s.commit()
        return JSONResponse({"ok": True, "updated": updated})

    return app