"""
image_monitor.py
Standalone, separate monitor for Hermes and Louis Vuitton product images.
"""

import time
import random
from datetime import datetime
from io import BytesIO

import yaml
import requests
import pytesseract
from PIL import Image

from poller import fetch_products
from database import create_image_tables, image_exists, insert_image
from notifier import send_notification, send_photo_group

POLL_INTERVAL_SECONDS = 120
TARGET_VENDOR_NAMES = {
    "hermes", "hermès", "louis vuitton", "louis viutton",
    "gucci",
    "saint laurent", "ysl", "yves saint laurent",
    "chanel",
}
HEADERS = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'}


def load_config():
    with open("config.yaml", "r") as file:
        return yaml.safe_load(file)


def is_target_vendor(raw_product):
    vendor = (raw_product.get("vendor") or "").strip().lower()
    return vendor in TARGET_VENDOR_NAMES


def check_image_for_text(image_url):
    try:
        response = requests.get(image_url, timeout=15, headers=HEADERS)
        img = Image.open(BytesIO(response.content))
        return pytesseract.image_to_string(img).strip()
    except Exception as e:
        print(f"OCR check failed for {image_url}: {e}")
        return ""


def run_once(config):
    raw_products = fetch_products(config["store"]["base_url"])
    checked = 0
    new_images = 0

    for raw in raw_products:
        if not is_target_vendor(raw):
            continue

        product_id = raw["id"]
        title = raw["title"]
        handle = raw["handle"]
        all_images = raw.get("images", [])
        new_found = False

        for img in all_images:
            image_id = img["id"]
            src = img["src"]

            if image_exists(image_id):
                continue

            checked += 1
            ocr_text = check_image_for_text(src)
            insert_image(image_id, product_id, src, ocr_text, datetime.now().isoformat())
            new_images += 1
            new_found = True

        if new_found:
            link = f"{config['store']['base_url']}/products/{handle}"
            all_urls = [img["src"] for img in all_images]
            caption = f"New image(s) - {raw.get('vendor', 'Unknown')}\n\n{title}\n\nLink: {link}"
            send_photo_group(all_urls, caption, config["telegram"]["bot_token"], config["telegram"]["chat_ids"])

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] Checked {checked} new images | New: {new_images}")


def main():
    config = load_config()
    create_image_tables()
    print("=" * 40)
    print(f"Image Monitor Started (Hermes + Louis Vuitton) - checking every {POLL_INTERVAL_SECONDS}s")
    print("=" * 40)

    while True:
        try:
            run_once(config)
        except Exception as e:
            print(f"ERROR during image check cycle: {e}")
        time.sleep(POLL_INTERVAL_SECONDS + random.uniform(-5, 10))


if __name__ == "__main__":
    main()
