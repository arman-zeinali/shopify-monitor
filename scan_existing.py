"""
scan_existing.py

One-off scan of all currently-seeded products for:
  1. Candidate discount codes already present in descriptions.
  2. Variants already priced at $1 or less.

Writes results to scan_results.txt for review. Does not send
Telegram messages — this is a manual historical check, separate
from the live monitor's real-time alerts.
"""

import yaml

from database import get_connection
from diff_engine import find_candidate_codes

DOLLAR_THRESHOLD = 1.00


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
        products = conn.execute(
            "SELECT product_id, title, handle, description_text FROM products"
        ).fetchall()

        variants = conn.execute(
            "SELECT product_id, variant_id, title, price, available FROM variants"
        ).fetchall()
    finally:
        conn.close()

    print(f"Scanning {len(products)} products and {len(variants)} variants...")

    product_lookup = {p["product_id"]: p for p in products}

    code_results = []
    price_results = []

    for p in products:
        codes = find_candidate_codes(p["description_text"])
        if codes:
            code_results.append({
                "title": p["title"],
                "handle": p["handle"],
                "codes": sorted(codes),
                "link": build_product_link(base_url, p["handle"]),
            })

    for v in variants:
        try:
            price = float(v["price"])
        except (TypeError, ValueError):
            continue

        if price <= DOLLAR_THRESHOLD:
            product = product_lookup.get(v["product_id"])
            if product:
                price_results.append({
                    "title": product["title"],
                    "variant_title": v["title"],
                    "price": price,
                    "available": bool(v["available"]),
                    "link": build_product_link(base_url, product["handle"]),
                })

    with open("scan_results.txt", "w") as f:
        f.write(f"Scan of {len(products)} products, {len(variants)} variants\n")
        f.write("=" * 60 + "\n\n")

        f.write(f"$1-OR-LESS ITEMS: {len(price_results)} found\n")
        f.write("-" * 60 + "\n")
        for r in price_results:
            f.write(f"{r['title']} — {r['variant_title']}\n")
            f.write(f"Price: ${r['price']:.2f} | In stock: {r['available']}\n")
            f.write(f"Link: {r['link']}\n\n")

        f.write("\n")
        f.write(f"CANDIDATE CODES: {len(code_results)} found\n")
        f.write("-" * 60 + "\n")
        for r in code_results:
            f.write(f"{r['title']}\n")
            f.write(f"Candidates: {', '.join(r['codes'])}\n")
            f.write(f"Link: {r['link']}\n\n")

    print(f"Done. {len(price_results)} $1-or-less item(s), {len(code_results)} code candidate(s).")
    print("Results written to scan_results.txt")


if __name__ == "__main__":
    main()