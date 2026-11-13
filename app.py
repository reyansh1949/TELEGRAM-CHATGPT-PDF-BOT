
def extract_chat(url):
    match = re.search(
        r"chatgpt\.com/share/([a-zA-Z0-9-]+)",
        url
    )

    if not match:
        raise ValueError("Invalid ChatGPT share link.")

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

    title = data.get("title", "ChatGPT Notes")
    mapping = data.get("mapping", {})

    messages = []

    for node in mapping.values():
        message = node.get("message")

        if not message:
            continue

        author = message.get("author", {})
        role = author.get("role", "")

        if role not in ("user", "assistant"):
            continue

        content = message.get("content", {})
        parts = content.get("parts", [])

        for part in parts:
            if isinstance(part, str):
                messages.append(
                    role.upper() + ": " + part
                )

    if not messages:
        raise ValueError(
            "Conversation API se messages nahi mile. "
            "Share link access ya API response check karein."
        )

    return title, "\n\n".join(messages)
    
