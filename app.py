
import os
import re
import uuid
import logging
import threading
from pathlib import Path
from urllib.parse import quote

from flask import Flask, send_file, jsonify, abort
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

BASE_URL = (
    os.environ.get("BASE_URL")
    or os.environ.get("RENDER_EXTERNAL_URL")
    or ""
).strip().rstrip("/")

PDF_STORAGE_DIR = Path(
    os.environ.get("PDF_STORAGE_DIR", "/tmp/pdfs")
)

MAX_FILE_SIZE = 20 * 1024 * 1024  # 20 MB

PORT = int(os.environ.get("PORT", "10000"))

PDF_STORAGE_DIR.mkdir(parents=True, exist_ok=True)

# ==================================================
# LOGGING
# ==================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

# ==================================================
# FLASK APP
# ==================================================

app = Flask(__name__)

# ==================================================
# HELPER FUNCTIONS
# ==================================================

def safe_filename(filename):
    filename = os.path.basename(filename or "document.pdf")
    filename = re.sub(r"[^a-zA-Z0-9._-]", "_", filename)

    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    return filename[:150]


def get_pdf_path(file_id):
    try:
        file_id = str(uuid.UUID(file_id))
    except (ValueError, TypeError, AttributeError):
        abort(404)

    path = PDF_STORAGE_DIR / f"{file_id}.pdf"

    if not path.is_file():
        abort(404)

    return path


# ==================================================
# HOME ROUTE
# ==================================================

@app.route("/")
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>PDF Link Converter</title>
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <style>
            body {
                font-family: Arial, sans-serif;
                background: #f4f6f8;
                text-align: center;
                padding: 40px 15px;
            }
            .box {
                background: white;
                max-width: 450px;
                margin: auto;
                padding: 30px;
                border-radius: 15px;
                box-shadow: 0 3px 15px #0001;
            }
            h1 { color: #1685d5; }
            p { color: #555; line-height: 1.6; }
        </style>
    </head>
    <body>
        <div class="box">
            <h1>PDF Link Converter</h1>
            <p>Send a PDF file to our Telegram bot.</p>
            <p>You will receive two links:</p>
            <p><b>1. View PDF in browser</b></p>
            <p><b>2. Download PDF</b></p>
        </div>
    </body>
    </html>
    """


# ==================================================
# HEALTH CHECK
# ==================================================

@app.route("/health")
def health():
    return jsonify({
        "status": "ok",
        "service": "PDF Link Converter",
    })


# ==================================================
# VIEW PDF IN BROWSER
# ==================================================

@app.route("/pdf/<file_id>")
def view_pdf(file_id):
    path = get_pdf_path(file_id)

    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=False,
        download_name=f"{file_id}.pdf",
    )


# ==================================================
# DOWNLOAD PDF
# ==================================================

@app.route("/download/<file_id>")
def download_pdf(file_id):
    path = get_pdf_path(file_id)

    return send_file(
        path,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"{file_id}.pdf",
    )


# ==================================================
# TELEGRAM START COMMAND
# ==================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "👋 Welcome to PDF Link Converter Bot!\n\n"
            "📄 Send me any PDF file.\n\n"
            "I will generate:\n"
            "🔗 PDF View Link\n"
            "⬇️ PDF Download Link\n\n"
            "Maximum PDF size: 20 MB"
        )


# ==================================================
# HELP COMMAND
# ==================================================

async def help_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "📌 How to use this bot:\n\n"
            "1. Send a PDF document.\n"
            "2. Wait for processing.\n"
            "3. Receive your PDF links.\n\n"
            "You can open the view link in a browser "
            "or use the download link."
        )


# ==================================================
# PDF FILE HANDLER
# ==================================================

async def handle_pdf(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if not message or not message.document:
        return

    document = message.document

    original_name = document.file_name or "document.pdf"

    if not original_name.lower().endswith(".pdf"):
        await message.reply_text(
            "❌ Please send a PDF file only."
        )
        return

    if document.file_size and document.file_size > MAX_FILE_SIZE:
        await message.reply_text(
            "❌ File is too large.\n"
            "Maximum allowed size is 20 MB."
        )
        return

    if not BASE_URL:
        await message.reply_text(
            "⚠️ Server URL is not configured.\n"
            "Please contact the administrator."
        )
        logger.error("BASE_URL or RENDER_EXTERNAL_URL is missing.")
        return

    file_id = str(uuid.uuid4())
    file_path = PDF_STORAGE_DIR / f"{file_id}.pdf"

    try:
        await message.reply_text("⏳ Processing your PDF...")

        telegram_file = await context.bot.get_file(document.file_id)

        await telegram_file.download_to_drive(
            custom_path=str(file_path)
        )

        if not file_path.is_file():
            raise RuntimeError("Downloaded file not found.")

        if file_path.stat().st_size > MAX_FILE_SIZE:
            file_path.unlink(missing_ok=True)
            await message.reply_text(
                "❌ File is larger than the allowed 20 MB."
            )
            return

        # Verify that the file is actually a PDF.
        with open(file_path, "rb") as pdf_file:
            signature = pdf_file.read(5)

        if signature != b"%PDF-":
            file_path.unlink(missing_ok=True)
            await message.reply_text(
                "❌ Invalid PDF file. Please send a valid PDF."
            )
            return

        view_link = f"{BASE_URL}/pdf/{file_id}"
        download_link = f"{BASE_URL}/download/{file_id}"

        safe_name = safe_filename(original_name)

        await message.reply_text(
            "✅ PDF converted successfully!\n\n"
            f"📄 File: {safe_name}\n\n"
            "🌐 View PDF:\n"
            f"{view_link}\n\n"
            "⬇️ Download PDF:\n"
            f"{download_link}\n\n"
            "🔗 Both links are publicly accessible."
        )

        logger.info("PDF saved successfully: %s", file_id)

    except Exception:
        logger.exception("PDF processing failed.")

        if file_path.exists():
            file_path.unlink(missing_ok=True)

        await message.reply_text(
            "❌ Something went wrong while processing your PDF.\n"
            "Please try again."
        )


# ==================================================
# OTHER FILE HANDLER
# ==================================================

async def unsupported_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message

    if message:
        await message.reply_text(
            "❌ Please send a PDF document only."
        )


# ==================================================
# FLASK SERVER THREAD
# ==================================================

def run_flask():
    app.run(
        host="0.0.0.0",
        port=PORT,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


# ==================================================
# MAIN FUNCTION
# ==================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN is missing. Add it in Render Environment."
        )

    if not BASE_URL:
        logger.warning(
            "BASE_URL is missing. Set RENDER_EXTERNAL_URL "
            "or BASE_URL in Render Environment."
        )

    # Start Flask web server.
    flask_thread = threading.Thread(
        target=run_flask,
        daemon=True,
    )
    flask_thread.start()

    logger.info("Flask server started on port %s", PORT)

    # Start Telegram bot.
    telegram_app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .build()
    )

    telegram_app.add_handler(
        CommandHandler("start", start_command)
    )

    telegram_app.add_handler(
        CommandHandler("help", help_command)
    )

    telegram_app.add_handler(
        MessageHandler(
            filters.Document.ALL,
            handle_pdf,
        )
    )

    logger.info("Telegram bot is starting...")

    telegram_app.run_polling(
        drop_pending_updates=True
    )


# ==================================================
# RUN
# ==================================================

if __name__ == "__main__":
    main()
    
