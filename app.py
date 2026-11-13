
import os
import re
import uuid
import html
import threading
from pathlib import Path
from urllib.parse import urlparse

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
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
    KeepTogether,
    HRFlowable,
)
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas


# ==================================================
# CONFIGURATION
# ==================================================

BOT_TOKEN = os.environ.get("BOT_TOKEN")

BASE_URL = os.environ.get(
    "BASE_URL",
    os.environ.get("RENDER_EXTERNAL_URL", "")
).rstrip("/")

PDF_DIR = Path("/tmp/pdfs")
PDF_DIR.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

PAGE_WIDTH, PAGE_HEIGHT = A4


# ==================================================
# FONT SYSTEM
# ==================================================

FONT_DIR = Path("fonts")

def find_font(filename, system_paths=()):
    candidates = [
        FONT_DIR / filename,
        Path(filename),
        *[Path(p) for p in system_paths],
    ]

    for path in candidates:
        if path.is_file():
            return path

    return None


FONT_FILES = {
    "Hindi": find_font("NotoSansDevanagari-Regular.ttf"),
    "HindiBold": find_font("NotoSansDevanagari-Bold.ttf"),
    "Math": find_font(
        "DejaVuSans.ttf",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"],
    ),
    "MathBold": find_font(
        "DejaVuSans-Bold.ttf",
        ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"],
    ),
}

FONT_NAMES = {}

for name, path in FONT_FILES.items():
    if path:
        pdfmetrics.registerFont(TTFont(name, str(path)))
        FONT_NAMES[name] = name

REGULAR = (
    "Math" if "Math" in FONT_NAMES else "Helvetica"
)

BOLD = (
    "MathBold" if "MathBold" in FONT_NAMES else "Helvetica-Bold"
)

HINDI = (
    "Hindi" if "Hindi" in FONT_NAMES else REGULAR
)

HINDI_BOLD = (
    "HindiBold" if "HindiBold" in FONT_NAMES else BOLD
)

print("FONT STATUS:", FONT_NAMES)


# ==================================================
# FONT FALLBACK
# ==================================================

DEVANAGARI = re.compile(r"[\u0900-\u097F\uA8E0-\uA8FF]")

def font_fallback(markup):
    """
    Switch between Hindi and mathematical fonts
    while preserving ReportLab paragraph markup.
    """

    tokens = re.split(r"(<[^>]+>)", markup)

    result = []
    bold_mode = False
    code_mode = False

    for token in tokens:

        if not token:
            continue

        if token == "<b>":
            bold_mode = True
            result.append(token)
            continue

        if token == "</b>":
            bold_mode = False
            result.append(token)
            continue

        if 'name="Courier"' in token:
            code_mode = True
            result.append(token)
            continue

        if token == "</font>":
            code_mode = False
            result.append(token)
            continue

        if token.startswith("<") and token.endswith(">"):
            result.append(token)
            continue

        if code_mode:
            result.append(token)
            continue

        chunks = re.split(
            r"([\u0900-\u097F\uA8E0-\uA8FF]+)",
            token
        )

        for chunk in chunks:
            if not chunk:
                continue

            if DEVANAGARI.search(chunk):
                selected = HINDI_BOLD if bold_mode else HINDI
            else:
                selected = BOLD if bold_mode else REGULAR

            if selected in ("Helvetica", "Helvetica-Bold"):
                result.append(chunk)
            else:
                result.append(
                    f'<font name="{selected}">{chunk}</font>'
                )

    return "".join(result)


# ==================================================
# CHATGPT SHARE API
# ==================================================

def get_share_id(url):

    parsed = urlparse(url.strip())

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Invalid URL")

    if parsed.netloc.lower() not in (
        "chatgpt.com",
        "www.chatgpt.com",
    ):
        raise ValueError("Invalid ChatGPT share link")

    match = re.search(
        r"/share/([a-zA-Z0-9-]+)",
        parsed.path
    )

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
        timeout=45,
    )

    response.raise_for_status()

    data = response.json()

    if isinstance(data.get("data"), dict):
        data = data["data"]

    if not isinstance(data.get("mapping"), dict):
        raise ValueError(
            "Conversation data unavailable"
        )

    return data


# ==================================================
# EXTRACT CONVERSATION
# ==================================================

def get_active_path(data):

    mapping = data["mapping"]
    current = data.get("current_node")

    if current in mapping:

        path = []
        visited = set()

        while (
            current
            and current in mapping
            and current not in visited
        ):
            visited.add(current)
            node = mapping[current]
            path.append(node)
            current = node.get("parent")

        return list(reversed(path))

    children = set()

    for node in mapping.values():
        children.update(node.get("children") or [])

    leaves = [
        node_id
        for node_id in mapping
        if node_id not in children
    ]

    if not leaves:
        leaves = list(mapping)

    def build_path(node_id):

        path = []
        seen = set()

        while (
            node_id
            and node_id in mapping
            and node_id not in seen
        ):
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

            elif part.get("content_type") in (
                "audio",
                "video",
            ):
                result.append("[Media attached]")

    return "\n".join(result).strip()


def extract_chat(data):

    messages = []

    for node in get_active_path(data):

        message = node.get("message")

        if not isinstance(message, dict):
            continue

        author = message.get("author") or {}
        role = author.get("role")

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

        text = extract_text(
            message.get("content") or {}
        )

        if text:
            messages.append({
                "role": role,
                "text": text,
            })

    if not messages:
        raise ValueError("No readable messages found")

    return messages


# ==================================================
# MATHEMATICAL FORMULA CONVERTER
# ==================================================

SUPER_MAP = str.maketrans(
    "0123456789+-=()",
    "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾"
)

SUB_MAP = str.maketrans(
    "0123456789+-=()",
    "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎"
)

LATEX_SYMBOLS = {
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "delta": "δ",
    "epsilon": "ε",
    "theta": "θ",
    "lambda": "λ",
    "mu": "μ",
    "pi": "π",
    "phi": "φ",
    "varphi": "φ",
    "omega": "ω",
    "Delta": "Δ",
    "Sigma": "Σ",
    "Omega": "Ω",
    "infty": "∞",
    "times": "×",
    "cdot": "·",
    "div": "÷",
    "pm": "±",
    "mp": "∓",
    "neq": "≠",
    "leq": "≤",
    "geq": "≥",
    "approx": "≈",
    "equiv": "≡",
    "propto": "∝",
    "in": "∈",
    "notin": "∉",
    "cup": "∪",
    "cap": "∩",
    "subset": "⊂",
    "subseteq": "⊆",
    "emptyset": "∅",
    "therefore": "∴",
    "because": "∵",
    "perp": "⊥",
    "parallel": "∥",
    "rightarrow": "→",
    "leftarrow": "←",
    "Rightarrow": "⇒",
    "Leftrightarrow": "⇔",
    "degree": "°",
    "cdots": "⋯",
    "ldots": "…",
    "sum": "∑",
    "prod": "∏",
    "int": "∫",
    "partial": "∂",
    "nabla": "∇",
    "forall": "∀",
    "exists": "∃",
    "sin": "sin",
    "cos": "cos",
    "tan": "tan",
    "cot": "cot",
    "sec": "sec",
    "csc": "csc",
    "arcsin": "sin⁻¹",
    "arccos": "cos⁻¹",
    "arctan": "tan⁻¹",
    "arccot": "cot⁻¹",
    "arcsec": "sec⁻¹",
    "arccsc": "csc⁻¹",
    "log": "log",
    "ln": "ln",
    "lim": "lim",
    "max": "max",
    "min": "min",
}


def read_group(text, start):

    if start >= len(text) or text[start] != "{":
        return None, start

    depth = 0

    for i in range(start, len(text)):

        if text[i] == "{":
            depth += 1

        elif text[i] == "}":
            depth -= 1

            if depth == 0:
                return text[start + 1:i], i + 1

    return None, start


def convert_fractions(text):

    pattern = re.compile(
        r"\\(?:dfrac|tfrac|frac)\s*\{([^{}]*)\}\s*\{([^{}]*)\}"
    )

    previous = None

    while previous != text:
        previous = text

        text = pattern.sub(
            lambda m: f"({m.group(1)})⁄({m.group(2)})",
            text
        )

    return text


def latex_to_unicode(text):

    text = text.replace(r"\(", "")
    text = text.replace(r"\)", "")
    text = text.replace(r"\[", "")
    text = text.replace(r"\]", "")
    text = text.replace("$$", "")
    text = text.replace("$", "")

    text = convert_fractions(text)

    # Square roots
    text = re.sub(
        r"\\sqrt(?:\[(.*?)\])?\{([^{}]*)\}",
        lambda m: (
            (m.group(1) + "√") if m.group(1) else "√"
        ) + f"({m.group(2)})",
        text
    )

    # Superscripts
    text = re.sub(
        r"\^\{([^{}]+)\}",
        lambda m: m.group(1).translate(SUPER_MAP),
        text
    )

    text = re.sub(
        r"\^([0-9+\-=()])",
        lambda m: m.group(1).translate(SUPER_MAP),
        text
    )

    # Subscripts
    text = re.sub(
        r"_\{([^{}]+)\}",
        lambda m: m.group(1).translate(SUB_MAP),
        text
    )

    text = re.sub(
        r"_([0-9+\-=()])",
        lambda m: m.group(1).translate(SUB_MAP),
        text
    )

    # Remove simple formatting wrappers
    for command in (
        "text",
        "mathrm",
        "mathbf",
        "mathit",
        "mathsf",
        "mathbb",
        "mathcal",
        "operatorname",
        "boxed",
        "overline",
        "underline",
        "vec",
        "hat",
        "bar",
        "tilde",
    ):

        text = re.sub(
            r"\\" + command + r"\{([^{}]*)\}",
            r"\1",
            text
        )

    text = re.sub(
        r"\\begin\{[^{}]*\}|\\end\{[^{}]*\}",
        "",
        text
    )

    text = re.sub(
        r"\\([A-Za-z]+)",
        lambda m: LATEX_SYMBOLS.get(
            m.group(1),
            m.group(1)
        ),
        text
    )

    replacements = {
        r"\{": "{",
        r"\}": "}",
        r"\,": " ",
        r"\;": " ",
        r"\:": " ",
        r"\!": "",
        r"\%": "%",
        r"\#": "#",
        r"\$": "$",
        r"\&": "&",
        r"\_": "_",
    }

    for old, new in replacements.items():
        text = text.replace(old, new)

    return text


# ==================================================
# MARKDOWN AND PARAGRAPH FORMATTING
# ==================================================

def format_inline(text):

    text = latex_to_unicode(text)
    text = html.escape(text)

    # Inline code
    text = re.sub(
        r"`([^`]+)`",
        r'<font name="Courier">\1</font>',
        text
    )

    # Bold
    text = re.sub(
        r"\*\*(.+?)\*\*",
        r"<b>\1</b>",
        text
    )

    # Italic
    text = re.sub(
        r"(?<!\*)\*([^*]+)\*(?!\*)",
        r"<i>\1</i>",
        text
    )

    # Strikethrough
    text = re.sub(
        r"~~(.+?)~~",
        r"<strike>\1</strike>",
        text
    )

    return font_fallback(text)


def split_long_line(line, limit=450):

    result = []

    while len(line) > limit:

        pos = line.rfind(" ", 0, limit)

        if pos <= 0:
            pos = limit

        result.append(line[:pos])
        line = line[pos:].lstrip()

    if line:
        result.append(line)

    return result


def make_message_paragraphs(text, styles):

    output = []
    code_mode = False
    code_lines = []

    for raw_line in text.splitlines():

        line = raw_line.rstrip()

        if line.strip().startswith("```"):

            if not code_mode:
                code_mode = True
                code_lines = []
            else:
                code_mode = False

                code_text = "<br/>".join(
                    html.escape(x) for x in code_lines
                )

                output.append(
                    Paragraph(
                        f'<font name="Courier">{code_text}</font>',
                        styles["code"]
                    )
                )

            continue

        if code_mode:
            code_lines.append(line)
            continue

        line = line.strip()

        if not line:
            output.append(Spacer(1, 4))
            continue

        if line in ("---", "***", "___"):
            output.append(
                HRFlowable(
                    width="100%",
                    thickness=0.6,
                    color=colors.HexColor("#BBBBBB"),
                    spaceBefore=5,
                    spaceAfter=7,
                )
            )
            continue

        heading_match = re.match(r"^(#{1,6})\s+(.*)", line)

        if heading_match:

            level = len(heading_match.group(1))
            content = heading_match.group(2)

            heading_style = styles[
                "h1" if level == 1 else
                "h2" if level == 2 else "h3"
            ]

            output.append(
                Paragraph(
                    "<b>" + format_inline(content) + "</b>",
                    heading_style
                )
            )

            continue

        if line.startswith(">"):
            line = "│ " + line.lstrip("> ")

        list_match = re.match(
            r"^(\s*)([-*+]|\d+[.)])\s+(.*)",
            raw_line
        )

        if list_match:
            marker = list_match.group(2)
            content = list_match.group(3)

            line = (
                "• " + content
                if marker in ("-", "*", "+")
                else marker + " " + content
            )

        for chunk in split_long_line(line):

            output.append(
                Paragraph(
                    format_inline(chunk),
                    styles["body"]
                )
            )

    if code_mode and code_lines:

        code_text = "<br/>".join(
            html.escape(x) for x in code_lines
        )

        output.append(
            Paragraph(
                f'<font name="Courier">{code_text}</font>',
                styles["code"]
            )
        )

    return output


# ==================================================
# CHAT BUBBLE DESIGN
# ==================================================

def create_bubble(message, paragraph, first=False):

    is_user = message["role"] == "user"

    if is_user:

        background = colors.HexColor("#DCEBFF")
        border = colors.HexColor("#B9D3F5")
        text_color = colors.HexColor("#17365D")
        heading = "You"
        alignment = "RIGHT"

    else:

        background = colors.HexColor("#F0F0F0")
        border = colors.HexColor("#DDDDDD")
        text_color = colors.HexColor("#252525")
        heading = "ChatGPT"
        alignment = "LEFT"

    elements = []

    if first:

        heading_style = ParagraphStyle(
            "BubbleHeading",
            fontName=BOLD,
            fontSize=9,
            leading=12,
            textColor=text_color,
            spaceAfter=4,
        )

        elements.append(
            Paragraph(heading, heading_style)
        )

    if isinstance(paragraph, Paragraph):
        elements.append(paragraph)
    else:
        elements.append(paragraph)

    bubble = Table(
        [[elements]],
        colWidths=[166 * mm],
        hAlign=alignment,
        splitByRow=1,
    )

    bubble.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), background),
        ("BOX", (0, 0), (-1, -1), 0.65, border),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 11),
        ("RIGHTPADDING", (0, 0), (-1, -1), 11),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
    ]))

    return bubble


# ==================================================
# PDF HEADER AND FOOTER
# ==================================================

def draw_page(canvas_obj, doc):

    canvas_obj.saveState()

    # Footer line
    canvas_obj.setStrokeColor(
        colors.HexColor("#E0E0E0")
    )

    canvas_obj.setLineWidth(0.5)

    canvas_obj.line(
        17 * mm,
        12 * mm,
        PAGE_WIDTH - 17 * mm,
        12 * mm,
    )

    canvas_obj.setFont(REGULAR, 8)

    canvas_obj.setFillColor(
        colors.HexColor("#777777")
    )

    canvas_obj.drawString(
        17 * mm,
        7 * mm,
        "ChatGPT Conversation PDF"
    )

    canvas_obj.drawRightString(
        PAGE_WIDTH - 17 * mm,
        7 * mm,
        f"Page {doc.page}"
    )

    canvas_obj.restoreState()


# ==================================================
# CREATE PDF
# ==================================================

def make_pdf(title, messages, filename):

    path = PDF_DIR / filename

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        rightMargin=15 * mm,
        leftMargin=15 * mm,
        topMargin=16 * mm,
        bottomMargin=19 * mm,
        title=title,
        author="ChatGPT PDF Bot",
        subject="ChatGPT Conversation",
        pageCompression=1,
    )

    styles = {

        "body": ParagraphStyle(
            "Body",
            fontName=REGULAR,
            fontSize=10,
            leading=15,
            textColor=colors.HexColor("#252525"),
            wordWrap="CJK",
            spaceAfter=5,
        ),

        "h1": ParagraphStyle(
            "H1",
            fontName=BOLD,
            fontSize=15,
            leading=20,
            textColor=colors.HexColor("#174EA6"),
            spaceBefore=7,
            spaceAfter=8,
        ),

        "h2": Paragraph
