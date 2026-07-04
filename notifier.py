import time
import requests

MAX_MESSAGE_LENGTH = 4096


def send_notification(message, bot_token, chat_id, max_retries=3):
    print("BOT:", repr(bot_token), "CHAT:", repr(chat_id))
    
    """
    Send a Telegram notification.

    Automatically:
    • skips if Telegram is not configured
    • truncates oversized messages
    • retries temporary network failures
    """

    if not bot_token or not chat_id:
        print("Telegram not configured. Skipping notification.")
        return

    if len(message) > MAX_MESSAGE_LENGTH:
        message = (
            message[: MAX_MESSAGE_LENGTH - len("\n...[truncated]")]
            + "\n...[truncated]"
        )

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"

    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": False,
    }

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=15,
            )

            response.raise_for_status()

            return

        except requests.RequestException as error:

            print(
                f"Telegram notification attempt {attempt}/{max_retries} failed: {error}"
            )

            if attempt < max_retries:
                time.sleep(2 * attempt)

    print("Telegram notification permanently failed.")