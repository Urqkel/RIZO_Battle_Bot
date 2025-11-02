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

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from telegram import Update, InputFile, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

from PIL import Image, ImageDraw, ImageFont
import pytesseract

# ---------- Config ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")  # e.g. https://pfp-battle-bot.onrender.com
PORT = int(os.getenv("PORT", 10000))

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in environment.")
if not RENDER_EXTERNAL_URL:
    raise RuntimeError("RENDER_EXTERNAL_URL missing in environment.")

WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
WEBHOOK_URL = f"{RENDER_EXTERNAL_URL}{WEBHOOK_PATH}"

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("pfp-battle-bot")

# ---------- FastAPI + Templates ----------
app = FastAPI()
templates = Jinja2Templates(directory="templates")
app.mount("/static", StaticFiles(directory="static"), name="static")

# create battles folder
os.makedirs("battles", exist_ok=True)
os.makedirs("cards", exist_ok=True)

# ---------- Simple SQLite storage for battles ----------
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

# ---------- In-memory runtime state ----------
# pending_challenges: challenger_id -> opponent_username (string without @)
pending_challenges: dict[int, str] = {}
# uploaded_cards: user_id -> card dict (username, path, power, defense, rarity, serial)
uploaded_cards: dict[int, dict] = {}


# ---------- OCR / Parsing helpers ----------
RARITY_BONUS = {"common": 0, "rare": 20, "ultrarare": 40, "ultra-rare": 40, "legendary": 60}


def ocr_text_from_bytes(file_bytes: bytes) -> str:
    """Return OCR text; raises if image unreadable."""
    image = Image.open(io.BytesIO(file_bytes)).convert("RGB")
    text = pytesseract.image_to_string(image)
    return text


def parse_stats_from_text(text: str) -> dict:
    """
    Look for power, defense, rarity, serial in OCR text.
    Use robust fallbacks.
    """
    lower = text.lower()

    # rarity
    rarity = "Common"
    for key in ["legendary", "ultra-rare", "ultra rare", "ultrarare", "rare", "common"]:
        if key in lower:
            if "ultra" in key:
                rarity = "Ultra-Rare"
            else:
                rarity = key.capitalize()
            break

    # find numbers
    nums = [int(n) for n in re.findall(r"\b([0-9]{1,4})\b", text)]

    # power (explicit first)
    power = None
    m = re.search(r"power[:\s]*([0-9]{1,4})", lower)
    if m:
        power = int(m.group(1))
    else:
        m2 = re.search(r"(attack|atk)[:\s]*([0-9]{1,4})", lower)
        if m2:
            power = int(m2.group(2))

    # defense
    defense = None
    m = re.search(r"defen(?:se|c)e?[:\s]*([0-9]{1,4})", lower)
    if m:
        defense = int(m.group(1))
    else:
        m2 = re.search(r"\bdef[:\s]*([0-9]{1,4})\b", lower)
        if m2:
            defense = int(m2.group(1))

    # serial
    serial = None
    m = re.search(r"serial[:\s#]*([0-9]{1,4})", lower)
    if m:
        serial = int(m.group(1))
    else:
        m2 = re.search(r"#\s*([0-9]{1,4})", text)
        if m2:
            serial = int(m2.group(1))
        else:
            m3 = re.search(r"s\/n[:\s]*([0-9]{1,4})", lower)
            if m3:
                serial = int(m3.group(1))

    # sensible fallbacks
    if power is None:
        power = nums[0] if len(nums) >= 1 else 50
    if defense is None:
        defense = nums[1] if len(nums) >= 2 else 50
    if serial is None:
        # choose smallest found (more exclusive)
        serial = min(nums) if nums else 1000

    # clamp serial to 1..1999
    serial = max(1, min(int(serial), 1999))

    return {"power": int(power), "defense": int(defense), "rarity": rarity, "serial": int(serial)}


# ---------- HP calculation ----------
def calculate_hp(card: dict) -> int:
    """
    HP = base + rarity_bonus + serial_bonus
    base = power + defense
    serial_bonus = (2000 - serial) / 50
    """
    base = card.get("power", 50) + card.get("defense", 50)
    rarity_key = card.get("rarity", "Common").lower()
    rarity_bonus = RARITY_BONUS.get(rarity_key, 0)
    serial = int(card.get("serial", 1000))
    serial_bonus = (2000 - serial) / 50.0
    hp = int(base + rarity_bonus + serial_bonus)
    return max(1, hp)


# ---------- Battle simulation ----------
def simulate_battle(hp1: int, hp2: int, power1: int, power2: int):
    """Simple looped battle returning final hp1, hp2."""
    # deterministic-ish randomness
    while hp1 > 0 and hp2 > 0:
        dmg1 = max(1, int(power1 * random.uniform(0.08, 0.16)))
        dmg2 = max(1, int(power2 * random.uniform(0.08, 0.16)))
        hp2 -= dmg1
        if hp2 <= 0:
            break
        hp1 -= dmg2
    return max(0, hp1), max(0, hp2)


# ---------- Replay HTML generation ----------
def save_battle_html(battle_id: str, replay_url: str = None):
    """
    Save a battle HTML page that mirrors the working test_battle demo.
    If replay_url is provided, it will embed that; otherwise uses placeholder.
    """
    os.makedirs("battles", exist_ok=True)

    # Default to placeholder image if no replay available
    image_src = replay_url if replay_url else "/static/battle_placeholder.mp4"

    battle_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Battle {battle_id}</title>
        <style>
            body {{ 
                background-color: #0d0d0d; 
                color: white; 
                text-align: center; 
                font-family: Arial, sans-serif; 
            }}
            img {{ 
                width: 400px; 
                height: auto; 
                margin-top: 50px; 
                border-radius: 12px;
                box-shadow: 0 0 20px rgba(255,255,255,0.3);
            }}
        </style>
    </head>
    <body>
        <h1>Battle Replay: {battle_id}</h1>
        <img src="{image_src}" alt="Battle Replay">
        <p>{'Battle replay generated!' if replay_url else 'Static placeholder shown until replay is ready.'}</p>
    </body>
    </html>
    """

    file_path = f"battles/{battle_id}.html"
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(battle_html)

    return file_path


def persist_battle_record(battle_id: str, challenger_username: str, challenger_stats: dict,
                          opponent_username: str, opponent_stats: dict, winner: Optional[str], html_path: str):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        "INSERT INTO battles (id, timestamp, challenger_username, challenger_stats, opponent_username, opponent_stats, winner, html_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            battle_id,
            datetime.utcnow().isoformat(),
            challenger_username,
            json.dumps(challenger_stats),
            opponent_username,
            json.dumps(opponent_stats),
            winner or "",
            html_path,
        ),
    )
    conn.commit()
    conn.close()


# ---------- Telegram handlers ----------
async def cmd_battle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wake/help command."""
    await update.message.reply_text(
        "⚔️ PFP Battle Bot\n"
        "Use /challenge @username to challenge someone.\n"
        "Then both players upload their PFP battle card in the chat (photo or file)."
    )


async def cmd_challenge(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Store pending challenge (challenger -> opponent_username)."""
    if not context.args or not context.args[0].startswith("@"):
        await update.message.reply_text("Usage: /challenge @username")
        return

    challenger = update.effective_user
    opponent_username = context.args[0].lstrip("@").strip()
    pending_challenges[challenger.id] = opponent_username
    log.info("Challenge: @%s -> @%s", challenger.username, opponent_username)

    await update.message.reply_text(
        f"⚔️ @{challenger.username} has challenged @{opponent_username}!\n"
        "Both players: upload your battle card image in this chat. Uploads can be in any order."
    )


async def handler_card_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receive uploaded card (photo or document), OCR it, store, and trigger battle if both ready."""
    user = update.effective_user
    username = (user.username or f"user{user.id}").lower()
    user_id = user.id

    # get file bytes
    file_obj = None
    if update.message.photo:
        file_obj = await update.message.photo[-1].get_file()
    elif update.message.document:
        file_obj = await update.message.document.get_file()
    else:
        await update.message.reply_text("Please upload an image (photo or file).")
        return

    file_bytes = await file_obj.download_as_bytearray()

    # save image to disk
    os.makedirs("cards", exist_ok=True)
    save_path = f"cards/{username}.png"
    try:
        with open(save_path, "wb") as f:
            f.write(file_bytes)
    except Exception:
        log.exception("Failed saving uploaded card to %s", save_path)

    # OCR + parse
    try:
        ocr_text = ocr_text_from_bytes(file_bytes)
        parsed = parse_stats_from_text(ocr_text)
    except Exception as e:
        log.exception("OCR failure for @%s: %s", username, e)
        parsed = {"power": 50, "defense": 50, "rarity": "Common", "serial": 1000}

    card = {
        "username": username,
        "user_id": user_id,
        "path": save_path,
        "power": int(parsed["power"]),
        "defense": int(parsed["defense"]),
        "rarity": parsed["rarity"],
        "serial": int(parsed["serial"]),
    }

    uploaded_cards[user_id] = card

    await update.message.reply_text(
        f"✅ @{username}'s card received — Calculating HP"
    )

    # Determine if this upload completes a pending challenge
    triggered_pair = None  # (challenger_id, opponent_id)

    # First, check if uploader is a challenger who has a pending opponent username
    if user_id in pending_challenges:
        opponent_username = pending_challenges[user_id].lower()
        # look for opponent's user_id by matching uploaded_cards username
        opponent_id = next(
            (uid for uid, c in uploaded_cards.items() if c["username"].lower() == opponent_username),
            None,
        )
        if opponent_id:
            triggered_pair = (user_id, opponent_id)

    # Next, check if uploader matches any pending opponent (uploader is opponent)
    if not triggered_pair:
        for challenger_id, opponent_username in pending_challenges.items():
            if username == opponent_username.lower():
                # check if challenger uploaded already
                if challenger_id in uploaded_cards:
                    triggered_pair = (challenger_id, user_id)
                    break

    # If both have uploaded, run battle
    if triggered_pair:
        challenger_id, opponent_id = triggered_pair
        card1 = uploaded_cards.get(challenger_id)
        card2 = uploaded_cards.get(opponent_id)
        if not card1 or not card2:
            log.warning("Triggered pair but missing card data (c:%s, o:%s)", challenger_id, opponent_id)
            return

        # compute HPs
        hp1_start = calculate_hp(card1)
        hp2_start = calculate_hp(card2)

        # simulate battle
        hp1_end, hp2_end = simulate_battle(hp1_start, hp2_start, card1["power"], card2["power"])

        # decide winner
        if hp1_end > hp2_end:
            winner = card1["username"]
        elif hp2_end > hp1_end:
            winner = card2["username"]
        else:
            winner = None  # tie

        # create battle_id and save HTML replay
        battle_id = str(uuid.uuid4())
        context_for_template = {
            "card1_name": card1["username"],
            "card2_name": card2["username"],
            "card1_stats": {"power": card1["power"], "defense": card1["defense"], "rarity": card1["rarity"], "serial": card1["serial"]},
            "card2_stats": {"power": card2["power"], "defense": card2["defense"], "rarity": card2["rarity"], "serial": card2["serial"]},
            "hp1_start": hp1_start,
            "hp2_start": hp2_start,
            "hp1_end": hp1_end,
            "hp2_end": hp2_end,
            "winner_name": winner or "Tie",
            "battle_id": battle_id,
        }

        html_path = save_battle_html(battle_id, context_for_template)

        # persist metadata
        persist_battle_record(
            battle_id,
            card1["username"],
            {"power": card1["power"], "defense": card1["defense"], "rarity": card1["rarity"], "serial": card1["serial"]},
            card2["username"],
            {"power": card2["power"], "defense": card2["defense"], "rarity": card2["rarity"], "serial": card2["serial"]},
            winner,
            html_path,
        )

        # send message with View Replay button
        replay_url = f"{RENDER_EXTERNAL_URL}/battle/{battle_id}"
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 View Battle Replay", url=replay_url)]])
        summary_text = f"⚔️ Battle complete!\n"
        if winner:
            summary_text += f"🏆 Winner: @{winner}\n"
        else:
            summary_text += "🤝 It's a tie!\n"
        summary_text += f"@{card1['username']} HP: {hp1_end} vs @{card2['username']} HP: {hp2_end}"

        # send reply to the chat where upload happened
        await update.message.reply_text(summary_text, reply_markup=keyboard)

        # cleanup in-memory state for these players
        uploaded_cards.pop(challenger_id, None)
        uploaded_cards.pop(opponent_id, None)
        pending_challenges.pop(challenger_id, None)

    # else: wait for the other player
    else:
        # If uploader is challenger, inform waiting
        waiting_for = None
        if user_id in pending_challenges:
            waiting_for = pending_challenges[user_id]
        else:
            # check if this username matches any pending opponent
            for challenger_id, opponent_username in pending_challenges.items():
                if username == opponent_username.lower():
                    waiting_for = f"@{list(filter(lambda x: x==challenger_id, [challenger_id]))}"
                    break

        if waiting_for:
            await update.message.reply_text(f"Card received. Waiting for {waiting_for} to upload theirs.")
        else:
            await update.message.reply_text("Card received. If you are challenging someone, use /challenge @username first.")


# ---------- FastAPI routes ----------
@app.get("/")
async def root():
    return {"status": "ok", "service": "PFP Battle Bot"}


@app.get("/battle/{battle_id}", response_class=HTMLResponse)
async def battle_page(request: Request, battle_id: str):
    """Serve existing battle replay or show styled placeholder."""
    battle_file = f"battles/{battle_id}.html"
    if os.path.exists(battle_file):
        return FileResponse(battle_file, media_type="text/html")

    # Fallback placeholder — detect available static media
    placeholder_mp4 = "static/battle_placeholder.mp4"
    placeholder_gif = "static/battle_placeholder.gif"
    placeholder_png = "static/battle_placeholder.png"

    if os.path.exists(placeholder_mp4):
        replay_media = f"/{placeholder_mp4}"
        media_type = "video"
    elif os.path.exists(placeholder_gif):
        replay_media = f"/{placeholder_gif}"
        media_type = "image"
    elif os.path.exists(placeholder_png):
        replay_media = f"/{placeholder_png}"
        media_type = "image"
    else:
        # default fallback text
        replay_media = None
        media_type = "none"

    # Build inline HTML so it looks like the final replay page
    content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>Battle {battle_id}</title>
        <style>
            body {{
                background-color: #0d0d0d;
                color: white;
                text-align: center;
                font-family: Arial, sans-serif;
            }}
            .media {{
                width: 400px;
                margin-top: 50px;
                border-radius: 12px;
                box-shadow: 0 0 20px rgba(255,255,255,0.3);
            }}
            p {{ opacity: 0.8; margin-top: 10px; }}
        </style>
    </head>
    <body>
        <h1>Battle Replay: {battle_id}</h1>
        {f'<video class="media" autoplay loop muted playsinline><source src="{replay_media}" type="video/mp4"></video>' if media_type == 'video' else ''}
        {f'<img class="media" src="{replay_media}" alt="Battle Placeholder">' if media_type == 'image' else ''}
        {f'<p>Static placeholder shown until replay is ready.</p>' if media_type != 'none' else '<p>No placeholder file found.</p>'}
    </body>
    </html>
    """

    return HTMLResponse(content=content)


@app.get("/test_battle")
async def test_battle(request: Request):
    battle_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <title>Battle Replay Test</title>
        <style>
            body { 
                background-color: #0d0d0d; 
                color: white; 
                text-align: center; 
                font-family: Arial, sans-serif; 
            }
            img { 
                width: 400px; 
                height: auto; 
                margin-top: 50px; 
                border-radius: 12px;
                box-shadow: 0 0 20px rgba(255,255,255,0.3);
            }
        </style>
    </head>
    <body>
        <h1>Demo Battle Replay</h1>
        <img src="/static/battle_placeholder.mp4" alt="Battle Placeholder">
        <p>Static test loaded successfully!</p>
    </body>
    </html>
    """

    # Write the HTML for inspection
    os.makedirs("battles", exist_ok=True)
    with open("battles/demo_battle.html", "w", encoding="utf-8") as f:
        f.write(battle_html)

    return HTMLResponse(content=battle_html)

@app.post(WEBHOOK_PATH)
async def webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, telegram_app.bot)
    await telegram_app.process_update(update)
    return JSONResponse({"ok": True})


# ---------- Startup / Shutdown ----------
telegram_app: Optional[Application] = None


@app.on_event("startup")
async def on_startup():
    global telegram_app
    log.info("Starting Telegram Application and registering handlers...")

    telegram_app = Application.builder().token(BOT_TOKEN).build()

    # Register handlers BEFORE initialize()
    telegram_app.add_handler(CommandHandler("battle", cmd_battle))
    telegram_app.add_handler(CommandHandler("challenge", cmd_challenge))
    telegram_app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handler_card_upload))

    # Initialize & set webhook
    await telegram_app.initialize()
    await telegram_app.bot.delete_webhook(drop_pending_updates=True)
    await telegram_app.bot.set_webhook(WEBHOOK_URL)
    log.info("Webhook set to %s", WEBHOOK_URL)


@app.on_event("shutdown")
async def on_shutdown():
    if telegram_app:
        await telegram_app.bot.delete_webhook()
        await telegram_app.shutdown()
        await telegram_app.stop()
    log.info("Bot stopped cleanly.")


# ---------- Run guard ----------
if __name__ == "__main__":
    # Only used for local testing; Render runs via uvicorn
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=PORT, reload=False)
