import time
import requests

MAX_MESSAGE_LENGTH = 4096


def send_notification(message, bot_token, chat_ids, max_retries=3):
    """
    Send a Telegram notification to one or more chat IDs.

    Automatically:
    • skips if Telegram is not configured
    • truncates oversized messages
    • retries temporary network failures per chat
    • isolates failures — one chat failing does not block others
    """

    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]

    if not bot_token or not chat_ids:
        print("Telegram not configured. Skipping notification.")
        return

    if len(message) > MAX_MESSAGE_LENGTH:
        message = (
            message[: MAX_MESSAGE_LENGTH - len("\n...[truncated]")]
            + "\n...[truncated]"
        )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": False,
        }

        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, json=payload, timeout=15)
                response.raise_for_status()
                break

            except requests.RequestException as error:
                print(
                    f"Telegram notification to {chat_id} attempt {attempt}/{max_retries} failed: {error}"
                )

                if attempt < max_retries:
                    time.sleep(2 * attempt)
        else:
            print(f"Telegram notification to {chat_id} permanently failed.")