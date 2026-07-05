"""
scan_existing.py

One-off scan of all currently-seeded products for candidate discount
codes that may already be present, independent of change detection.
Run this manually whenever you want a full sweep — not part of the
regular polling loop.
"""

import yaml

from database import get_connection
from diff_engine import find_candidate_codes
from notifier import send_notification


def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)


def build_product_link(base_url, handle):
    return f"{base_url}/products/{handle}"


def main():
    config = load_config()
    base_url = config["store"]["base_url"]

    conn = get_connection()
    try:
        cursor = conn.execute(
            "SELECT product_id, title, handle, description_text FROM products"
        )
        rows = cursor.fetchall()
    finally:
        conn.close()

    print(f"Scanning {len(rows)} products for existing candidate codes...")

    found_count = 0

    for row in rows:
        product_id, title, handle, description_text = (
            row["product_id"], row["title"], row["handle"], row["description_text"]
        )

        codes = find_candidate_codes(description_text)

        if codes:
            found_count += 1
            link = build_product_link(base_url, handle)

            message = (
                f"🔎 EXISTING CODE CANDIDATE FOUND\n\n"
                f"{title}\n"
                f"Candidates: {', '.join(codes)}\n\n"
                f"Link: {link}\n\n"
                f"Description: {description_text[:300]}"
            )

            print("=" * 40)
            print(message)

            send_notification(
                message,
                config["telegram"]["bot_token"],
                config["telegram"]["chat_ids"],
            )

    print(f"\nScan complete. {found_count} product(s) flagged out of {len(rows)}.")


if __name__ == "__main__":
    main()