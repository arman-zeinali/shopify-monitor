"""
backfill_ocr.py  (v2)

One-shot historical scan of images already in LDJ's feed. Stores every
OCR result in ocr_monitor.db so the text can be searched afterward
without re-downloading anything.

Design decision worth understanding: this script does NOT alert on every
image with text. Product photos routinely include brand stamps,
authenticity cards, and date codes, so a naive "has text" alert over
7,000 images would produce hundreds of useless notifications.

Instead: collect now, analyse after. Run the scan, then run --report to
see every scanned image ranked by how much text it contains. A promo
graphic carrying an announcement will sit at the top of that list; a
stamp closeup will not. This works without knowing which words to look
for, which matters because LDJ has said the next giveaway will not be
findable by searching for obvious terms.

Keyword matches still alert immediately during the scan, as a shortcut.

Tiers (by image dimensions):
  1  non-square only
  2  non-square + squares that are not 2048x2048 or 720x720
  3  tier 2 plus 720x720
  4  everything

Writes only to ocr_monitor.db. Does not touch product_images or
shopify_monitor.db. Images come from cdn.shopify.com; the only ldj.com
request is a single feed crawl.
"""

import argparse
import random
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

import requests
import yaml
import pytesseract
from PIL import Image

from poller import fetch_products

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "ocr_monitor.db"

STANDARD_SIZES = {(2048, 2048), (720, 720)}
MAX_MESSAGE_LENGTH = 4096
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

WORD_RE = re.compile(r"[A-Za-z]{2,}")

REAL_WORDS = set("""the and for you our this that with are was not but all can has have had your they will from what when who how why out get got its into more some than then them there these those over only just also very much many must need needs needed want wants item items thing things win wins winner winners won winning free prize prizes gift gifts giveaway giveaways contest contests draw code codes coupon coupons discount discounts promo promotion sale sales off percent dollar dollars price priced deal deals offer offers buy buys bought purchase purchased purchasing checkout cart order orders ship ships shipped shipping deliver delivered delivery post posts posted posting share shared sharing tag tags tagged tagging story stories page pages public account accounts follow follower followers instagram facebook social media first fast quick hurry today tonight now soon limited exclusive special annual event events july august september june eligible eligibility qualify qualifies qualified requirement requirements rules rule terms conditions apply applies applied verify verified verification legitimate reserves right rights snatch snatched snatching claim claimed claiming enter entered entering entry entries chance chances lucky luck congratulations congrats one two three four five ten twenty bag bags handbag purse wallet leather condition authentic authenticity new used pristine excellent please refer provided photos comprehensive visual notable wear exterior interior minor signs reflects corner""".split())


def real_word_count(text):
    return sum(1 for w in WORD_RE.findall(text or "") if w.lower() in REAL_WORDS)


def get_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def word_count(text):
    return len(WORD_RE.findall(text or ""))


def record_result(image_id, product_id, src, ocr_text):
    conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO seen_images
                (image_id, product_id, src, ocr_text, scanned, first_seen)
            VALUES (?, ?, ?, ?, 1, ?)
            ON CONFLICT(image_id) DO UPDATE SET
                ocr_text = excluded.ocr_text,
                scanned = 1
            """,
            (image_id, product_id, src, ocr_text, datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()


def already_scanned_ids():
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT image_id FROM seen_images WHERE scanned = 1"
        ).fetchall()
        return {row["image_id"] for row in rows}
    finally:
        conn.close()


def tier_filter(width, height, tier):
    if width is None or height is None:
        return tier == 4
    size = (width, height)
    is_square = width == height
    if tier == 1:
        return not is_square
    if tier == 2:
        return (not is_square) or (size not in STANDARD_SIZES)
    if tier == 3:
        return (not is_square) or (size != (2048, 2048))
    return True


def collect_targets(raw_products, cutoff, tier, skip_ids):
    targets = []
    for raw in raw_products:
        title = raw.get("title", "")
        handle = raw.get("handle", "")
        product_id = raw.get("id")
        for img in raw.get("images", []) or []:
            image_id = img.get("id")
            if image_id is None or image_id in skip_ids:
                continue
            created = img.get("created_at")
            if cutoff and created:
                try:
                    if datetime.fromisoformat(created) < cutoff:
                        continue
                except ValueError:
                    pass
            if not tier_filter(img.get("width"), img.get("height"), tier):
                continue
            targets.append({
                "image_id": image_id,
                "product_id": product_id,
                "src": img.get("src"),
                "title": title,
                "handle": handle,
                "width": img.get("width"),
                "height": img.get("height"),
                "created_at": created,
            })
    return targets


def ocr_image(src, downscale_max_px):
    try:
        response = requests.get(src, timeout=25, headers=HEADERS)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        if downscale_max_px and max(image.size) > downscale_max_px:
            ratio = downscale_max_px / max(image.size)
            image = image.resize(
                (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                Image.LANCZOS,
            )
        return pytesseract.image_to_string(image).strip()
    except Exception as error:
        print(f"  OCR failed: {error}")
        return None


def check_keywords(text, keywords):
    lower = (text or "").lower()
    matched = []
    for kw in keywords:
        k = kw.lower()
        if k == "$1":
            if re.search(r"\$\s?1(?![\d,])", lower):
                matched.append(kw)
        else:
            if re.search(r"\b" + re.escape(k) + r"\b", lower):
                matched.append(kw)
    return matched


def send_notification(message, bot_token, chat_ids, max_retries=3):
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
    if not bot_token or not chat_ids:
        return
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 20] + "\n...[truncated]"
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": message,
                   "disable_web_page_preview": False}
        for attempt in range(1, max_retries + 1):
            try:
                requests.post(url, json=payload, timeout=15).raise_for_status()
                break
            except requests.RequestException as error:
                print(f"  Telegram attempt {attempt} failed: {error}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)


def run_report(top_n, min_words):
    """
    Ranks every scanned image by word count. No network, no downloads.
    This is where a promo graphic surfaces: it will have far more text
    than a brand stamp or a date code.
    """
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT image_id, src, ocr_text FROM seen_images "
            "WHERE scanned = 1 AND ocr_text IS NOT NULL AND ocr_text != ''"
        ).fetchall()
    finally:
        conn.close()

    ranked = []
    for row in rows:
        words = real_word_count(row["ocr_text"])
        if words >= min_words:
            ranked.append((words, row["src"], " ".join(row["ocr_text"].split())))
    ranked.sort(reverse=True, key=lambda item: item[0])

    total_scanned = len(rows)
    print(f"Scanned images with any text: {total_scanned}")
    print(f"With {min_words}+ words: {len(ranked)}")
    print()
    for words, src, text in ranked[:top_n]:
        print(f"[{words} real words] {src}")
        print(f"   {text[:400]}")
        print()
    if len(ranked) > top_n:
        print(f"... and {len(ranked) - top_n} more. Raise --top to see them.")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tier", type=int, default=4, choices=[1, 2, 3, 4])
    parser.add_argument("--since", default="all",
                        help="YYYY-MM-DD, or 'all' for no date limit")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", action="store_true",
                        help="rank already-scanned images by word count; no network")
    parser.add_argument("--top", type=int, default=40)
    parser.add_argument("--min-words", type=int, default=10)
    parser.add_argument("--min-gap", type=float, default=0.5)
    parser.add_argument("--max-gap", type=float, default=1.5)
    parser.add_argument("--downscale", type=int, default=1400)
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: {DB_PATH} not found. Run ocr_monitor.py first to seed.")
        sys.exit(1)

    if args.report:
        run_report(args.top, args.min_words)
        return

    config = yaml.safe_load(open("config.yaml"))
    settings = config.get("ocr_monitor") or {}
    keywords = settings.get("keywords") or []
    bot_token = config["telegram"]["bot_token"]
    chat_ids = config["telegram"]["chat_ids"]
    base_url = config["store"]["base_url"]

    cutoff = None
    if args.since != "all":
        cutoff = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)

    print(f"Tier {args.tier} | since {args.since} | downscale {args.downscale}px "
          f"| gap {args.min_gap}-{args.max_gap}s | dry-run={args.dry_run}")
    print("Fetching feed (one crawl)...")
    raw_products = fetch_products(base_url)

    skip_ids = already_scanned_ids()
    targets = collect_targets(raw_products, cutoff, args.tier, skip_ids)
    if args.limit:
        targets = targets[: args.limit]

    print(f"Already scanned (skipped): {len(skip_ids)}")
    print(f"Targets this run: {len(targets)}")

    per_image = (args.min_gap + args.max_gap) / 2 + 1.0
    print(f"Rough estimate: {len(targets) * per_image / 3600:.1f} hours")
    print()

    if args.dry_run:
        for t in targets[:40]:
            print(f"  {t['width']}x{t['height']}  {t['created_at']}  {t['title'][:45]}")
        if len(targets) > 40:
            print(f"  ... and {len(targets) - 40} more")
        print("\nNothing downloaded.")
        return

    scanned = 0
    keyword_hits = 0
    text_heavy = 0
    failures = 0
    started = time.time()

    for index, t in enumerate(targets):
        if index > 0:
            time.sleep(random.uniform(args.min_gap, args.max_gap))

        ocr_text = ocr_image(t["src"], args.downscale)
        if ocr_text is None:
            failures += 1
            continue

        record_result(t["image_id"], t["product_id"], t["src"], ocr_text)
        scanned += 1

        words = real_word_count(ocr_text)
        if words >= args.min_words:
            text_heavy += 1

        matched = check_keywords(ocr_text, keywords)
        if matched and words >= 3:
            keyword_hits += 1
            excerpt = " ".join(ocr_text.split())[:800]
            link = f"{base_url}/products/{t['handle']}"
            print(f"  *** KEYWORD MATCH {matched}: {t['title'][:40]}")
            send_notification(
                f"\U0001F534 BACKFILL KEYWORD MATCH ({', '.join(matched)})\n\n"
                f"{t['title']}\n\n"
                f"Text in image ({words} words):\n{excerpt}\n\n"
                f"Uploaded: {t['created_at']}\n"
                f"Product: {link}\n"
                f"Image: {t['src']}",
                bot_token, chat_ids,
            )

        if (index + 1) % 25 == 0 or index + 1 == len(targets):
            elapsed = time.time() - started
            rate = (index + 1) / elapsed if elapsed else 0
            remaining = (len(targets) - index - 1) / rate / 60 if rate else 0
            print(f"[{index + 1}/{len(targets)}] scanned={scanned} "
                  f"text-heavy={text_heavy} keyword={keyword_hits} "
                  f"fail={failures} | ~{remaining:.0f} min left")

    print(f"\nDone. Scanned: {scanned} | Text-heavy: {text_heavy} | "
          f"Keyword hits: {keyword_hits} | Failures: {failures}")
    print("Now run:  .venv/bin/python backfill_ocr.py --report")


if __name__ == "__main__":
    main()
