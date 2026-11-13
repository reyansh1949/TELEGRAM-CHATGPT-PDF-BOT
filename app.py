
import os
import re
import uuid
import html
import threading
from pathlib import Path
from urllib.parse import urlparse
from io import BytesIO

import requests
from flask import Flask, send_from_directory, abort
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer,
    Table, TableStyle, Image
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader

# ---------------- CONFIG ----------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ---------------- FONT ----------------

FONT = "Helvetica"
BOLD_FONT = "Helvetica-Bold"

regular_font = Path("NotoSansDevanagari-Regular.ttf")
bold_font = Path("NotoSansDevanagari-Bold.ttf")

if regular_font.exists():
    pdfmetrics.registerFont(
        TTFont("HindiFont", str(regular_font))
    )
    FONT = "HindiFont"

if bold_font.exists():
    pdfmetrics.registerFont(
        TTFont("HindiFontBold", str(bold_font))
    )
    BOLD_FONT = "HindiFontBold"

# ---------------- CHATGPT SHARE API ----------------

def get_share_id(url):
    parsed = urlparse(url.strip())

    if parsed.netloc not in ("chatgpt.com", "www.chatgpt.com"):
        raise ValueError("Invalid ChatGPT share link")

    match = re.search(r"/share/([^/?#]+)", parsed.path)

    if not match:
        raise ValueError("Share ID not found")

    return match.group(1)


def fetch_conversation(url):
    share_id = get_share_id(url)

    api_url = (
        "https://chatgpt.com/backend-api/share/"
        + share_id
    )

    response = requests.get(
        api_url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json",
        },
        timeout=40,
    )

    response.raise_for_status()
    data = response.json()

    if isinstance(data.get("data"), dict):
        data = data["data"]

    if not isinstance(data.get("mapping"), dict):
        raise ValueError("Conversation data unavailable")

    return data


# ---------------- EXTRACT CHAT ----------------

def get_active_path(data):
    mapping = data["mapping"]
    current = data.get("current_node")

    if current in mapping:
        path = []
        visited = set()

        while current and current in mapping and current not in visited:
            visited.add(current)
            node = mapping[current]
            path.append(node)
            current = node.get("parent")

        return list(reversed(path))

    # Fallback if current_node is unavailable
    children = set()

    for node in mapping.values():
        children.update(node.get("children") or [])

    leaves = [
        node_id for node_id in mapping
        if node_id not in children
    ]

    if not leaves:
        leaves = list(mapping)

    def build_path(node_id):
        path = []
        seen = set()

        while node_id and node_id in mapping and node_id not in seen:
            seen.add(node_id)
            node = mapping[node_id]
            path.append(node)
            node_id = node.get("parent")

        return list(reversed(path))

    return max(
        (build_path(x) for x in leaves),
        key=len
    )


def extract_text(content):
    if not isinstance(content, dict):
        return ""

    parts = content.get("parts")

    if parts is None:
        return content.get("text", "")

    result = []

    for part in parts:
        if isinstance(part, str):
            result.append(part)

        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                result.append(part["text"])
            elif part.get("content_type") == "image":
                result.append("[Image attached]")
            elif part.get("content_type") in ("audio", "video"):
                result.append("[Media attached]")

    return "\n".join(result).strip()


def extract_chat(data):
    messages = []

    for node in get_active_path(data):
        message = node.get("message")

        if not isinstance(message, dict):
            continue

        role = (message.get("author") or {}).get("role")

        if role not in ("user", "assistant"):
            continue

        if role == "assistant":
            metadata = message.get("metadata") or {}

            if metadata.get("message_type") in (
                "analysis",
                "commentary",
                "notification",
            ):
                continue

            recipient = message.get("recipient")
            if recipient and recipient != "all":
                continue

        text = extract_text(message.get("content") or {})

        if text:
            messages.append({
                "role": role,
                "text": text,
            })

    if not messages:
        raise ValueError("No readable messages found")

    return messages


# ---------------- MARKDOWN ----------------

def safe_text(value):
    return html.escape(str(value))


def format_inline(text):
    text = safe_text(text)

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text
    )

    text = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)",
        r"<i>\1</i>",
        text
    )

    text = re.sub(
        r"`(.+?)`",
        r'<font name="Courier">\1</font>',
        text
    )

    return text


def make_message_paragraphs(text, style):
    output = []

    for line in text.splitlines():
        line = line.strip()

        if not line:
            output.append(Spacer(1, 4))
            continue

        # Headings
        if line.startswith("### "):
            output.append(
                Paragraph(
                    "<b>" + format_inline(line[4:]) + "</b>",
                    style
                )
            )
            continue

        if line.startswith("## "):
            output.append(
                Paragraph(
                    "<b>" + format_inline(line[3:]) + "</b>",
                    style
                )
            )
            continue

        if line.startswith("# "):
            output.append(
                Paragraph(
                    "<b>" + format_inline(line[2:]) + "</b>",
                    style
                )
            )
            continue

        # Bullet lists
        if line.startswith("- ") or line.startswith("* "):
            line = "• " + line[2:]

        output.append(
            Paragraph(format_inline(line), style)
        )

    return output


# ---------------- CHAT BUBBLES ----------------

def create_bubble(message, styles):
    is_user = message["role"] == "user"

    if is_user:
        background = colors.HexColor("#DCEBFF")
        border = colors.HexColor("#C2DAF8")
        text_color = colors.HexColor("#17365D")
        heading = "You"
        alignment = "RIGHT"
        left_padding = 12
        right_padding = 12
    else:
        background = colors.HexColor("#EEEEEE")
        border = colors.HexColor("#DDDDDD")
        text_color = colors.HexColor("#242424")
        heading = "ChatGPT"
        alignment = "LEFT"
        left_padding = 12
        right_padding = 12

    bubble_style = ParagraphStyle(
        "BubbleText",
        fontName=FONT,
        fontSize=10.5,
        leading=16,
        textColor=text_color,
        wordWrap="CJK",
        spaceAfter=4,
    )

    heading_style = ParagraphStyle(
        "BubbleHeading",
        fontName=BOLD_FONT,
        fontSize=9,
        leading=13,
        textColor=text_color,
        spaceAfter=6,
    )

    content = [
        Paragraph(heading, heading_style)
    ]

    content.extend(
        make_message_paragraphs(
            message["text"],
            bubble_style
        )
    )

    bubble = Table(
        [[content]],
        colWidths=[145 * mm],
        hAlign=alignment,
    )

    bubble.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.7, border),
        ("LEFTPADDING", (0, 0), (-1, -1), left_padding),
        ("RIGHTPADDING", (0, 0), (-1, -1), right_padding),
        ("TOPPADDING", (0, 0), (-1, -1), 9),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
        ("ROUNDEDCORNERS", [12, 12, 12, 12]),
    ]))

    return bubble


# ---------------- PDF ----------------

def make_pdf(title, messages, filename):
    path = PDF_DIR / filename

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=17 * mm,
        leftMargin=17 * mm,
        topMargin=16 * mm,
        bottomMargin=16 * mm,
        title=title,
    )

    styles = getSampleStyleSheet()

    title_style = ParagraphStyle(
        "ChatTitle",
        fontName=BOLD_FONT,
        fontSize=17,
        leading=23,
        alignment=1,
        textColor=colors.HexColor("#174EA6"),
        spaceAfter=16,
    )

    story = [
        Paragraph(safe_text(title), title_style),
        Spacer(1, 8),
    ]

    for message in messages:
        story.append(create_bubble(message, styles))
        story.append(Spacer(1, 10))

    doc.build(story)

    return path


# ---------------- FLASK ----------------

@app.route("/")
def home():
    return "ChatGPT PDF Bot is running."


@app.route("/pdf/<filename>")
def serve_pdf(filename):
    if Path(filename).name != filename:
        abort(404)

    path = PDF_DIR / filename

    if not path.is_file():
        abort(404)

    return send_from_directory(
        str(PDF_DIR),
        filename,
        mimetype="application/pdf",
        as_attachment=False,
    )


# ---------------- TELEGRAM ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "नमस्ते! 👋\n\n"
        "ChatGPT conversation का Share Link भेजें।\n"
        "मैं उसे ChatGPT-style coloured bubbles वाली PDF में बदल दूँगा।"
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    url = update.message.text.strip()

    if "chatgpt.com/share/" not in url:
        await update.message.reply_text(
            "कृपया सही ChatGPT Share Link भेजें।"
        )
        return

    status = await update.message.reply_text(
        "⏳ Chat पढ़ी जा रही है और PDF बनाई जा रही है..."
    )

    try:
        data = fetch_conversation(url)
        messages = extract_chat(data)

        title = data.get("title") or "ChatGPT Conversation"
        filename = f"{uuid.uuid4()}.pdf"

        make_pdf(title, messages, filename)

        if not BASE_URL:
            raise ValueError("BASE_URL missing")

        pdf_url = f"{BASE_URL}/pdf/{filename}"

        await status.edit_text(
            "✅ आपकी ChatGPT-style PDF तैयार है!\n\n"
            f"💬 Total messages: {len(messages)}\n\n"
            f"🔗 PDF Link:\n{pdf_url}"
        )

    except requests.exceptions.RequestException as error:
        print("REQUEST ERROR:", repr(error))
        await status.edit_text(
            "❌ ChatGPT Share Link से data नहीं मिला।"
        )

    except Exception as error:
        print("PDF ERROR:", repr(error))
        await status.edit_text(
            "❌ PDF बनाने में समस्या आई। Logs में error देखें।"
        )


# ---------------- START SERVER ----------------

def run_flask():
    port = int(os.environ.get("PORT", 10000))

    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing")

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    bot = ApplicationBuilder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start", start))

    bot.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_message
        )
    )

    print("Telegram PDF Bot Started!")
    bot.run_polling()
    
