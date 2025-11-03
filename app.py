import os
import io
import re
import random
import asyncio
import logging
import httpx
from datetime import datetime, timedelta
from PIL import Image
from fastapi import FastAPI, Request
from telegram import Update, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG ----------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")  # e.g. https://rizo-battle-bot.onrender.com
PORT = int(os.getenv("PORT", 10000))
BATTLE_COOLDOWN = int(os.getenv("BATTLE_COOLDOWN", 60))  # seconds

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("rizo-battle-bot")

# ---------------- TELEGRAM APP ----------------
application = Application.builder().token(BOT_TOKEN).build()

# ---------------- FASTAPI APP ----------------
fastapi_app = FastAPI(title="Rizo Battle Bot")

# ---------------- STATE ----------------
user_last_battle = {}  # user_id -> datetime of last battle

# ------------------------------------------------
#                 HELPERS
# ------------------------------------------------
def within_cooldown(user_id: int) -> bool:
    """Check if user is within cooldown window."""
    now = datetime.utcnow()
    last_time = user_last_battle.get(user_id)
    if not last_time:
        return False
    return (now - last_time).total_seconds() < BATTLE_COOLDOWN

async def download_file(url: str) -> io.BytesIO:
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        return io.BytesIO(resp.content)

async def extract_stats_from_card(image_bytes: io.BytesIO) -> dict:
    """Fake OCR/stat extraction - placeholder for real logic."""
    # In a real bot, this would run OCR or metadata parsing
    # For now, randomly generate stats for testing
    stats = {
        "power": random.randint(50, 150),
        "defense": random.randint(50, 150),
        "speed": random.randint(50, 150),
    }
    logger.info(f"Extracted stats: {stats}")
    return stats

def determine_winner(stats1: dict, stats2: dict) -> str:
    """Simple sum-based winner logic."""
    total1 = sum(stats1.values())
    total2 = sum(stats2.values())
    if total1 > total2:
        return "player1"
    elif total2 > total1:
        return "player2"
    else:
        return "draw"

async def generate_battle_image(user1, user2, stats1, stats2, winner):
    """Placeholder for a generated battle card result."""
    # Could generate OpenAI image here - for now return a mock image
    img = Image.new("RGB", (512, 256), (30, 30, 40))
    img_bytes = io.BytesIO()
    img.save(img_bytes, format="PNG")
    img_bytes.seek(0)
    return img_bytes

# ------------------------------------------------
#                 COMMANDS
# ------------------------------------------------
async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🥊🥊! The bot is alive.")

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "⚔️ *Rizo Battle Bot Commands*\n\n"
        "/battle - Upload two battle cards to start a match\n"
        "/ping - Check if bot is alive\n"
        "/help - Show this message\n"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔️ Send me *two card images* to start a battle!\n"
        "You can also reply to another user's card image with `/battle` to challenge them.",
        parse_mode="Markdown",
    )

# ------------------------------------------------
#                 IMAGE HANDLER
# ------------------------------------------------
@application.add_handler
class ImageBattleHandler(MessageHandler):
    def __init__(self):
        super().__init__(filters.PHOTO | filters.Document.IMAGE, self.process_image)

    async def process_image(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username or user_id

        if within_cooldown(user_id):
            remaining = BATTLE_COOLDOWN - int((datetime.utcnow() - user_last_battle[user_id]).total_seconds())
            await update.message.reply_text(f"⏳ Please wait {remaining}s before another battle.")
            return

        user_last_battle[user_id] = datetime.utcnow()

        # Download image
        try:
            if update.message.photo:
                file = await update.message.photo[-1].get_file()
            else:
                file = await update.message.document.get_file()

            img_bytes = await download_file(file.file_path)
            stats = await extract_stats_from_card(img_bytes)
        except Exception as e:
            logger.error(f"Failed to process image: {e}")
            await update.message.reply_text("❌ Failed to process your card. Try again.")
            return

        # Save card info temporarily
        context.user_data["last_card"] = {"user": username, "stats": stats, "img": img_bytes}

        # Check if opponent already uploaded
        if "waiting_card" not in context.chat_data:
            context.chat_data["waiting_card"] = context.user_data["last_card"]
            await update.message.reply_text(f"🃏 Card from @{username} locked in. Waiting for an opponent...")
            return

        # Run battle
        opponent = context.chat_data.pop("waiting_card")
        player1 = opponent["user"]
        player2 = username
        stats1 = opponent["stats"]
        stats2 = stats
        winner = determine_winner(stats1, stats2)

        logger.info(f"Battle between {player1} vs {player2} => Winner: {winner}")

        # Generate result image
        battle_img = await generate_battle_image(player1, player2, stats1, stats2, winner)

        caption = f"⚔️ *Battle Result*\n\n@{player1}: {stats1}\n@{player2}: {stats2}\n\n"
        if winner == "draw":
            caption += "🤝 It's a draw!"
        elif winner == "player1":
            caption += f"🏆 Winner: @{player1}"
        else:
            caption += f"🏆 Winner: @{player2}"

        await update.message.reply_photo(InputFile(battle_img, filename="battle.png"), caption=caption, parse_mode="Markdown")

# ------------------------------------------------
#                 FASTAPI ROUTES
# ------------------------------------------------
@fastapi_app.get("/")
async def root():
    return {"status": "ok", "message": "Rizo Battle Bot is running"}

@fastapi_app.get("/healthz")
async def healthz():
    return {"ok": True}

@fastapi_app.post("/webhook/{token}")
async def telegram_webhook(request: Request, token: str):
    if token != BOT_TOKEN:
        logger.warning("Invalid webhook token access attempt")
        return {"ok": False, "error": "invalid token"}

    try:
        update_data = await request.json()
        update = Update.de_json(update_data, application.bot)
        await application.update_queue.put(update)
        return {"ok": True}
    except Exception as e:
        logger.exception(f"Webhook error: {e}")
        return {"ok": False, "error": str(e)}

@fastapi_app.get("/webhook/{token}")
async def webhook_alive(token: str):
    return {"ok": True, "message": "Webhook alive", "token": token}

# ------------------------------------------------
#                 STARTUP / SHUTDOWN
# ------------------------------------------------
@fastapi_app.on_event("startup")
async def on_startup():
    await application.initialize()
    webhook_url = f"{WEBHOOK_URL}/webhook/{BOT_TOKEN}"
    try:
        await application.bot.set_webhook(url=webhook_url)
        logger.info(f"✅ Webhook set to {webhook_url}")
    except Exception as e:
        logger.error(f"❌ Webhook setup failed: {e}")

    asyncio.create_task(application.start())

@fastapi_app.on_event("shutdown")
async def on_shutdown():
    await application.stop()
    await application.shutdown()
    logger.info("🛑 Bot stopped cleanly.")

# ------------------------------------------------
#                 ENTRYPOINT
# ------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    logger.info("🚀 Starting Rizo Battle Bot (FastAPI + Telegram)")
    uvicorn.run("bot:fastapi_app", host="0.0.0.0", port=PORT)
