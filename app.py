import asyncio
import os
import re
import uuid
import threading
import requests
from bs4 import BeautifulSoup
from flask import Flask, send_from_directory
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm

TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

app = Flask(__name__)
PDF_DIR = "/tmp/pdfs"
os.makedirs(PDF_DIR, exist_ok=True)


@app.route("/")
def home():
    return "Telegram PDF Bot is running!"


@app.route("/pdf/<filename>")
def get_pdf(filename):
    if not re.fullmatch(r"[a-f0-9]+\.pdf", filename):
        return "Invalid file", 404
    return send_from_directory(PDF_DIR, filename)


def extract_chat(url):
    if not re.match(
        r"^https://(chatgpt\.com|chat\.openai\.com)/share/",
        url
    ):
        raise ValueError("Please send a valid ChatGPT share link.")

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = soup.title.get_text(" ", strip=True) if soup.title else "ChatGPT Conversation"

    content = soup.get_text("\n", strip=True)

    if len(content) < 100:
        raise ValueError(
            "Conversation content could not be extracted. "
            "The shared page may require a different access method."
        )

    return title, content


def make_pdf(title, content):
    filename = uuid.uuid4().hex + ".pdf"
    path = os.path.join(PDF_DIR, filename)

    styles = getSampleStyleSheet()
    story = []

    story.append(Paragraph("ChatGPT Conversation", styles["Title"]))
    story.append(Spacer(1, 8 * mm))
    story.append(Paragraph(title[:300], styles["Heading2"]))
    story.append(Spacer(1, 5 * mm))

    for line in content.splitlines():
        line = line.strip()
        if not line:
            continue

        safe_line = (
            line.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

        story.append(Paragraph(safe_line, styles["BodyText"]))
        story.append(Spacer(1, 3 * mm))

    doc = SimpleDocTemplate(
        path,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm
    )

    doc.build(story)
    return filename


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Welcome to ChatGPT PDF Bot!\n\n"
        "Send a public ChatGPT conversation share link "
        "and I will try to convert it into a PDF."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    await update.message.reply_text("Please wait. Processing your link...")

    try:
        title, content = await asyncio.to_thread(extract_chat, url)
        filename = await asyncio.to_thread(make_pdf, title, content)

        if not BASE_URL:
            await update.message.reply_text(
                "PDF created, but public hosting is not configured yet."
            )
            return

        pdf_url = f"{BASE_URL}/pdf/{filename}"

        await update.message.reply_text(
            "Your PDF is ready!\n\n"
            f"Open PDF:\n{pdf_url}"
        )

    except Exception as error:
        await update.message.reply_text(
            f"Sorry, conversion failed:\n{str(error)[:500]}"
        )


def run_web():
    app.run(host="0.0.0.0", port=PORT, use_reloader=False)


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN is missing.")

    threading.Thread(target=run_web, daemon=True).start()

    bot = Application.builder().token(TOKEN).build()
    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link)
    )

    bot.run_polling()


if __name__ == "__main__":
    import asyncio
    main()
  
