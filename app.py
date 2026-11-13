import os
import re
import uuid
import threading
import logging
from pathlib import Path
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
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image as RLImage,
    PageBreak
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
    raise RuntimeError("BOT_TOKEN Render Environment Variables mein missing hai.")

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", 10000))

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

# ==================================================
# WEBSITE ROUTES
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
# HINDI FONT
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

        logging.info("Hindi font loaded successfully.")

        return "HindiFont"

    except Exception:

        logging.exception("Hindi font loading failed.")

        return "Helvetica"


FONT_NAME = setup_font()


# ==================================================
# EXTRACT CHATGPT SHARE CONVERSATION
# ==================================================

def extract_chat(url):

    match = re.search(
        r"https?://(?:www\.)?chatgpt\.com/share/([a-zA-Z0-9-]+)",
        url.strip()
    )

    if not match:
        raise ValueError(
            "Valid ChatGPT public share link bhejein."
        )

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

    for node in mapping.values():

        message = node.get("message")

        if not message:
            continue

        role = message.get("author", {}).get("role", "")

        # Only Assistant responses
        if role != "assistant":
            continue

        metadata = message.get("metadata") or {}

        message_type = metadata.get("message_type", "")

        # Skip internal/non-final message types
        if message_type in (
            "analysis",
            "commentary",
            "summary",
            "notification"
        ):
            continue

        content = message.get("content") or {}

        parts = content.get("parts") or []

        text_parts = []

        for part in parts:

            if isinstance(part, str) and part.strip():
                text_parts.append(part.strip())

        if text_parts:

            messages.append(
                "\n".join(text_parts)
            )

    if not messages:

        raise ValueError(
            "Assistant ka answer nahi mila. "
            "Share link public hona chahiye."
        )

    return title, "\n\n".join(messages)


# ==================================================
# MATHEMATICAL FORMULA DETECTION
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

    image_path = PDF_DIR / (
        "math_" + str(uuid.uuid4()) + ".png"
    )

    # Matplotlib MathText rendering
    math_to_image(
        "$" + expression + "$",
        str(image_path),
        dpi=200,
        format="png"
    )

    image_reader = ImageReader(str(image_path))

    width, height = image_reader.getSize()

    max_width = 165 * mm
    max_height = 45 * mm

    scale = min(
        max_width / width,
        max_height / height,
        1
    )

    return RLImage(
        str(image_path),
        width=width * scale,
        height=height * scale,
        hAlign="CENTER"
    )


# ==================================================
# MARKDOWN TEXT FORMATTING
# ==================================================

def format_markdown(text):

    text = escape(text)

    # Bold
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text,
        flags=re.DOTALL
    )

    # Italic
    text = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)",
        r"<i>\1</i>",
        text
    )

    # Inline code
    text = re.sub(
        r"`([^`]+)`",
        r'<font name="Courier">\1</font>',
        text
    )

    return text


# ==================================================
# PDF TEXT AND MATH PROCESSING
# ==================================================

def add_content_to_pdf(story, content, styles):

    body_style = styles["body"]
    heading_style = styles["heading"]
    small_style = styles["small"]

    # Remove Markdown code fence markers
    content = re.sub(
        r"```[a-zA-Z0-9_+-]*\s*",
        "",
        content
    )

    content = content.replace("```", "")

    lines = content.splitlines()

    for line in lines:

        line = line.strip()

        if not line:

            story.append(Spacer(1, 5))

            continue

        # Headings
        if line.startswith("### "):

            heading = format_markdown(line[4:])

            story.append(
                Paragraph(heading, heading_style)
            )

            continue

        if line.startswith("## "):

            heading = format_markdown(line[3:])

            story.append(
                Paragraph(heading, heading_style)
            )

            continue

        if line.startswith("# "):

            heading = format_markdown(line[2:])

            story.append(
                Paragraph(heading, heading_style)
            )

            continue

        # Horizontal separator
        if line in ("---", "***", "___"):

            story.append(Spacer(1, 8))

            continue

        # Formula detection
        matches = list(MATH_PATTERN.finditer(line))

        if matches:

            last_position = 0

            for match in matches:

                before = line[last_position:match.start()].strip()

                if before:

                    story.append(
                        Paragraph(
                            format_markdown(before),
                            body_style
                        )
                    )

                expression = next(
                    group
                    for group in match.groups()
                    if group is not None
                )

                try:

                    formula_image = render_math(expression)

                    story.append(formula_image)

                    story.append(Spacer(1, 5))

                except Exception:

                    logging.warning(
                        "Math rendering failed: %s",
                        expression
                    )

                    story.append(
                        Paragraph(
                            escape(match.group(0)),
                            small_style
                        )
                    )

                last_position = match.end()

            remaining = line[last_position:].strip()

            if remaining:

                story.append(
                    Paragraph(
                        format_markdown(remaining),
                        body_style
                    )
                )

            story.append(Spacer(1, 5))

            continue

        # Bullet points
        if line.startswith("- ") or line.startswith("* "):

            bullet_text = format_markdown(line[2:])

            story.append(
                Paragraph(
                    "&#8226;&nbsp;&nbsp;" + bullet_text,
                    body_style
                )
            )

            continue

        # Numbered list
        numbered = re.match(r"^(\d+)\.\s+(.*)$", line)

        if numbered:

            number = numbered.group(1)

            text = format_markdown(numbered.group(2))

            story.append(
                Paragraph(
                    f"<b>{number}.</b>&nbsp;{text}",
                    body_style
                )
            )

            continue

        # Normal text
        story.append(
            Paragraph(
                format_markdown(line),
                body_style
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
            name="CustomTitle",
            parent=styles["Title"],
            fontName=FONT_NAME,
            fontSize=19,
            leading=26,
            alignment=TA_CENTER,
            spaceAfter=18
        )
    )

    styles.add(
        ParagraphStyle(
            name="body",
            parent=styles["Normal"],
            fontName=FONT_NAME,
            fontSize=11,
            leading=17,
            alignment=TA_LEFT,
            spaceAfter=6,
            wordWrap="CJK"
        )
    )

    styles.add(
        ParagraphStyle(
            name="heading",
            parent=styles["Heading2"],
            fontName=FONT_NAME,
            fontSize=14,
            leading=20,
            spaceBefore=10,
            spaceAfter=8
        )
    )

    styles.add(
        ParagraphStyle(
            name="small",
            parent=styles["Normal"],
            fontName=FONT_NAME,
            fontSize=9,
            leading=13
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

    story = []

    story.append(
        Paragraph(
            format_markdown(title),
            styles["CustomTitle"]
        )
    )

    story.append(Spacer(1, 12))

    add_content_to_pdf(
        story,
        content,
        styles
    )

    document.build(story)

    return filename


# ==================================================
# TELEGRAM COMMANDS
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
        "Aapki PDF ban rahi hai. Kripya wait karein..."
    )

    try:

        title, content = extract_chat(url)

        filename = make_pdf(
            title,
            content
        )

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
            "PDF conversion failed:\n"
            + str(error)[:700]
        )


# ==================================================
# RUN FLASK WEBSITE
# ==================================================

def run_website():

    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False
    )


# ==================================================
# MAIN
# ==================================================

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
    
