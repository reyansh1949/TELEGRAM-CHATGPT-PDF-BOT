import os
import re
import uuid
import threading
import logging
from pathlib import Path
from io import BytesIO
from xml.sax.saxutils import escape

import requests
import matplotlib
matplotlib.use("Agg")
from matplotlib.mathtext import math_to_image

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
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image as RLImage
)

# ==================================================
# CONFIGURATION
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing hai.")

BASE_URL = os.environ.get(
    "RENDER_EXTERNAL_URL", ""
).rstrip("/")

PORT = int(os.environ.get("PORT", 10000))

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ==================================================
# WEBSITE
# ==================================================

@app.route("/")
def home():
    return "ChatGPT PDF Bot is running!"


@app.route("/health")
def health():
    return {"status": "ok"}


@app.route("/pdf/<filename>")
def download_pdf(filename):

    if not re.fullmatch(r"[a-f0-9-]+\.pdf", filename):
        return "Invalid PDF name", 400

    return send_from_directory(
        PDF_DIR,
        filename,
        as_attachment=False,
        mimetype="application/pdf"
    )

# ==================================================
# FONT
# ==================================================

def setup_font():

    font_path = PDF_DIR / "NotoSansDevanagari-Regular.ttf"

    font_url = (
        "https://raw.githubusercontent.com/"
        "notofonts/noto-fonts/main/hinted/ttf/"
        "NotoSansDevanagari/"
        "NotoSansDevanagari-Regular.ttf"
    )

    try:

        if not font_path.exists():

            response = requests.get(
                font_url,
                timeout=40
            )

            response.raise_for_status()

            font_path.write_bytes(response.content)

        pdfmetrics.registerFont(
            TTFont("HindiFont", str(font_path))
        )

        logging.info("Hindi font loaded.")

        return "HindiFont"

    except Exception:

        logging.exception("Hindi font loading failed.")

        return "Helvetica"


FONT_NAME = setup_font()

# ==================================================
# EXTRACT ASSISTANT ANSWERS
# ==================================================

def extract_chat(url):

    match = re.search(
        r"https?://(?:www\.)?chatgpt\.com/share/([a-zA-Z0-9-]+)",
        url.strip()
    )

    if not match:
        raise ValueError("Valid ChatGPT share link bhejein.")

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
        timeout=45
    )

    response.raise_for_status()

    data = response.json()

    title = data.get("title") or "ChatGPT Notes"

    mapping = data.get("mapping") or {}

    messages = []

    # Conversation tree ko parent-child order mein traverse karo.
    root_ids = [
        node_id
        for node_id, node in mapping.items()
        if not node.get("parent")
    ]

    visited = set()

    def visit(node_id):

        if node_id in visited:
            return

        visited.add(node_id)

        node = mapping.get(node_id)

        if not node:
            return

        message = node.get("message")

        if message:

            role = message.get(
                "author", {}
            ).get("role", "")

            metadata = message.get("metadata") or {}

            message_type = metadata.get(
                "message_type", ""
            )

            if role == "assistant" and message_type not in (
                "analysis",
                "commentary",
                "summary",
                "notification"
            ):

                content = message.get("content") or {}

                parts = content.get("parts") or []

                text_parts = [
                    part.strip()
                    for part in parts
                    if isinstance(part, str) and part.strip()
                ]

                if text_parts:
                    messages.append("\n".join(text_parts))

        for child_id in node.get("children", []):
            visit(child_id)

    for root_id in root_ids:
        visit(root_id)

    if not messages:
        raise ValueError(
            "Assistant ka answer nahi mila. "
            "Share link public hona chahiye."
        )

    return title, "\n\n".join(messages)

# ==================================================
# LATEX MATH RENDERING
# ==================================================

MATH_PATTERN = re.compile(
    r"\$\$(.+?)\$\$"
    r"|\\\[(.+?)\\\]"
    r"|\\\((.+?)\\\)"
    r"|(?<!\\)\$(.+?)\$",
    re.DOTALL
)


def render_math(expression):

    expression = expression.strip()

    # Common LaTeX commands not directly supported by MathText
    replacements = {
        r"\dfrac": r"\frac",
        r"\tfrac": r"\frac",
        r"\left": "",
        r"\right": "",
        r"\cdot": r"\cdot",
        r"\times": r"\times",
    }

    for old, new in replacements.items():
        expression = expression.replace(old, new)

    buffer = BytesIO()

    math_to_image(
        "$" + expression + "$",
        buffer,
        dpi=200,
        format="png"
    )

    buffer.seek(0)

    width, height = ImageReader(buffer).getSize()

    max_width = 165 * mm
    max_height = 45 * mm

    scale = min(
        max_width / width,
        max_height / height,
        1
    )

    buffer.seek(0)

    return RLImage(
        buffer,
        width=width * scale,
        height=height * scale,
        hAlign="CENTER"
    )

# ==================================================
# MARKDOWN FORMATTING
# ==================================================

def format_markdown(text):

    text = escape(text)

    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL
    )

    text = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)",
        r"<i>\1</i>",
        text
    )

    text = re.sub(
        r"`([^`]+)`",
        r'<font name="Courier">\1</font>',
        text
    )

    return text

# ==================================================
# PDF CONTENT
# ==================================================

def add_content(story, content, styles):

    # Remove code fences but preserve their contents
    content = re.sub(
        r"```[a-zA-Z0-9_+-]*\s*",
        "",
        content
    )

    content = content.replace("```", "")

    for line in content.splitlines():

        line = line.strip()

        if not line:
            story.append(Spacer(1, 5))
            continue

        # Headings
        if line.startswith("### "):
            story.append(
                Paragraph(
                    format_markdown(line[4:]),
                    styles["Heading3Custom"]
                )
            )
            continue

        if line.startswith("## "):
            story.append(
                Paragraph(
                    format_markdown(line[3:]),
                    styles["Heading2Custom"]
                )
            )
            continue

        if line.startswith("# "):
            story.append(
                Paragraph(
                    format_markdown(line[2:]),
                    styles["Heading1Custom"]
                )
            )
            continue

        if line in ("---", "***", "___"):
            story.append(Spacer(1, 8))
            continue

        # Detect math inside a line
        matches = list(MATH_PATTERN.finditer(line))

        if matches:

            position = 0

            for match in matches:

                before = line[position:match.start()].strip()

                if before:
                    story.append(
                        Paragraph(
                            format_markdown(before),
                            styles["BodyCustom"]
                        )
                    )

                expression = next(
                    item for item in match.groups()
                    if item is not None
                )

                try:
                    story.append(render_math(expression))
                    story.append(Spacer(1, 5))

                except Exception:
                    logging.exception(
                        "Math rendering failed: %s",
                        expression
                    )

                    story.append(
                        Paragraph(
                            escape(match.group(0)),
                            styles["BodyCustom"]
                        )
                    )

                position = match.end()

            remaining = line[position:].strip()

            if remaining:
                story.append(
                    Paragraph(
                        format_markdown(remaining),
                        styles["BodyCustom"]
                    )
                )

            story.append(Spacer(1, 5))
            continue

        # Bullet list
        if line.startswith("- ") or line.startswith("* "):

            story.append(
                Paragraph(
                    "&#8226;&nbsp;&nbsp;" +
                    format_markdown(line[2:]),
                    styles["BodyCustom"]
                )
            )

            continue

        # Numbered list
        numbered = re.match(
            r"^(\d+)\.\s+(.*)$",
            line
        )

        if numbered:

            story.append(
                Paragraph(
                    "<b>" + numbered.group(1) +
                    ".</b>&nbsp;" +
                    format_markdown(numbered.group(2)),
                    styles["BodyCustom"]
                )
            )

            continue

        story.append(
            Paragraph(
                format_markdown(line),
                styles["BodyCustom"]
            )
        )

        story.append(Spacer(1, 4))

# ==================================================
# CREATE PDF
# ==================================================

def make_pdf(title, content):

    filename = str(uuid.uuid4()) + ".pdf"

    filepath = PDF_DIR / filename

    styles = getSampleStyleSheet()

    styles.add(
        ParagraphStyle(
            "TitleCustom",
            parent=styles["Title"],
            fontName=FONT_NAME,
            fontSize=19,
            leading=26,
            alignment=TA_CENTER,
            spaceAfter=18
        )
    )

    for name, size in (
        ("Heading1Custom", 17),
        ("Heading2Custom", 14),
        ("Heading3Custom", 12)
    ):

        styles.add(
            ParagraphStyle(
                name,
                parent=styles["Heading2"],
                fontName=FONT_NAME,
                fontSize=size,
                leading=size + 6,
                spaceBefore=10,
                spaceAfter=7
            )
        )

    styles.add(
        ParagraphStyle(
            "BodyCustom",
            parent=styles["Normal"],
            fontName=FONT_NAME,
            fontSize=11,
            leading=17,
            spaceAfter=6,
            wordWrap="CJK"
        )
    )

    document = SimpleDocTemplate(
        str(filepath),
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=18 * mm,
        bottomMargin=18 * mm,
        title=title
    )

    story = [
        Paragraph(
            format_markdown(title),
            styles["TitleCustom"]
        ),
        Spacer(1, 12)
    ]

    add_content(story, content, styles)

    document.build(story)

    return filename

# ==================================================
# TELEGRAM BOT
# ==================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Welcome to ChatGPT PDF Bot!\n\n"
        "ChatGPT public share link bhejein.\n"
        "Main Assistant ke answers ko PDF mein convert karunga."
    )


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):

    url = (update.message.text or "").strip()

    if "chatgpt.com/share/" not in url:

        await update.message.reply_text(
            "Kripya valid ChatGPT public share link bhejein."
        )

        return

    await update.message.reply_text(
        "PDF ban rahi hai. Kripya wait karein..."
    )

    try:

        title, content = extract_chat(url)

        filename = make_pdf(title, content)

        if not BASE_URL:
            raise RuntimeError(
                "Render external URL missing hai."
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

# ==================================================
# RUN WEBSITE
# ==================================================

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

    bot = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    bot.add_handler(
        CommandHandler("start", start)
    )

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
    
