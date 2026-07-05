import time
import hashlib
from datetime import datetime

import yaml
from bs4 import BeautifulSoup

from poller import fetch_products
from database import (
    create_tables,
    product_exists,
    insert_product,
    update_product,
    get_product,
    variant_exists,
    insert_variant,
    update_variant,
    get_metadata,
    set_metadata,
)
from diff_engine import compare_product
from notifier import send_notification


POLL_INTERVAL_SECONDS = 30


def load_config():
    with open("config.yaml", "r") as file:
        return yaml.safe_load(file)


def clean_description(html):
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    text = soup.get_text(" ", strip=True)
    return " ".join(text.split())


def build_product(raw_product):
    clean_text = clean_description(raw_product.get("body_html", ""))
    return {
        "product_id": raw_product["id"],
        "title": raw_product["title"],
        "handle": raw_product["handle"],
        "description_text": clean_text,
        "description_hash": hashlib.sha256(clean_text.encode("utf-8")).hexdigest(),
        "last_seen": datetime.now().isoformat(),
    }


def build_variants(raw_product):
    variants = []
    for v in raw_product.get("variants", []):
        variants.append({
            "variant_id": v["id"],
            "product_id": raw_product["id"],
            "title": v.get("title", ""),
            "price": v["price"],
            "available": int(v["available"]),
            "last_seen": datetime.now().isoformat(),
        })
    return variants


def upsert_variants(variants):
    for v in variants:
        if variant_exists(v["variant_id"]):
            update_variant(v)
        else:
            insert_variant(v)


def format_event_message(product_title, product_handle, base_url, event):
    link = f"{base_url}/products/{product_handle}"
    t = event["type"]

    if t == "DOLLAR_ITEM":
        return (
            f"🚨 $1 ITEM DETECTED\n\n{product_title}\n"
            f"Variant: {event['variant_title']}\n"
            f"${event['old_price']:.2f} → ${event['new_price']:.2f}\n\n"
            f"Link: {link}"
        )
    if t == "PRICE_CHANGED":
        return (
            f"💲 Price changed\n\n{product_title}\n"
            f"Variant: {event['variant_title']}\n"
            f"${event['old_price']:.2f} → ${event['new_price']:.2f}\n\n"
            f"Link: {link}"
        )
    if t == "BACK_IN_STOCK":
        return f"🔵 Back in stock\n\n{product_title}\nVariant: {event['variant_title']}\n\nLink: {link}"
    if t == "SOLD_OUT":
        return f"⚫ Sold out\n\n{product_title}\nVariant: {event['variant_title']}\n\nLink: {link}"
    if t == "NEW_VARIANT":
        return f"🆕 New variant\n\n{product_title}\n{event['variant_title']} — ${event['price']}\n\nLink: {link}"
    if t == "NEW_DISCOUNT_CODE":
        return (
            f"🎁 POSSIBLE DISCOUNT CODE\n\n{product_title}\n"
            f"Candidate: {event['code']}\n\n"
            f"Context: {event['context']}\n\n"
            f"Link: {link}"
        )
    if t == "DESCRIPTION_CHANGED":
        return f"📝 Description changed\n\n{product_title}\n\n{event['diff_snippet']}\n\nLink: {link}"

    return f"{product_title}\n\n{t}\n\nLink: {link}"


def seed_database(raw_products):
    for raw in raw_products:
        product = build_product(raw)
        insert_product(product)
        for v in build_variants(raw):
            insert_variant(v)

    set_metadata("schema_v2_seeded", "true")
    print(f"Seeded {len(raw_products)} products. No notifications sent this run.")


def run_once(config):
    """
    Runs a single check cycle: fetch, compare, notify, update.
    Returns nothing — prints a summary line to the terminal so you
    can watch it working in real time.
    """
    raw_products = fetch_products(config["store"]["base_url"])

    if get_metadata("schema_v2_seeded") != "true":
        seed_database(raw_products)
        return

    inserted = 0
    changed_products = 0
    total_events = 0

    for raw in raw_products:
        product = build_product(raw)
        variants = build_variants(raw)

        if not product_exists(product["product_id"]):
            insert_product(product)
            for v in variants:
                insert_variant(v)

            inserted += 1
            print(f"NEW PRODUCT: {product['title']}")

            link = f"{config['store']['base_url']}/products/{product['handle']}"
            send_notification(
                f"🆕 NEW PRODUCT\n\n{product['title']}\n\nLink: {link}",
                config["telegram"]["bot_token"],
                config["telegram"]["chat_ids"],
            )
            continue

        old_product = get_product(product["product_id"])
        events = compare_product(
            product["product_id"],
            variants,
            old_product["description_text"],
            product["description_text"],
        )

        if events:
            changed_products += 1
            total_events += len(events)
            print("=" * 40)
            print(product["title"])

            for event in events:
                print(event)
                send_notification(
                    format_event_message(
                        product["title"],
                        product["handle"],
                        config["store"]["base_url"],
                        event,
                    ),
                    config["telegram"]["bot_token"],
                    config["telegram"]["chat_ids"],
                )

        update_product(product)
        upsert_variants(variants)

    timestamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{timestamp}] Downloaded: {len(raw_products)} | Inserted: {inserted} | "
          f"Changed: {changed_products} | Events: {total_events}")


def main():
    config = load_config()
    create_tables()

    print("=" * 40)
    print(f"Shopify Monitor Started — checking every {POLL_INTERVAL_SECONDS} seconds")
    print("Press Ctrl+C to stop.")
    print("=" * 40)

    while True:
        try:
            run_once(config)
        except Exception as e:
            print(f"ERROR during check cycle: {e}")
            print("Continuing to next cycle...")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()