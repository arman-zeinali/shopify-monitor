"""
scan_existing.py

One-off scan of all currently-seeded products for candidate discount
codes. Writes results to a single local file for review — does NOT
send individual Telegram messages per hit, since this can produce
dozens of results on a large catalog and isn't time-sensitive the
way live detection is.
"""

import yaml

from database import get_connection
from diff_engine import find_candidate_codes


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

    results = []

    for row in rows:
        product_id, title, handle, description_text = (
            row["product_id"], row["title"], row["handle"], row["description_text"]
        )
        codes = find_candidate_codes(description_text)
        if codes:
            results.append({
                "title": title,
                "handle": handle,
                "codes": sorted(codes),
                "link": build_product_link(base_url, handle),
            })

    with open("scan_results.txt", "w") as f:
        f.write(f"Scan of {len(rows)} products — {len(results)} flagged\n")
        f.write("=" * 60 + "\n\n")
        for r in results:
            f.write(f"{r['title']}\n")
            f.write(f"Candidates: {', '.join(r['codes'])}\n")
            f.write(f"Link: {r['link']}\n")
            f.write("-" * 60 + "\n")

    print(f"Done. {len(results)} product(s) flagged. Results written to scan_results.txt")


if __name__ == "__main__":
    main()