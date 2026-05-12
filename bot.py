"""
Telegram group message listener.
Receives all messages from the configured group and prints them to stdout.

Usage:
    pip install -r requirements.txt
    python bot.py
"""

import os
import asyncio
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

load_dotenv()

TG_API = os.getenv("TG_API")
TG_GR_ID = int(os.getenv("TG_GR_ID"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Debug handler — logs every incoming update regardless of chat."""
    logger.info("RAW UPDATE: %s", update.to_dict())
    print(f"[RAW] {update.to_dict()}", flush=True)


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if msg is None:
        return

    chat_id = update.effective_chat.id
    if chat_id != TG_GR_ID:
        return

    sender = None
    if msg.from_user:
        sender = f"@{msg.from_user.username}" if msg.from_user.username else msg.from_user.full_name
    elif msg.sender_chat:
        sender = msg.sender_chat.title

    text = msg.text or msg.caption or "<non-text message>"
    logger.info("[%s] %s: %s", chat_id, sender, text)
    print(f"[MSG] {sender}: {text}", flush=True)


def main() -> None:
    if not TG_API:
        raise RuntimeError("TG_API is not set in .env")

    async def run():
        app = ApplicationBuilder().token(TG_API).build()
        app.add_handler(MessageHandler(filters.ALL, on_any_update), group=-1)
        app.add_handler(
            MessageHandler(filters.ALL & filters.Chat(TG_GR_ID), on_message)
        )
        logger.info("Bot started. Listening to group %s ...", TG_GR_ID)
        async with app:
            await app.start()
            await app.updater.start_polling(allowed_updates=["message", "channel_post"])
            await asyncio.Event().wait()  # run forever until Ctrl+C

    asyncio.run(run())


if __name__ == "__main__":
    main()
