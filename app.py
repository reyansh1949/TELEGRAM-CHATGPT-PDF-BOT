import os
import re
import uuid
import asyncio
import threading
import requests

from flask import Flask, send_from_directory
from bs4 import BeautifulSoup
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
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

app = Flask(__name__)
PDF_DIR = "/tmp/pdfs"
os.makedirs(PDF_DIR, exist_ok=True)


def setup_font():
    font_path = "/tmp/NotoSansDevanagari.ttf"

    if not os.path.exists(font_path):
        url = (
            "https://raw.githubusercontent.com/google/fonts/"
            "main/ofl/notosansdevanagari/"
            "NotoSansDevanagari%5Bwght%5D.ttf"
        )
        r = requests.get(url, timeout=40)
        r.raise_for_status()

        with open(font_path, "wb") as f:
            f.write(r.content)

    pdfmetrics.registerFont(
        TTFont("HindiFont", font_path)
    )


@app.route("/")
def home():
    return "Telegram PDF Bot is running!"


@app.route("/pdf/<filename>")
def get_pdf(filename):
    if not re.fullmatch(r"[a-f0-9]+\.pdf", filename):
        return "Invalid file", 404

    return send_from_directory(
        PDF_DIR, filename, as_attachment=False
    )


def extract_chat(url):
    if not re.match(
        r"^https://(chatgpt\.com|chat\.openai\.com)/share/",
        url
    ):
        raise ValueError("Valid ChatGPT share link bhejein.")

    response = requests.get(
        url,
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=40
    )
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    title = (
        soup.title.get_text(" ", strip=True)
        if soup.title else "ChatGPT Notes"
    )

    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    content = soup.body.get_text("\n", strip=True)

    if len(content) < 100:
        raise ValueError(
            "Conversation content nahi mila. "
            "Share link publicly accessible hona chahiye."
        )

    return title, content


def format_math(text):
    replacements = {
        r"\pi": "π",
        r"\theta": "θ",
        r"\alpha": "α",
        r"\beta": "β",
        r"\infty": "∞",
        r"\sqrt": "√",
        r"\times": "×",
        r"\cdot": "·",
        r"\pm": "±",
        r"\leq": "≤",
        r"\geq": "≥",
        r"\neq": "≠",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    text = re.sub(r"\^\{([^}]+)\}", r"<super>\1</super>", text)
    text = re.sub(r"_\{([^}]+)\}", r"<sub>\1</sub>", text)

    return text


def make_pdf(title, content):
    setup_font()

    filename = uuid.uuid4().hex + ".pdf"
    path = os.path.join(PDF_DIR, filename)

    styles = getSampleStyleSheet()
    styles["Normal"].fontName = "HindiFont"
    styles["Normal"].fontSize = 11
    styles["Normal"].leading = 17

    styles["Title"].fontName = "HindiFont"
    styles["Heading2"].fontName = "HindiFont"

    story = [
        Paragraph("ChatGPT Study Notes", styles["Title"]),
        Spacer(1, 8 * mm),
        Paragraph(title[:300], styles["Heading2"]),
        Spacer(1, 5 * mm)
    ]

    for line in content.splitlines():
        line = line.strip()

        if not line:
            continue

        line = format_math(line)

        line = (
            line.replace("&", "&amp;")
            .replace("<super>", "<<<SUPER>>>")
            .replace("</super>", "<<<ENDSUPER>>>")
            .replace("<sub>", "<<<SUB>>>")
            .replace("</sub>", "<<<ENDSUB>>>")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace("<<<SUPER>>>", "<super>")
            .replace("<<<ENDSUPER>>>", "</super>")
            .replace("<<<SUB>>>", "<sub>")
            .replace("<<<ENDSUB>>>", "</sub>")
        )

        story.append(Paragraph(line, styles["Normal"]))
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
        "ChatGPT public share link bhejein. "
        "Main PDF banakar link bhejunga."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()

    message = await update.message.reply_text(
        "PDF ban rahi hai. Please wait..."
    )

    try:
        title, content = await asyncio.to_thread(
            extract_chat, url
        )

        filename = await asyncio.to_thread(
            make_pdf, title, content
        )

        if not BASE_URL:
            raise ValueError("Public hosting URL configured nahi hai.")

        pdf_url = f"{BASE_URL}/pdf/{filename}"

        await message.edit_text(
            "PDF ready hai!\n\n"
            f"TV ya mobile mein kholein:\n{pdf_url}"
        )

    except Exception as error:
        await message.edit_text(
            f"Conversion failed:\n{str(error)[:400]}"
        )


def run_web():
    app.run(
        host="0.0.0.0",
        port=PORT,
        use_reloader=False
    )


def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN missing!")

    threading.Thread(
        target=run_web,
        daemon=True
    ).start()

    bot = Application.builder().token(TOKEN).build()

    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_link
        )
    )

    bot.run_polling()


if __name__ == "__main__":
    main()
    
