"""Entrypoint - inicia tudo:
   - Loga no userbot Telethon (pede código na 1ª vez)
   - Inicia listener de respostas (tracker)
   - Inicia o agendador (APScheduler)
   - Sobe o painel web (Uvicorn)
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

from db import init_db  # noqa: E402
from userbot import start_client, stop_client, scheduler, start_reply_listener  # noqa: E402
from web import create_app  # noqa: E402


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("main")


async def main() -> None:
    init_db()
    logger.info("Banco inicializado.")

    # Detecta ffmpeg pra vídeo bolinha
    try:
        from userbot.sender import check_ffmpeg_available
        ok, info = check_ffmpeg_available()
        if ok:
            logger.info("[ffmpeg] OK — %s", info)
        else:
            logger.warning(
                "[ffmpeg] NÃO disponível — vídeo bolinha pode sair grande/feio. "
                "Solução: rode instalar_ffmpeg.bat na pasta do projeto."
            )
    except Exception:
        pass

    try:
        await start_client()
    except Exception as e:
        logger.error("Falha ao logar no Telegram: %s", e)
        logger.error("Confirme TELEGRAM_API_ID, TELEGRAM_API_HASH e TELEGRAM_PHONE no .env")
        sys.exit(1)

    await start_reply_listener()

    # ═══ CATCH-UP DE STARTUP (100% em background) ══════════════════════════
    # Recupera os leads que chegaram enquanto o bot estava DESLIGADO.
    # Roda TUDO em background pra o painel + tempo real subirem NA HORA.
    #   1) Texto (grátis): cataloga leads novos + IDs em texto
    #   2) Imagens (Vision, cache-protegido): lê prints novos
    async def _bg_catchup():
        try:
            await asyncio.sleep(3)  # deixa o painel subir primeiro
            from userbot.leads import sync_leads_from_dm_history
            logger.info("[catch-up] texto (background)...")
            res = await sync_leads_from_dm_history(
                exclude_group_members=False, scan_images=False,
            )
            logger.info("[catch-up] texto OK: %s", res)
            if os.getenv("STARTUP_IMAGE_CATCHUP", "1").strip() == "1":
                logger.info("[catch-up] imagens/prints (background)...")
                res2 = await sync_leads_from_dm_history(
                    exclude_group_members=False, scan_images=True,
                )
                logger.info("[catch-up] imagens OK: %s", res2)
        except Exception:
            logger.exception("[catch-up] erro")
    asyncio.create_task(_bg_catchup())

    scheduler.start()
    logger.info("Agendador iniciado.")

    # Liga — agenda lembretes, checkpoints e ranking
    try:
        from liga.scheduler import start_liga_scheduler
        from userbot import get_client
        liga_client = await get_client()
        start_liga_scheduler(liga_client)
    except Exception:
        logger.exception("Falha ao iniciar scheduler da Liga")

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8080"))
    config = uvicorn.Config(
        create_app(),
        host=host,
        port=port,
        log_level="warning",
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    logger.info("Painel web em http://%s:%s — abra esse link no navegador!", host, port)

    try:
        await server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        logger.info("Finalizando...")
        scheduler.shutdown(wait=False)
        await stop_client()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nEncerrado.")
