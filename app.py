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
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from PIL import Image
import pytesseract

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
# e.g. https://rizo-battle-bot.onrender.com. Used to construct the WEBHOOK_URL.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  
# Reads the PORT environment variable provided by Render, defaults to 10000.
PORT = int(os.getenv("PORT", 10000)) 

if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
    # Use log.error instead of raising for cleaner shutdown context
    logging.error("BOT_TOKEN or RENDER_EXTERNAL_URL missing in environment.")

# --- FIX: Using a simpler, shorter path for robustness ---
WEBHOOK_PATH = "/telegram-hook"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"
# --------------------------------------------------------

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
    # Ensure serial is treated as int
    serial_num = int(card.get("serial", 1000))
    hp_base = int(card.get("hp", 100))
    serial_bonus = (2000 - serial_num) // 50
    return max(1, hp_base + serial_bonus)

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

    while hp1 > 0 and hp2 > 0 and turn < 100: # Add turn limit to prevent infinite loops
        attacker_card, defender_card = (card1, card2) if turn % 2 == 0 else (card2, card1)
        attacker_hp_ref, defender_hp_ref = (hp1, hp2) if turn % 2 == 0 else (hp2, hp1)
        defender_defense = defense2 if turn % 2 == 0 else defense1

        move_name, move_power = random.choice([(attacker_card["attack1_name"], attacker_card["attack1_power"]),
                                                (attacker_card["attack2_name"], attacker_card["attack2_power"])])
        
        elem1 = get_element(move_name)
        # For simplicity, base element effectiveness on the opponent's first move element
        elem2 = get_element(defender_card["attack1_name"]) 
        modifier = ELEMENTAL_MODIFIERS.get(elem1, {}).get(elem2, 1.0)
        
        # Damage calculation logic
        raw_damage = move_power * random.uniform(0.8, 1.2) * modifier
        damage_reduction = defender_defense * 0.1
        dmg = int(raw_damage - damage_reduction)
        dmg = max(5, dmg) # Minimum damage is 5

        # Update defender's HP
        if turn % 2 == 0:
            hp2 -= dmg
        else:
            hp1 -= dmg
        
        battle_log.append(f"@{attacker_card['username']} used {move_name} ({int(modifier*100)}% eff.) → {dmg} dmg!")
        turn += 1

    winner = card1["username"] if hp1 > 0 else (card2["username"] if hp2 > 0 else None)
    return {"winner": winner, "hp1_end": max(0, hp1), "hp2_end": max(0, hp2), "log": battle_log}


# ---------- HTML replay ----------
def save_battle_html(battle_id: str, context: dict):
    html_path = f"battles/{battle_id}.html"
    
    # Use a generic image placeholder URL since we cannot include local files in a public URL
    image_src = "https://placehold.co/400x200/ff6666/ffffff?text=BATTLE+REPLAY" 
    
    log_content = "\n".join([f"<p>{line}</p>" for line in context['log']])
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Battle Replay: {battle_id}</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.tailwindcss.com"></script>
        <style>
            body {{
                font-family: 'Inter', sans-serif;
            }}
            .card {{
                box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.1), 0 2px 4px -2px rgba(0, 0, 0, 0.1);
            }}
        </style>
    </head>
    <body class="bg-gray-900 text-white min-h-screen p-4 flex flex-col items-center">
        <div class="max-w-xl w-full">
            <h1 class="text-3xl font-bold mb-4 text-red-400">⚔️ Rizo Battle Replay</h1>
            <div class="bg-gray-800 p-6 rounded-xl card mb-6">
                <h2 class="text-2xl font-semibold mb-3">ID: {battle_id[:8]}...</h2>
                <div class="flex flex-col sm:flex-row justify-around items-center mb-4 space-y-4 sm:space-y-0">
                    <div class="text-center">
                        <p class="text-xl font-bold text-blue-400">@{context['card1']['username']}</p>
                        <p class="text-sm">HP: {context['hp1_end']}</p>
                    </div>
                    <p class="text-2xl font-extrabold text-red-500">VS</p>
                    <div class="text-center">
                        <p class="text-xl font-bold text-green-400">@{context['card2']['username']}</p>
                        <p class="text-sm">HP: {context['hp2_end']}</p>
                    </div>
                </div>
                
                <p class="text-4xl font-black mt-4 mb-4">🏆 {context.get('winner_name','DRAW')}</p>
            </div>

            <div class="bg-gray-800 p-4 rounded-xl card">
                <h2 class="text-xl font-semibold mb-2 text-yellow-300">Battle Log</h2>
                <div class="h-64 overflow-y-scroll bg-gray-900 p-3 rounded-lg text-left text-sm space-y-1">
                    {log_content}
                </div>
            </div>
            <p class="mt-4 text-xs text-gray-500">
                This page is running on the bot's server.
            </p>
        </div>
    </body>
    </html>
    """
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    return html_path

def persist_battle_record(battle_id: str, challenger_username: str, challenger_stats: dict,
                          opponent_username: str, opponent_stats: dict, winner: Optional[str], html_path: str, hp1_end: int, hp2_end: int):
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
    
    if not challenger.username:
        await update.message.reply_text("Please set a Telegram username to issue challenges.")
        return
        
    opponent_username = context.args[0].lstrip("@").strip().lower()
    pending_challenges[challenger.id] = opponent_username
    await update.message.reply_text(f"⚔️ @{challenger.username} challenged @{opponent_username}! Upload cards now.")

async def handler_card_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    if not user.username:
        await update.message.reply_text("Please set a Telegram username before uploading a card for battle.")
        return
        
    username = user.username.lower()
    user_id = user.id

    # Download file
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
    elif update.message.document:
        # Check if the document is an image MIME type
        if not update.message.document.mime_type or not update.message.document.mime_type.startswith("image/"):
             await update.message.reply_text("Please upload an image (photo or image file).")
             return
        file_obj = await update.message.document.get_file()
    else:
        # This branch should not be reached due to the filter, but acts as a fallback
        await update.message.reply_text("Upload an image (photo or file).")
        return
        
    # Check if the file is too large before downloading (Telegram limit is 20MB, but safer to check)
    if file_obj.file_size and file_obj.file_size > 5 * 1024 * 1024: # 5MB limit for speed
        await update.message.reply_text("File size limit exceeded (5MB). Please upload a smaller image.")
        return

    try:
        file_bytes = await file_obj.download_as_bytearray()
    except Exception as e:
        log.error(f"Error downloading file: {e}")
        await update.message.reply_text("Failed to download the file. Please try again.")
        return

    os.makedirs("cards", exist_ok=True)
    save_path = f"cards/{username}.png"
    try:
        with open(save_path, "wb") as f:
            f.write(file_bytes)

        ocr_text = ocr_text_from_bytes(file_bytes)
        parsed = parse_stats_from_text(ocr_text)
    except Exception as e:
        log.warning(f"Error processing OCR/File save: {e}. Using default stats.")
        parsed = {"hp":100,"defense":50,"serial":1000,"attack1_name":"Basic Strike","attack1_power":30,"attack2_name":"Heavy Blow","attack2_power":40}

    card = {"username":username, "user_id":user_id, "path":save_path, **parsed}
    uploaded_cards[user_id] = card
    await update.message.reply_text(f"✅ @{username}'s card received — Base HP: {card['hp']} (Calculated HP: {calculate_hp(card)})")

    # Trigger battle if both uploaded
    triggered_pair = None
    
    # Case 1: Challenger uploads card after challenging
    if user_id in pending_challenges:
        opp_name = pending_challenges[user_id]
        opp_id = next((uid for uid,c in uploaded_cards.items() if c["username"]==opp_name), None)
        if opp_id and opp_id != user_id: # Ensure user isn't challenging/fighting self
             triggered_pair = (user_id, opp_id)
             
    # Case 2: Opponent uploads card matching an existing challenge
    if not triggered_pair:
        for challenger_id, opp_name in pending_challenges.items():
            if username==opp_name and challenger_id in uploaded_cards and challenger_id != user_id:
                triggered_pair = (challenger_id, user_id)
                break

    if triggered_pair:
        c1_id,c2_id = triggered_pair
        card1, card2 = uploaded_cards[c1_id], uploaded_cards[c2_id]
        
        # Simulate and get results
        result = simulate_battle(card1, card2)
        battle_id = str(uuid.uuid4())
        
        # Save HTML replay
        html_context = {
            "winner_name": result["winner"] or "Tie",
            "card1": card1, "card2": card2,
            "hp1_end": result["hp1_end"], "hp2_end": result["hp2_end"],
            "log": result["log"]
        }
        html_path = save_battle_html(battle_id, html_context)
        
        # Persist to database
        persist_battle_record(battle_id, card1["username"], card1, card2["username"], card2, 
                              result["winner"], html_path, result["hp1_end"], result["hp2_end"])

        # Send notification
        replay_url = f"{RENDER_EXTERNAL_URL}/battle/{battle_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 View Battle Replay", url=replay_url)]])
        
        winner_text = f"🏆 Winner: @{result['winner']}" if result['winner'] else "🤝 It's a tie!"
        summary_text = f"⚔️ Battle complete!\n{winner_text}\n"
        summary_text += f"@{card1['username']} HP: {result['hp1_end']} vs @{card2['username']} HP: {result['hp2_end']}\n"
        summary_text += "\n\n**Log Snippet:**\n"
        summary_text += "\n".join(result["log"][:3]) + ("\n...(see replay for full log)" if len(result["log"])>3 else "")
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=summary_text, 
            reply_markup=keyboard
        )

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
    return HTMLResponse("<h1 class='text-white bg-gray-900'>Battle not found.</h1>", status_code=404)

# --- FIX: Match the new, simpler WEBHOOK_PATH ---
@app.post(WEBHOOK_PATH)
async def telegram_webhook(req: Request):
    data = await req.json()
    
    if not telegram_app or not telegram_app.bot:
        log.error("Telegram Application is not initialized.")
        return {"ok": False, "error": "Bot not initialized"}
        
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}
# --------------------------------------------------------

# ---------- Telegram app startup ----------
telegram_app: Optional[Application] = None

@app.on_event("startup")
async def on_startup():
    global telegram_app
    
    if not BOT_TOKEN or not RENDER_EXTERNAL_URL:
        log.warning("Startup aborted: BOT_TOKEN or RENDER_EXTERNAL_URL missing.")
        return
        
    log.info("Starting Telegram bot initialization...")
    telegram_app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Add handlers
    telegram_app.add_handler(CommandHandler("battle", cmd_battle))
    telegram_app.add_handler(CommandHandler("challenge", cmd_challenge))
    # Filter for photos and image documents
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handler_card_upload))

    await telegram_app.initialize()
    
    # Set the webhook to the external URL
    try:
        await telegram_app.bot.delete_webhook(drop_pending_updates=True)
        await telegram_app.bot.set_webhook(WEBHOOK_URL)
        log.info(f"✅ Webhook set successfully to {WEBHOOK_URL}")
    except Exception as e:
        log.error(f"Failed to set webhook: {e}")
        pass 

@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        log.info("Shutting down and deleting webhook...")
        await telegram_app.bot.delete_webhook()
