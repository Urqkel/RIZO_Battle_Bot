# app.py
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
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from PIL import Image
import pytesseract

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # e.g. https://rizo-battle-bot.onrender.com
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
    raise RuntimeError("BOT_TOKEN or RENDER_EXTERNAL_URL missing in environment.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rizo-battle-bot")

# ---------- FastAPI ----------
app = FastAPI()
app.mount("/static", StaticFiles(directory="static"), name="static")

# ---------- Storage ----------
os.makedirs("battles", exist_ok=True)
os.makedirs("cards", exist_ok=True)
DB_PATH = "battles.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
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
        """
    )
    conn.commit()
    conn.close()

init_db()

# ---------- Runtime state ----------
pending_challenges: dict[int, str] = {}  # challenger_id -> opponent_username
uploaded_cards: dict[int, dict] = {}      # user_id -> card info

# ---------- OCR helpers ----------
def ocr_text_from_bytes(file_bytes: bytes) -> str:
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    return pytesseract.image_to_string(image)

def parse_stats_from_text(text: str) -> dict:
    lower = text.lower()
    # HP
    hp_match = re.search(r"hp[:\s]*([0-9]{1,4})", lower)
    hp = int(hp_match.group(1)) if hp_match else 100
    # Defense
    defense_match = re.search(r"defen(?:se|c)e?[:\s]*([0-9]{1,4})", lower)
    defense = int(defense_match.group(1)) if defense_match else 50
    # Serial
    serial_match = re.search(r"#\s*([0-9]{1,4})", text)
    serial = int(serial_match.group(1)) if serial_match else 1000
    # Attacks
    attack_patterns = re.findall(r"([a-z\s]+)\s*[:\-]?\s*([0-9]{1,4})", text, re.IGNORECASE)
    attacks = []
    for name, val in attack_patterns:
        name = name.strip().title()
        if any(k in name.lower() for k in ["attack", "move", "strike", "blast", "slash"]):
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

# ---------- HP calculation ----------
def calculate_hp(card: dict) -> int:
    serial_bonus = (2000 - card["serial"]) // 50
    return max(1, card["hp"] + serial_bonus)

# ---------- Battle simulation ----------
ELEMENTAL_MODIFIERS = {
    "fire": {"water": 0.5, "earth": 1.0, "fire": 1.0},
    "water": {"fire": 1.5, "earth": 1.0, "water": 1.0},
    "earth": {"fire": 1.0, "water": 1.0, "earth": 1.0},
}

def get_element(move_name: str) -> str:
    name = move_name.lower()
    if "fire" in name: return "fire"
    if "water" in name: return "water"
    if "earth" in name: return "earth"
    return "normal"

def simulate_battle(card1: dict, card2: dict):
    hp1 = calculate_hp(card1)
    hp2 = calculate_hp(card2)
    defense1 = card1["defense"]
    defense2 = card2["defense"]
    turn = 0
    battle_log = []

    while hp1 > 0 and hp2 > 0:
        if turn % 2 == 0:
            move_name, move_power = random.choice([(card1["attack1_name"], card1["attack1_power"]),
                                                    (card1["attack2_name"], card1["attack2_power"])])
            elem1 = get_element(move_name)
            elem2 = get_element(card2["attack1_name"])
            modifier = ELEMENTAL_MODIFIERS.get(elem1, {}).get(elem2, 1.0)
            dmg = int(move_power * random.uniform(0.8, 1.2) * modifier) - int(defense2 * 0.1)
            dmg = max(5, dmg)
            hp2 -= dmg
            battle_log.append(f"{card1['username']} used {move_name} → {dmg} dmg!")
        else:
            move_name, move_power = random.choice([(card2["attack1_name"], card2["attack1_power"]),
                                                    (card2["attack2_name"], card2["attack2_power"])])
            elem1 = get_element(move_name)
            elem2 = get_element(card1["attack1_name"])
            modifier = ELEMENTAL_MODIFIERS.get(elem1, {}).get(elem2, 1.0)
            dmg = int(move_power * random.uniform(0.8, 1.2) * modifier) - int(defense1 * 0.1)
            dmg = max(5, dmg)
            hp1 -= dmg
            battle_log.append(f"{card2['username']} used {move_name} → {dmg} dmg!")
        turn += 1

    winner = card1["username"] if hp1 > 0 else (card2["username"] if hp2 > 0 else None)
    return {"winner": winner, "hp1_end": max(0, hp1), "hp2_end": max(0, hp2), "log": battle_log}

# ---------- HTML replay ----------
def save_battle_html(battle_id: str, context: dict):
    html_path = f"battles/{battle_id}.html"
    image_src = "/static/battle_placeholder.mp4"
    html = f"""
    <!DOCTYPE html>
    <html>
    <head><title>Battle {battle_id}</title></head>
    <body style="background:#0d0d0d;color:white;text-align:center;font-family:Arial,sans-serif;">
        <h1>Battle Replay: {battle_id}</h1>
        <img src="{image_src}" alt="Battle Replay" style="width:400px;border-radius:12px;">
        <p>{context.get('winner_name','Pending')}</p>
    </body>
    </html>
    """
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path

def persist_battle_record(battle_id: str, challenger_username: str, challenger_stats: dict,
                          opponent_username: str, opponent_stats: dict, winner: Optional[str], html_path: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO battles (id,timestamp,challenger_username,challenger_stats,opponent_username,opponent_stats,winner,html_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (battle_id, datetime.utcnow().isoformat(), challenger_username, json.dumps(challenger_stats),
         opponent_username, json.dumps(opponent_stats), winner or "", html_path)
    )
    conn.commit()
    conn.close()

# ---------- Telegram handlers ----------
async def cmd_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⚔️ Rizo Battle Bot\nUse /challenge @username to challenge.\nUpload your Rizo battle card (photo/file)."
    )

async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args or not context.args[0].startswith("@"):
        await update.message.reply_text("Usage: /challenge @username")
        return
    challenger = update.effective_user
    opponent_username = context.args[0].lstrip("@").strip().lower()
    pending_challenges[challenger.id] = opponent_username
    await update.message.reply_text(f"⚔️ @{challenger.username} challenged @{opponent_username}! Upload cards now.")

async def handler_card_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    username = (user.username or f"user{user.id}").lower()
    user_id = user.id

    # Download file
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
    elif update.message.document:
        file_obj = await update.message.document.get_file()
    else:
        await update.message.reply_text("Upload an image (photo or file).")
        return
    file_bytes = await file_obj.download_as_bytearray()

    os.makedirs("cards", exist_ok=True)
    save_path = f"cards/{username}.png"
    with open(save_path, "wb") as f:
        f.write(file_bytes)

    try:
        parsed = parse_stats_from_text(ocr_text_from_bytes(file_bytes))
    except Exception:
        parsed = {"hp":100,"defense":50,"serial":1000,"attack1_name":"Basic Strike","attack1_power":30,"attack2_name":"Heavy Blow","attack2_power":40}

    card = {"username":username, "user_id":user_id, "path":save_path, **parsed}
    uploaded_cards[user_id] = card
    await update.message.reply_text(f"✅ @{username}'s card received — Calculating HP...")

    # Trigger battle if both uploaded
    triggered_pair = None
    if user_id in pending_challenges:
        opp_name = pending_challenges[user_id]
        opp_id = next((uid for uid,c in uploaded_cards.items() if c["username"]==opp_name), None)
        if opp_id: triggered_pair = (user_id, opp_id)
    if not triggered_pair:
        for challenger_id, opp_name in pending_challenges.items():
            if username==opp_name and challenger_id in uploaded_cards:
                triggered_pair = (challenger_id, user_id)
                break

    if triggered_pair:
        c1_id,c2_id = triggered_pair
        card1, card2 = uploaded_cards[c1_id], uploaded_cards[c2_id]
        result = simulate_battle(card1, card2)
        battle_id = str(uuid.uuid4())
        html_path = save_battle_html(battle_id, {"winner_name":result["winner"] or "Tie"})
        persist_battle_record(battle_id, card1["username"], card1, card2["username"], card2, result["winner"], html_path)

        replay_url = f"{RENDER_EXTERNAL_URL}/battle/{battle_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 View Battle Replay", url=replay_url)]])
        summary_text = f"⚔️ Battle complete!\n"
        summary_text += f"🏆 Winner: @{result['winner']}\n" if result['winner'] else "🤝 It's a tie!\n"
        summary_text += f"@{card1['username']} HP: {result['hp1_end']} vs @{card2['username']} HP: {result['hp2_end']}\n"
        summary_text += "\n".join(result["log"][:3]) + ("\n..." if len(result["log"])>3 else "")
        await update.message.reply_text(summary_text, reply_markup=keyboard)

        # Clean up
        uploaded_cards.pop(c1_id, None)
        uploaded_cards.pop(c2_id, None)
        pending_challenges.pop(c1_id, None)

# ---------- FastAPI routes ----------
@app.get("/")
async def root():
    return {"status": "ok", "service": "Rizo Battle Bot"}

@app.get("/battle/{battle_id}", response_class=HTMLResponse)
async def battle_page(battle_id: str):
    battle_file = f"battles/{battle_id}.html"
    if os.path.exists(battle_file):
        return FileResponse(battle_file, media_type="text/html")
    return HTMLResponse("<h1>Battle not found.</h1>")

@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

# ---------- Telegram app startup ----------
telegram_app: Optional[Application] = None

@app.on_event("startup")
async def on_startup():
    global telegram_app
    log.info("Starting Telegram bot...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
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
