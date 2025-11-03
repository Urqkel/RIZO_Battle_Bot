import os
import io
import re
import uuid
import json
import sqlite3
import logging
import random
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from PIL import Image
import pytesseract

# ===================== CONFIG =====================
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN not set in environment.")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("❌ RENDER_EXTERNAL_URL not set in environment.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

# ===================== LOGGING =====================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-battle-bot")

# ===================== FASTAPI =====================
app = FastAPI(title="Rizo Battle Bot", version="1.0")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ===================== DATABASE =====================
DB_PATH = "battles.db"
os.makedirs("battles", exist_ok=True)
os.makedirs("cards", exist_ok=True)

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS battles (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            challenger_username TEXT,
            challenger_stats TEXT,
            opponent_username TEXT,
            opponent_stats TEXT,
            winner TEXT,
            html_path TEXT
        )
    """)
    conn.commit()
    conn.close()

init_db()

# ===================== RUNTIME =====================
pending_challenges: dict[int, str] = {}
uploaded_cards: dict[int, dict] = {}

# ===================== OCR =====================
def ocr_text_from_bytes(file_bytes: bytes) -> str:
    try:
        image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
        text = pytesseract.image_to_string(image)
        return text
    except Exception as e:
        log.error(f"OCR failed: {e}")
        return ""

def parse_stats_from_text(text: str) -> dict:
    lower = text.lower()

    hp = int(re.search(r"hp[:\s]*([0-9]{1,4})", lower).group(1)) if re.search(r"hp[:\s]*([0-9]{1,4})", lower) else 100
    defense = int(re.search(r"defen(?:se|c)e?[:\s]*([0-9]{1,4})", lower).group(1)) if re.search(r"defen(?:se|c)e?[:\s]*([0-9]{1,4})", lower) else 50
    serial = int(re.search(r"#\s*([0-9]{1,4})", lower).group(1)) if re.search(r"#\s*([0-9]{1,4})", lower) else 1000

    attack_patterns = re.findall(r"([a-z\s]+)\s*[:\-]?\s*([0-9]{1,4})", text, re.IGNORECASE)
    attacks = []
    for name, val in attack_patterns:
        name = name.strip().title()
        if any(k in name.lower() for k in ["attack", "strike", "blast", "move", "punch", "kick"]):
            attacks.append((name, int(val)))

    if not attacks:
        attacks = [("Basic Strike", 30), ("Heavy Blow", 40)]
    elif len(attacks) == 1:
        attacks.append(("Heavy Blow", attacks[0][1] + 10))
    else:
        attacks = attacks[:2]

    return {
        "hp": hp,
        "defense": defense,
        "serial": serial,
        "attack1_name": attacks[0][0],
        "attack1_power": attacks[0][1],
        "attack2_name": attacks[1][0],
        "attack2_power": attacks[1][1],
    }

# ===================== BATTLE =====================
def calculate_hp(card: dict) -> int:
    serial_bonus = (2000 - card["serial"]) // 50
    return max(1, card["hp"] + serial_bonus)

ELEMENTAL_MODIFIERS = {
    "fire": {"water": 0.5, "earth": 1.2},
    "water": {"fire": 1.5, "earth": 1.0},
    "earth": {"fire": 0.8, "water": 1.2},
}

def get_element(move_name: str) -> str:
    name = move_name.lower()
    if "fire" in name:
        return "fire"
    if "water" in name:
        return "water"
    if "earth" in name:
        return "earth"
    return "normal"

def simulate_battle(card1: dict, card2: dict):
    hp1, hp2 = calculate_hp(card1), calculate_hp(card2)
    def1, def2 = card1["defense"], card2["defense"]
    log_lines = []
    turn = 0

    while hp1 > 0 and hp2 > 0:
        turn += 1
        attacker, defender = (card1, card2) if turn % 2 else (card2, card1)
        move_name, move_power = random.choice([
            (attacker["attack1_name"], attacker["attack1_power"]),
            (attacker["attack2_name"], attacker["attack2_power"]),
        ])
        elem1, elem2 = get_element(move_name), get_element(defender["attack1_name"])
        modifier = ELEMENTAL_MODIFIERS.get(elem1, {}).get(elem2, 1.0)
        dmg = int(move_power * random.uniform(0.8, 1.2) * modifier) - int(defender["defense"] * 0.1)
        dmg = max(5, dmg)

        if turn % 2:
            hp2 -= dmg
        else:
            hp1 -= dmg

        log_lines.append(f"{attacker['username']} used {move_name}! ({dmg} dmg)")

    winner = card1["username"] if hp1 > hp2 else card2["username"]
    return {"winner": winner, "hp1_end": max(0, hp1), "hp2_end": max(0, hp2), "log": log_lines}

# ===================== SAVE BATTLE =====================
def save_battle_html(battle_id: str, context: dict):
    html_path = f"battles/{battle_id}.html"
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(f"""
        <html><body style="background:#0d0d0d;color:white;text-align:center;font-family:sans-serif;">
        <h1>Battle #{battle_id[:8]}</h1>
        <p>Winner: {context['winner_name']}</p>
        <p>{context['hp1_end']} vs {context['hp2_end']}</p>
        </body></html>
        """)
    return html_path

def persist_battle(battle_id, c_user, c_stats, o_user, o_stats, winner, html_path):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO battles VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (battle_id, datetime.utcnow().isoformat(), c_user, json.dumps(c_stats), o_user, json.dumps(o_stats), winner, html_path)
    )
    conn.commit()
    conn.close()

# ===================== TELEGRAM =====================
telegram_app: Optional[Application] = None

async def cmd_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔️ *Rizo Battle Bot*\n\n"
        "Use /challenge @username to challenge someone.\n"
        "Then both upload your Rizo battle cards.",
        parse_mode="Markdown"
    )

async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].startswith("@"):
        return await update.message.reply_text("Usage: /challenge @username")

    challenger = update.effective_user
    opponent_username = context.args[0].lstrip("@").lower()
    pending_challenges[challenger.id] = opponent_username
    await update.message.reply_text(
        f"⚔️ @{challenger.username} challenged @{opponent_username}! Upload your cards."
    )

async def handler_card_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or f"user{user.id}").lower()

    file = None
    if update.message.photo:
        file = await update.message.photo[-1].get_file()
    elif update.message.document:
        file = await update.message.document.get_file()
    else:
        return await update.message.reply_text("Please upload an image file.")

    data = await file.download_as_bytearray()
    ocr_text = ocr_text_from_bytes(data)
    stats = parse_stats_from_text(ocr_text)
    card = {"username": username, "user_id": user.id, **stats}
    uploaded_cards[user.id] = card

    await update.message.reply_text(f"✅ @{username} card received.")

    # try match
    opponent_id = None
    for challenger_id, opp_user in pending_challenges.items():
        if opp_user == username and challenger_id in uploaded_cards:
            opponent_id = challenger_id
            break
        if challenger_id == user.id:
            opp_id = next((uid for uid, c in uploaded_cards.items() if c["username"] == opp_user), None)
            if opp_id:
                opponent_id = opp_id
                break

    if opponent_id:
        card1 = uploaded_cards[opponent_id]
        card2 = uploaded_cards[user.id]
        result = simulate_battle(card1, card2)
        battle_id = str(uuid.uuid4())
        html_path = save_battle_html(battle_id, {
            "winner_name": result["winner"],
            "hp1_end": result["hp1_end"],
            "hp2_end": result["hp2_end"],
        })
        persist_battle(battle_id, card1["username"], card1, card2["username"], card2, result["winner"], html_path)

        replay_url = f"{RENDER_EXTERNAL_URL}/battle/{battle_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 View Replay", url=replay_url)]])
        await update.message.reply_text(
            f"🏆 Winner: @{result['winner']}\n"
            f"{card1['username']} HP: {result['hp1_end']} vs {card2['username']} HP: {result['hp2_end']}",
            reply_markup=keyboard
        )

# ===================== FASTAPI ROUTES =====================
@app.get("/")
async def root():
    return {"status": "ok", "bot": "rizo-battle-bot"}

@app.get("/battle/{battle_id}", response_class=HTMLResponse)
async def get_battle(battle_id: str):
    path = f"battles/{battle_id}.html"
    if os.path.exists(path):
        return FileResponse(path)
    return HTMLResponse("<h1>Battle not found.</h1>")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    update = Update.de_json(await req.json(), telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})

# ===================== STARTUP / SHUTDOWN =====================
@app.on_event("startup")
async def on_startup():
    global telegram_app
    log.info("🚀 Starting Telegram bot...")
    telegram_app = Application.builder().token(BOT_TOKEN).build()
    telegram_app.add_handler(CommandHandler("battle", cmd_battle))
    telegram_app.add_handler(CommandHandler("challenge", cmd_challenge))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handler_card_upload))
    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    await telegram_app.bot.set_webhook(WEBHOOK_URL)
    log.info(f"✅ Webhook set to {WEBHOOK_URL}")

@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.bot.delete_webhook()
        await telegram_app.stop()
        await telegram_app.shutdown()
        log.info("🛑 Telegram bot stopped.")
