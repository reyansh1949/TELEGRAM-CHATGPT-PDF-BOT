import os
import re
import uuid
import threading
import logging
from pathlib import Path
from xml.sax.saxutils import escape

import requests
from flask import Flask, send_from_directory
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Preformatted
)

# ---------------- CONFIGURATION ----------------

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN Render Environment Variables mein missing hai.")

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ---------------- WEBSITE ----------------

@app.route("/")
def home():
    return "ChatGPT PDF Bot is running!"

@app.route("/pdf/<filename>")
def download_pdf(filename):
    if not re.fullmatch(r"[a-f0-9-]+\.pdf", filename):
        return "Invalid PDF name", 400

    return send_from_directory(
        PDF_DIR,
        filename,
        as_attachment=False
    )

# ---------------- FONT ----------------

def setup_font():
    font_path = PDF_DIR / "NotoSansDevanagari-Regular.ttf"

    if not font_path.exists():
        font_url = (
            "https://raw.githubusercontent.com/google/fonts/"
            "main/ofl/notosansdevanagari/static/"
            "NotoSansDevanagari-Regular.ttf"
        )

        try:
            response = requests.get(font_url, timeout=30)
            response.raise_for_status()
            font_path.write_bytes(response.content)
        except Exception as error:
            logging.warning("Hindi font download failed: %s", error)
            return "Helvetica"

    try:
        pdfmetrics.registerFont(
            TTFont("HindiFont", str(font_path))
        )
        return "HindiFont"
    except Exception as error:
        logging.warning("Font registration failed: %s", error)
        return "Helvetica"


FONT_NAME = setup_font()

# ---------------- CHAT EXTRACTION ----------------

def extract_chat(url):
    match = re.search(
        r"https?://(?:www\.)?chatgpt\.com/share/([a-zA-Z0-9-]+)",
        url.strip()
    )

    if not match:
        raise ValueError("Valid ChatGPT public share link bhejein.")

    share_id = match.group(1)

    api_url = (
        "https://chatgpt.com/backend-api/share/"
        + share_id
    )

    response = requests.get(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        },
        timeout=40
    )

    response.raise_for_status()
    data = response.json()

    title = data.get("title") or "ChatGPT Notes"
    mapping = data.get("mapping") or {}

    messages = []

    for node in mapping.values():
        message = node.get("message")

        if not message:
            continue

        role = message.get("author", {}).get("role", "")

        if role not in ("user", "assistant"):
            continue

        content = message.get("content") or {}
        parts = content.get("parts") or []

        for part in parts:
            if isinstance(part, str):
                messages.append(
                    role.upper() + ":\n" + part
                )

    if not messages:
        raise ValueError(
            "Conversation nahi mili. Share link public hona chahiye. "
            "ChatGPT ka share API badla hua bhi ho sakta hai."
        )

    return title, "\n\n".join(messages)

# ---------------- PDF CREATION ----------------

def make_pdf(title, content):
    filename = str(uuid.uuid4()) + ".pdf"
    filepath = PDF_DIR / filename

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "CustomTitle",
        parent=styles["Title"],
        fontName=FONT_NAME,
        fontSize=18,
        leading=24,
        alignment=TA_CENTER,
        spaceAfter=15
    )

    body_style = ParagraphStyle(
        "CustomBody",
        parent=styles["Normal"],
        fontName=FONT_NAME,
        fontSize=10,
        leading=16,
        spaceAfter=8,
        wordWrap="CJK"
    )

    document = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    story = [
        Paragraph(escape(title), title_style),
        Spacer(1, 10)
    ]

    for block in content.split("\n\n"):
        safe_text = escape(block).replace("\n", "<br/>")

        story.append(
            Paragraph(safe_text, body_style)
        )
        story.append(Spacer(1, 5))

    document.build(story)

    return filename

# ---------------- TELEGRAM BOT ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to ChatGPT PDF Bot!\n\n"
        "ChatGPT public share link bhejein. "
        "Main PDF banakar link bhejunga."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    if "chatgpt.com/share/" not in url:
        await update.message.reply_text(
            "Kripya ChatGPT public share link bhejein."
        )
        return

    await update.message.reply_text(
        "Aapki PDF ban rahi hai. Kripya wait karein..."
    )

    try:
        title, content = extract_chat(url)

        filename = make_pdf(title, content)

        if not BASE_URL:
            raise RuntimeError(
                "Render external URL available nahi hai."
            )

        pdf_url = BASE_URL + "/pdf/" + filename

        await update.message.reply_text(
            "PDF ready hai!\n\n" + pdf_url
        )

    except Exception as error:
        logging.exception("PDF conversion failed")

        await update.message.reply_text(
            "Conversion failed:\n" + str(error)[:700]
        )

# ---------------- START APPLICATION ----------------

def run_website():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


def main():
    website_thread = threading.Thread(
        target=run_website,
        daemon=True
    )
    website_thread.start()

    bot = Application.builder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start", start))

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    logging.info("Telegram bot starting...")
    bot.run_polling()


if __name__ == "__main__":
    main()
    
