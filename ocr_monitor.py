"""
ocr_monitor.py  (v2)

OCRs NEW product images across ALL vendors and alerts on two signals:

  1. KEYWORD  -- extracted text contains a watched term
  2. TEXT-HEAVY -- extracted text contains many words, regardless of
     which words they are

Signal 2 is the important one. the store has said the next giveaway will not
be findable by searching for terms like "$1", "giveaway", or "discount
code". Keyword matching cannot survive an adversary who picks different
words. Word count can: a photograph of a handbag yields little text (a
brand stamp is 3-6 words), while any graphic carrying an announcement
yields many. So we alert on "this image contains writing" instead of
"this image contains a word I guessed in advance".

Every OCR result is stored whether or not it alerts, so the text can be
queried later without re-downloading anything.

Isolation: own SQLite file (data/ocr_monitor.db), own table, own copy of
send_notification. Does NOT touch product_images (owned by
image_monitor.py) or shopify_monitor.db.

Traffic: images come from cdn.shopify.com, not the store site. The only request
to the store site is one feed crawl per cycle. Images are fetched serially with
randomized gaps and capped per cycle.
"""

import random
import re
import sqlite3
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path

import requests
import yaml
import pytesseract
from PIL import Image

from poller import fetch_products

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data" / "ocr_monitor.db"

MAX_MESSAGE_LENGTH = 4096
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/120.0.0.0 Safari/537.36"
}

DEFAULTS = {
    "poll_interval": 900,
    "max_ocr_per_cycle": 15,
    "min_gap_seconds": 2.0,
    "max_gap_seconds": 6.0,
    "downscale_max_px": 1400,
    "alert_min_words": 12,
    "keywords": [],
}

WORD_RE = re.compile(r"[A-Za-z]{2,}")

REAL_WORDS = set("""the and for you our this that with are was not but all can has have had your they will from what when who how why out get got its into more some than then them there these those over only just also very much many must need needs needed want wants item items thing things win wins winner winners won winning free prize prizes gift gifts giveaway giveaways contest contests draw code codes coupon coupons discount discounts promo promotion sale sales off percent dollar dollars price priced deal deals offer offers buy buys bought purchase purchased purchasing checkout cart order orders ship ships shipped shipping deliver delivered delivery post posts posted posting share shared sharing tag tags tagged tagging story stories page pages public account accounts follow follower followers instagram facebook social media first fast quick hurry today tonight now soon limited exclusive special annual event events july august september june eligible eligibility qualify qualifies qualified requirement requirements rules rule terms conditions apply applies applied verify verified verification legitimate reserves right rights snatch snatched snatching claim claimed claiming enter entered entering entry entries chance chances lucky luck congratulations congrats one two three four five ten twenty bag bags handbag purse wallet leather condition authentic authenticity new used pristine excellent please refer provided photos comprehensive visual notable wear exterior interior minor signs reflects corner""".split())


def real_word_count(text):
    return sum(1 for w in WORD_RE.findall(text or "") if w.lower() in REAL_WORDS)


def load_config():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)
    section = config.get("ocr_monitor") or {}
    settings = dict(DEFAULTS)
    settings.update({k: v for k, v in section.items() if v is not None})
    return config, settings


def get_connection():
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def create_tables():
    conn = get_connection()
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_images (
                image_id INTEGER PRIMARY KEY,
                product_id INTEGER,
                src TEXT,
                ocr_text TEXT,
                scanned INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.commit()
    finally:
        conn.close()


def get_meta(key):
    conn = get_connection()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None
    finally:
        conn.close()


def set_meta(key, value):
    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


def known_image_ids():
    conn = get_connection()
    try:
        rows = conn.execute("SELECT image_id FROM seen_images").fetchall()
        return {row["image_id"] for row in rows}
    finally:
        conn.close()


def record_result(image_id, product_id, src, ocr_text):
    """
    Upsert. The baseline seed may already have inserted this row with
    scanned=0, in which case a plain INSERT would be ignored and the OCR
    result silently lost -- hence ON CONFLICT DO UPDATE.
    """
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


def seed_baseline(raw_products):
    conn = get_connection()
    count = 0
    try:
        for raw in raw_products:
            product_id = raw.get("id")
            for img in raw.get("images", []) or []:
                conn.execute(
                    "INSERT OR IGNORE INTO seen_images "
                    "(image_id, product_id, src, ocr_text, scanned, first_seen) "
                    "VALUES (?, ?, ?, NULL, 0, ?)",
                    (img.get("id"), product_id, img.get("src"),
                     datetime.now().isoformat()),
                )
                count += 1
        conn.commit()
    finally:
        conn.close()
    return count


def collect_new_images(raw_products, known_ids):
    new_images = []
    for raw in raw_products:
        product_id = raw.get("id")
        title = raw.get("title", "")
        handle = raw.get("handle", "")
        for img in raw.get("images", []) or []:
            image_id = img.get("id")
            if image_id is None or image_id in known_ids:
                continue
            new_images.append({
                "image_id": image_id,
                "product_id": product_id,
                "src": img.get("src"),
                "title": title,
                "handle": handle,
            })
    return new_images


def ocr_image(src, downscale_max_px):
    """
    Downloads one image and returns OCR text, or None on failure so the
    caller can leave it unrecorded and retry next cycle.

    Downscaling: tesseract cost scales with pixel count. Shrinking a
    2048px image to 1400px cuts CPU roughly in half. Text large enough to
    be an announcement survives easily; only tiny text like serial
    numbers is lost, which is not what we are looking for.
    """
    try:
        response = requests.get(src, timeout=25, headers=HEADERS)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        if downscale_max_px and max(image.size) > downscale_max_px:
            ratio = downscale_max_px / max(image.size)
            new_size = (max(1, int(image.width * ratio)),
                        max(1, int(image.height * ratio)))
            image = image.resize(new_size, Image.LANCZOS)
        return pytesseract.image_to_string(image).strip()
    except Exception as error:
        print(f"  OCR failed for {src}: {error}")
        return None


def word_count(text):
    return len(WORD_RE.findall(text or ""))


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


REAL_WORDS_FRENCH_EXTRA = set("""
article authenticite carte certifions chanel controles haute marque
notre qualite renom rigoureux soumis symbolise
""".split())


def looks_like_promo_code(text):
    """
    Only fires on tokens mixing letters AND digits (CC1000FF-style).
    The pure-alpha branch was removed: OCR misreads of embossed brand
    logos (VUITTON -> VUNTTONM, VUNTTONM, NOILINA) produce unusual
    all-caps strings that aren't real words either, and every LV
    monogram bag was tripping it as a false "code". A word-only code
    like DIORBAGDROP is still caught separately via the description
    text pipeline (NEW_DISCOUNT_CODE in diff_engine.py), which this
    does not touch.
    """
    import re as _re
    if not text:
        return []
    candidates = _re.findall(r"\b[A-Z0-9]{5,15}\b", text)
    codes = []
    for c in candidates:
        has_digit = any(ch.isdigit() for ch in c)
        has_letter = any(ch.isalpha() for ch in c)
        if has_digit and has_letter:
            codes.append(c)
    return sorted(set(codes))


def build_urgent_alert(header_reason, item_title, ocr_text, link, image_src):
    excerpt = " ".join((ocr_text or "").split())[:500]
    return (
        f"\U0001F6A8\U0001F6A8\U0001F6A8 HURRY UP \u2014 POSSIBLE LIVE PROMO \U0001F6A8\U0001F6A8\U0001F6A8\n\n"
        f"{header_reason}\n\n"
        f"{item_title}\n\n"
        f"Text seen:\n{excerpt}\n\n"
        f"Product: {link}\n"
        f"Image: {image_src}\n\n"
        f"\U0001F6A8 ACT NOW \u2014 items like this have sold in under 5 minutes \U0001F6A8"
    )


def send_urgent(message, bot_token, chat_ids):
    send_notification(message, bot_token, chat_ids)
    time.sleep(2)
    send_notification(message, bot_token, chat_ids)


def send_notification(message, bot_token, chat_ids, max_retries=3):
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]
    if not bot_token or not chat_ids:
        print("Telegram not configured. Skipping notification.")
        return
    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - 20] + "\n...[truncated]"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    for chat_id in chat_ids:
        payload = {"chat_id": chat_id, "text": message,
                   "disable_web_page_preview": False}
        for attempt in range(1, max_retries + 1):
            try:
                response = requests.post(url, json=payload, timeout=15)
                response.raise_for_status()
                break
            except requests.RequestException as error:
                print(f"  Telegram to {chat_id} attempt {attempt}/{max_retries} failed: {error}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)
                else:
                    print(f"  Telegram to {chat_id} permanently failed.")


def build_alert(header, item, ocr_text, base_url, words):
    excerpt = " ".join((ocr_text or "").split())
    if len(excerpt) > 900:
        excerpt = excerpt[:900] + "..."
    link = f"{base_url}/products/{item['handle']}"
    return (
        f"{header}\n\n"
        f"{item['title']}\n\n"
        f"Text in image ({words} real words):\n{excerpt}\n\n"
        f"Product: {link}\n"
        f"Image: {item['src']}"
    )


def run_once(config, settings):
    base_url = config["store"]["base_url"]
    bot_token = config["telegram"]["bot_token"]
    chat_ids = config["telegram"]["chat_ids"]
    keywords = settings["keywords"]
    max_per_cycle = int(settings["max_ocr_per_cycle"])
    min_gap = float(settings["min_gap_seconds"])
    max_gap = float(settings["max_gap_seconds"])
    downscale = int(settings["downscale_max_px"])
    min_words = int(settings["alert_min_words"])

    raw_products = fetch_products(base_url)

    if get_meta("baseline_seeded") != "true":
        seeded = seed_baseline(raw_products)
        set_meta("baseline_seeded", "true")
        stamp = datetime.now().strftime("%H:%M:%S")
        print(f"[{stamp}] Baseline seeded: {seeded} images recorded, none downloaded.")
        return

    known_ids = known_image_ids()
    new_images = collect_new_images(raw_products, known_ids)
    batch = new_images[:max_per_cycle]
    deferred = len(new_images) - len(batch)

    scanned = 0
    alerts = 0

    for index, item in enumerate(batch):
        if index > 0:
            time.sleep(random.uniform(min_gap, max_gap))

        ocr_text = ocr_image(item["src"], downscale)
        if ocr_text is None:
            continue

        record_result(item["image_id"], item["product_id"], item["src"], ocr_text)
        scanned += 1

        words = real_word_count(ocr_text)
        matched = check_keywords(ocr_text, keywords)
        codes = looks_like_promo_code(ocr_text)

        link = f"{base_url}/products/{item['handle']}"

        if matched or codes:
            reason = f"Keywords: {matched}" if matched else ""
            if codes:
                reason += (" | " if reason else "") + f"Code-shaped token(s): {codes}"
            alerts += 1
            print(f"  URGENT :: {reason} :: {item['title'][:45]}")
            send_urgent(
                build_urgent_alert(reason, item["title"], ocr_text, link, item["src"]),
                bot_token, chat_ids,
            )
        elif words >= min_words:
            header = f"\U0001F7E0 TEXT-HEAVY IMAGE ({words} words)"
            alerts += 1
            print(f"  ALERT {header} :: {item['title'][:45]}")
            send_notification(
                build_alert(header, item, ocr_text, base_url, words),
                bot_token, chat_ids,
            )

    stamp = datetime.now().strftime("%H:%M:%S")
    print(f"[{stamp}] New: {len(new_images)} | Scanned: {scanned} | "
          f"Alerts: {alerts} | Deferred: {deferred}")


def main():
    config, settings = load_config()
    create_tables()

    interval = int(settings["poll_interval"])
    print("=" * 48)
    print("OCR Monitor v2 (all vendors, new images only)")
    print(f"Interval ~{interval}s | Max {settings['max_ocr_per_cycle']}/cycle")
    print(f"Downscale to {settings['downscale_max_px']}px | "
          f"Text-heavy alert at {settings['alert_min_words']}+ words")
    print("=" * 48)

    while True:
        try:
            run_once(config, settings)
        except Exception as error:
            print(f"ERROR during OCR cycle: {error}")
        time.sleep(interval + random.uniform(-10, 20))


if __name__ == "__main__":
    main()
