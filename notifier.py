import time
import requests

MAX_MESSAGE_LENGTH = 4096


def send_notification(message, bot_token, chat_ids, max_retries=3):
    """
    Send a Telegram notification to one or more chat IDs.

    Automatically:
    - skips if Telegram is not configured
    - truncates oversized messages
    - retries temporary network failures per chat
    - isolates failures - one chat failing does not block others
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


def send_photo_group(image_urls, caption, bot_token, chat_ids):
    """
    Sends multiple images as a single Telegram album/message per chat
    (up to 10 per group, split into multiple messages if more). Falls
    back to a normal single-photo send if there is only one image.
    If the image/album send fails entirely (e.g. Telegram itself
    cannot fetch one of the image URLs), falls back to a plain text
    message with the image links, so the alert is never silently lost.
    """
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]

    if not bot_token or not chat_ids:
        print("Telegram not configured. Skipping notification.")
        return

    if not image_urls:
        print("No images to send.")
        return

    for chat_id in chat_ids:
        success = False

        if len(image_urls) == 1:
            single_url = f"https://api.telegram.org/bot{bot_token}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": image_urls[0], "caption": caption}
            try:
                response = requests.post(single_url, json=payload, timeout=20)
                response.raise_for_status()
                success = True
            except requests.RequestException as e:
                print(f"Telegram single photo send to {chat_id} failed: {e}")
        else:
            url = f"https://api.telegram.org/bot{bot_token}/sendMediaGroup"
            chunks = [image_urls[i:i + 10] for i in range(0, len(image_urls), 10)]
            all_chunks_ok = True

            for idx, chunk in enumerate(chunks):
                media = []
                for i, img_url in enumerate(chunk):
                    item = {"type": "photo", "media": img_url}
                    if i == 0 and idx == 0:
                        item["caption"] = caption
                    media.append(item)

                payload = {"chat_id": chat_id, "media": media}
                try:
                    response = requests.post(url, json=payload, timeout=20)
                    response.raise_for_status()
                except requests.RequestException as e:
                    print(f"Telegram album send to {chat_id} failed: {e}")
                    all_chunks_ok = False

            success = all_chunks_ok

        if not success:
            fallback_text = caption + "\n\nImages (album send failed):\n" + "\n".join(image_urls[:10])
            fallback_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            fallback_payload = {"chat_id": chat_id, "text": fallback_text[:4096]}
            try:
                requests.post(fallback_url, json=fallback_payload, timeout=15)
            except requests.RequestException as e:
                print(f"Telegram fallback text send to {chat_id} also failed: {e}")
