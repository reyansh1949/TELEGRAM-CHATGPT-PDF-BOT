import os
import re
import uuid
import threading
from pathlib import Path
from urllib.parse import urlparse

import requests
import matplotlib
matplotlib.use("Agg")
from matplotlib.mathtext import math_to_image

from flask import Flask, send_from_directory, abort
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as RLImage,
    PageBreak
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import simpleSplit
from io import BytesIO

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIGURATION ----------------

BOT_TOKEN = os.environ.get("BOT_TOKEN")
BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ---------------- HINDI FONT ----------------

FONT_NAME = "Helvetica"
BOLD_FONT_NAME = "Helvetica-Bold"

font_path = Path("NotoSansDevanagari-Regular.ttf")
bold_font_path = Path("NotoSansDevanagari-Bold.ttf")

if font_path.exists():
    pdfmetrics.registerFont(TTFont("HindiFont", str(font_path)))
    FONT_NAME = "HindiFont"

if bold_font_path.exists():
    pdfmetrics.registerFont(TTFont("HindiFont-Bold", str(bold_font_path)))
    BOLD_FONT_NAME = "HindiFont-Bold"

# ---------------- SHARE LINK ----------------

def get_share_id(url):
    parsed = urlparse(url.strip())

    if parsed.netloc not in ("chatgpt.com", "www.chatgpt.com"):
        raise ValueError("Please send a valid ChatGPT share link.")

    match = re.search(r"/share/([^/?#]+)", parsed.path)

    if not match:
        raise ValueError("Share link में conversation ID नहीं मिली।")

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

    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]

    if not isinstance(data, dict) or not isinstance(data.get("mapping"), dict):
        raise ValueError(
            "ChatGPT से conversation data नहीं मिला। "
            "हो सकता है share link private हो या API बदल गई हो।"
        )

    return data


# ---------------- CONVERSATION EXTRACTION ----------------

def get_active_path(data):
    mapping = data["mapping"]
    current_node = data.get("current_node")

    # Current selected conversation branch
    if current_node in mapping:
        path = []
        visited = set()
        node_id = current_node

        while node_id and node_id in mapping and node_id not in visited:
            visited.add(node_id)
            node = mapping[node_id]
            path.append(node)
            node_id = node.get("parent")

        path.reverse()
        return path

    # Fallback: select the deepest/latest leaf
    parents = {}
    children = set()

    for node_id, node in mapping.items():
        parent = node.get("parent")
        parents[node_id] = parent

        for child in node.get("children") or []:
            children.add(child)

    leaves = [
        node_id for node_id in mapping
        if node_id not in children
    ]

    if not leaves:
        leaves = list(mapping.keys())

    def get_path(node_id):
        result = []
        visited = set()

        while node_id and node_id in mapping and node_id not in visited:
            visited.add(node_id)
            result.append(mapping[node_id])
            node_id = parents.get(node_id)

        result.reverse()
        return result

    candidates = [get_path(node_id) for node_id in leaves]

    return max(
        candidates,
        key=lambda p: (
            len(p),
            float(
                (p[-1].get("message") or {}).get("create_time") or 0
            ) if p else 0
        ),
    )


def extract_message_text(content):
    if not isinstance(content, dict):
        return ""

    parts = content.get("parts")

    if parts is None:
        text_value = content.get("text")
        return text_value if isinstance(text_value, str) else ""

    output = []

    for part in parts:
        if isinstance(part, str):
            output.append(part)

        elif isinstance(part, dict):
            if isinstance(part.get("text"), str):
                output.append(part["text"])
            elif part.get("content_type") == "image":
                output.append("[Image attached]")
            elif part.get("content_type") in ("audio", "video"):
                output.append("[Media attached]")

    return "\n".join(output).strip()


def extract_chat(data):
    messages = []
    path = get_active_path(data)

    for node in path:
        message = node.get("message")

        if not isinstance(message, dict):
            continue

        author = message.get("author") or {}
        role = author.get("role")

        if role not in ("user", "assistant"):
            continue

        metadata = message.get("metadata") or {}

        # Hide internal analysis and tool calls.
        if role == "assistant":
            message_type = metadata.get("message_type")

            if message_type in ("analysis", "commentary", "notification"):
                continue

            recipient = message.get("recipient")
            if recipient and recipient != "all":
                continue

        content = message.get("content") or {}
        text = extract_message_text(content)

        if not text:
            continue

        messages.append({
            "role": role,
            "text": text,
        })

    if not messages:
        raise ValueError("इस share link में पढ़ने योग्य messages नहीं मिले।")

    return messages


# ---------------- FORMULA RENDERING ----------------

def render_formula(formula):
    formula = formula.strip()

    # Common LaTeX commands supported by Matplotlib MathText
    formula = formula.replace(r"\displaystyle", "")
    formula = formula.replace(r"\left", "")
    formula = formula.replace(r"\right", "")

    buffer = BytesIO()

    try:
        math_to_image(
            "$" + formula + "$",
            buffer,
            dpi=180,
            format="png",
        )
        buffer.seek(0)

        image = RLImage(buffer)
        max_width = 165 * mm

        if image.drawWidth > max_width:
            scale = max_width / image.drawWidth
            image.drawWidth *= scale
            image.drawHeight *= scale

        return image

    except Exception:
        return None


def add_text_and_formulas(story, text, styles):
    lines = text.splitlines()

    block_formula_pattern = re.compile(
        r"^\s*(?:\$\$(.*?)\$\$|\\\[(.*?)\\\])\s*$"
    )

    inline_formula_pattern = re.compile(
        r"\\\((.+?)\\\)|(?<!\\)\$(.+?)(?<!\\)\$"
    )

    for line in lines:
        stripped = line.strip()

        if not stripped:
            story.append(Spacer(1, 4))
            continue

        block_match = block_formula_pattern.match(stripped)

        if block_match:
            formula = block_match.group(1) or block_match.group(2)
            image = render_formula(formula)

            if image:
                story.append(Spacer(1, 5))
                story.append(image)
                story.append(Spacer(1, 5))
            else:
                story.append(Paragraph(
                    escape_text(formula),
                    styles["body"]
                ))
            continue

        # Basic Markdown formatting
        segments = inline_formula_pattern.split(line)

        for index, segment in enumerate(segments):
            if segment is None or segment == "":
                continue

            # Formula groups are at odd indexes
            if index % 3 in (1, 2):
                image = render_formula(segment)
                if image:
                    story.append(image)
                else:
                    story.append(Paragraph(
                        escape_text(segment),
                        styles["body"]
                    ))
            else:
                formatted = format_markdown(segment)
                story.append(Paragraph(formatted, styles["body"]))


def escape_text(text):
    text = str(text)
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    return text


def format_markdown(text):
    text = escape_text(text)

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
        r"<font name='Courier'>\1</font>",
        text
    )

    return text


# ---------------- PDF GENERATION ----------------

def make_pdf(title, messages, filename):
    file_path = PDF_DIR / filename

    doc = SimpleDocTemplate(
        str(file_path),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="ChatGPT Conversation PDF Bot",
    )

    styles = getSampleStyleSheet()

    styles.add(ParagraphStyle(
        name="PDFTitle",
        fontName=BOLD_FONT_NAME,
        fontSize=18,
        leading=25,
        alignment=1,
        textColor=colors.HexColor("#174EA6"),
        spaceAfter=18,
    ))

    styles.add(ParagraphStyle(
        name="MessageHeading",
        fontName=BOLD_FONT_NAME,
        fontSize=12,
        leading=18,
        textColor=colors.HexColor("#174EA6"),
        spaceBefore=12,
        spaceAfter=6,
    ))

    styles.add(ParagraphStyle(
        name="body",
        fontName=FONT_NAME,
        fontSize=10.5,
        leading=17,
        spaceAfter=5,
        wordWrap="CJK",
    ))

    story = []

    story.append(Paragraph(
        escape_text(title or "ChatGPT Conversation"),
        styles["PDFTitle"]
    ))

    story.append(Spacer(1, 8))

    for message in messages:
        role = message["role"]
        heading = "User" if role == "user" else "ChatGPT"

        story.append(Paragraph(
            heading,
            styles["MessageHeading"]
        ))

        add_text_and_formulas(
            story,
            message["text"],
            styles
        )

        story.append(Spacer(1, 8))

    doc.build(story)

    return file_path


# ---------------- FLASK PDF ROUTE ----------------

@app.route("/")
def home():
    return "ChatGPT PDF Bot is running."


@app.route("/pdf/<filename>")
def serve_pdf(filename):
    if Path(filename).name != filename:
        abort(404)

    file_path = PDF_DIR / filename

    if not file_path.exists():
        abort(404)

    return send_from_directory(
        str(PDF_DIR),
        filename,
        mimetype="application/pdf",
        as_attachment=False,
    )


# ---------------- TELEGRAM BOT ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "नमस्ते! 👋\n\n"
        "मुझे ChatGPT conversation का Share Link भेजें।\n"
        "मैं उसे PDF में बदलकर आपको PDF का public link दूँगा।"
    )


async def handle_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    url = update.message.text.strip()

    if "chatgpt.com/share/" not in url:
        await update.message.reply_text(
            "कृपया ChatGPT का सही Share Link भेजें।"
        )
        return

    status = await update.message.reply_text(
        "⏳ Conversation पढ़ी जा रही है और PDF बन रही है..."
    )

    try:
        data = fetch_conversation(url)
        messages = extract_chat(data)

        title = data.get("title") or "ChatGPT Conversation"
        filename = f"{uuid.uuid4()}.pdf"

        make_pdf(title, messages, filename)

        if not BASE_URL:
            raise ValueError(
                "BASE_URL या RENDER_EXTERNAL_URL सेट नहीं है।"
            )

        pdf_url = f"{BASE_URL}/pdf/{filename}"

        await status.edit_text(
            "✅ PDF तैयार है!\n\n"
            f"📄 Messages: {len(messages)}\n\n"
            f"🔗 PDF Link:\n{pdf_url}"
        )

    except requests.exceptions.RequestException:
        await status.edit_text(
            "❌ ChatGPT share link से data नहीं मिल पाया। "
            "Link public है या नहीं, जाँचें।"
        )

    except Exception as error:
        print("PDF ERROR:", repr(error))
        await status.edit_text(
            "❌ PDF बनाने में समस्या आई। "
            "कृपया दूसरा share link भेजकर कोशिश करें।"
        )


def run_flask():
    port = int(os.environ.get("PORT", 10000))
    app.run(
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )


# ---------------- START ----------------

if __name__ == "__main__":
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN environment variable missing.")

    threading.Thread(
        target=run_flask,
        daemon=True
    ).start()

    bot = ApplicationBuilder().token(BOT_TOKEN).build()

    bot.add_handler(CommandHandler("start", start))
    bot.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    print("Telegram bot started.")
    bot.run_polling()
    
