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

    # ═══ CATCH-UP DE STARTUP ═══════════════════════════════════════════════
    # Toda vez que o bot liga, recupera os leads que chegaram enquanto estava
    # DESLIGADO. Sem isso, DMs recebidas offline eram perdidas.
    #
    # 1) Texto (rápido, GRÁTIS): cataloga leads novos + IDs em texto. Bloqueia
    #    só alguns segundos antes do bot subir.
    # 2) Imagens (background, cache-protegido): roda Vision nos prints novos
    #    sem travar o bot. Imagens já vistas = cache = grátis.
    try:
        from userbot.leads import sync_leads_from_dm_history
        logger.info("[catch-up] sincronizando leads que chegaram offline (texto)...")
        res = await sync_leads_from_dm_history(
            exclude_group_members=False, scan_images=False,
        )
        logger.info("[catch-up] texto OK: %s", res)
    except Exception:
        logger.exception("[catch-up] erro na sincronização de texto")

    if os.getenv("STARTUP_IMAGE_CATCHUP", "1").strip() == "1":
        async def _bg_image_catchup():
            try:
                await asyncio.sleep(15)  # deixa o bot subir primeiro
                from userbot.leads import sync_leads_from_dm_history
                logger.info("[catch-up] scan de imagens (prints) em background...")
                res = await sync_leads_from_dm_history(
                    exclude_group_members=False, scan_images=True,
                )
                logger.info("[catch-up] imagens OK: %s", res)
            except Exception:
                logger.exception("[catch-up] erro no scan de imagens")
        asyncio.create_task(_bg_image_catchup())

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
