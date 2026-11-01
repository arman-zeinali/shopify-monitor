"""
site_monitor.py

Standalone monitor for site-wide text changes on non-product pages
(homepage, FAQs, About Us). Runs independently of app.py / notifier.py
for the product monitor -- a bug here cannot affect the product monitor,
and vice versa.

Detects any text change on the configured pages and sends a Telegram
alert with only the changed lines. Flags alerts as HIGH PRIORITY if the
diff contains any of the configured keywords (giveaway, prize, etc).

First run on a fresh machine establishes a baseline (no alerts sent).
Every run after that compares against the stored baseline and alerts
on any difference.
"""

import hashlib
import json
import time
import difflib
import os
import re

import requests
import yaml
from bs4 import BeautifulSoup

CONFIG_PATH = "config.yaml"
STATE_PATH = "data/site_monitor_state.json"
MAX_MESSAGE_LENGTH = 4096


def load_config():
    with open(CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)


def load_state():
    if not os.path.exists(STATE_PATH):
        return {}
    with open(STATE_PATH, "r") as f:
        return json.load(f)


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


NOISY_CLASS_SUBSTRINGS = [
    "card-product",
    "js-error-message",
    "js-success-message",
    "card-slider",
    "card-collection",
]

NOISY_TEXT_SUBSTRINGS = [
    "themeColor",
    "iconColor",
    "showLogo",
    "topBottomPosition",
    "rightLeftPosition",
    "iconSize",
    "iconCustomSize",
]


def is_noisy_line(line):
    return any(marker in line for marker in NOISY_TEXT_SUBSTRINGS)


def fetch_page_text(url, scope_to_class=None):
    """
    Fetches a page and returns its visible text as a list of lines,
    stripped of scripts/styles, collapsed whitespace per line.
    Returns None on any fetch failure (network error, non-200, etc).

    If scope_to_class is given, only text inside the first element
    with that class is returned (used to isolate a hero/banner section
    from a noisy full page). If no element with that class is found,
    falls back to the full page (with known-noisy sections stripped)
    rather than silently returning nothing.

    Lines matching NOISY_TEXT_SUBSTRINGS (widget config JSON that
    Shopify injects as visible-looking text, e.g. wishlist/cookie
    consent widget settings) are always dropped, regardless of scope.
    """
    try:
        response = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (compatible; SiteMonitor/1.0)"
        })
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Failed to fetch {url}: {error}")
        return None

    soup = BeautifulSoup(response.text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    if scope_to_class:
        target = soup.find(class_=lambda c: c and scope_to_class in c)
        if target is not None:
            text = target.get_text("\n", strip=True)
            lines = [line.strip() for line in text.split("\n") if line.strip()]
            lines = [line for line in lines if not is_noisy_line(line)]
            return lines
        print(f"Warning: class '{scope_to_class}' not found on {url}, falling back to full page (noisy sections stripped).")

    for noisy in NOISY_CLASS_SUBSTRINGS:
        for tag in soup.find_all(class_=lambda c, n=noisy: c and n in c):
            tag.decompose()

    text = soup.get_text("\n", strip=True)
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    lines = [line for line in lines if not is_noisy_line(line)]
    return lines


def hash_lines(lines):
    joined = "\n".join(lines)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def diff_lines(old_lines, new_lines):
    diff = difflib.unified_diff(old_lines, new_lines, lineterm="")
    added = []
    for line in diff:
        if line.startswith("+") and not line.startswith("+++"):
            added.append(line[1:].strip())
    return added


def check_keywords(diff_text, keywords):
    lower = diff_text.lower()
    matched = []
    for kw in keywords:
        kw_lower = kw.lower()
        if kw_lower == "$1":
            if re.search(r"\$1(?![\d,])", lower):
                matched.append(kw)
        else:
            if kw_lower in lower:
                matched.append(kw)
    return matched


def send_notification(message, bot_token, chat_ids, max_retries=3):
    if isinstance(chat_ids, str):
        chat_ids = [chat_ids]

    if not bot_token or not chat_ids:
        print("Telegram not configured. Skipping notification.")
        return

    if len(message) > MAX_MESSAGE_LENGTH:
        message = message[: MAX_MESSAGE_LENGTH - len("\n...[truncated]")] + "\n...[truncated]"

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
                print(f"Telegram notification to {chat_id} attempt {attempt}/{max_retries} failed: {error}")
                if attempt < max_retries:
                    time.sleep(2 * attempt)
                else:
                    print(f"Telegram notification to {chat_id} permanently failed.")


def check_page(page, state, keywords, bot_token, chat_ids):
    """
    Tracks EVERY hash of lines ever seen for this page, not just the
    most recent one. A rotating homepage carousel flips between a
    small fixed set of banner states; without this, each flip back to
    a previously-seen state looks like "new" content and fires a
    false alert every rotation.
    """
    name = page["name"]
    url = page["url"]
    scope_to_class = page.get("scope_to_class")

    lines = fetch_page_text(url, scope_to_class=scope_to_class)
    if lines is None:
        return

    new_hash = hash_lines(lines)
    old_entry = state.get(url)

    if old_entry is None or "known_hashes" not in old_entry:
        state[url] = {
            "known_hashes": {new_hash: lines},
            "current_hash": new_hash,
        }
        print(f"[{name}] Baseline established.")
        return

    known = old_entry["known_hashes"]

    if new_hash in known:
        state[url]["current_hash"] = new_hash
        print(f"[{name}] No change (known rotation state).")
        return

    best_added = None
    for known_hash, known_lines in known.items():
        added = diff_lines(known_lines, lines)
        if best_added is None or len(added) < len(best_added):
            best_added = added

    added_lines = best_added or []

    known[new_hash] = lines
    if len(known) > 20:
        oldest_key = next(iter(known))
        if oldest_key != new_hash:
            known.pop(oldest_key, None)

    state[url] = {"known_hashes": known, "current_hash": new_hash}

    if not added_lines:
        print(f"[{name}] New state variant recorded, no new lines vs closest known state.")
        return

    diff_text = "\n".join(added_lines)
    matched_keywords = check_keywords(diff_text, keywords)

    if matched_keywords:
        prefix = f"🔴 HIGH PRIORITY CHANGE ({', '.join(matched_keywords)})"
    else:
        prefix = "🟡 Site change detected"

    preview_lines = added_lines[:25]
    preview_text = "\n".join(f"+ {line}" for line in preview_lines)
    if len(added_lines) > 25:
        preview_text += f"\n... and {len(added_lines) - 25} more changed lines"

    message = f"{prefix}\n\nPage: {name}\nURL: {url}\n\n{preview_text}"

    print(f"[{name}] Change detected. Sending notification.")
    send_notification(message, bot_token, chat_ids)


def run_once(config, state):
    pages = config["site_monitor"]["pages"]
    keywords = config["site_monitor"]["keywords"]
    bot_token = config["telegram"]["bot_token"]
    chat_ids = config["telegram"]["chat_ids"]

    for page in pages:
        check_page(page, state, keywords, bot_token, chat_ids)

    save_state(state)


def main():
    config = load_config()
    state = load_state()
    poll_interval = config.get("site_monitor", {}).get("poll_interval", 60)

    print("=" * 40)
    print("Site Monitor Started")
    print(f"Checking every {poll_interval} seconds")
    print("Press Ctrl+C to stop.")
    print("=" * 40)

    while True:
        try:
            run_once(config, state)
        except Exception as error:
            print(f"Unexpected error during check cycle: {error}")
        time.sleep(poll_interval)


if __name__ == "__main__":
    main()
