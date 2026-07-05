"""
diff_engine.py

Change detection for the Shopify product monitor.

Two independent detection paths:
  1. Variant-level price/availability changes (the $1 drop path).
  2. Description text anomalies — any token that doesn't read as
     normal prose (all-caps, mixed alphanumeric, or otherwise
     structurally unusual) is treated as a candidate discount code,
     regardless of what words surround it. Serial-number-shaped
     tokens (common in luxury resale descriptions for authentication
     purposes) are filtered out separately.

Both return structured event dicts, not pre-formatted strings, so
message formatting stays decoupled from detection logic.
"""

import difflib
import re

from database import get_variants

DOLLAR_THRESHOLD = 1.00

TOKEN_PATTERN = re.compile(r'\b[A-Za-z0-9]{4,20}\b')

STOPWORDS = {
    "NWT", "NWOT", "VGC", "EUC", "GUC", "FGC", "GENTLY", "USED",
    "GHW", "SHW", "RHW", "PHW",
    "XS", "XXS", "SM", "MD", "LG", "XL", "XXL", "OS",
    "USD", "CAD", "LDJ", "SKU", "ID",
    "NEW", "SALE", "SHIP", "SHIPPING", "FREE", "GIFT", "LIMITED",
    "ONLY", "TODAY", "NOW", "HOT", "SOLD", "OUT",
    "CM", "MM", "IN",
}

COMMON_PROMO_FRAGMENTS = (
    "VIP", "SAVE", "OFF", "SALE", "FREE", "SHIP", "WELCOME",
    "SUMMER", "WINTER", "SPRING", "FALL", "GIFT", "BDAY",
    "BIRTHDAY", "THANK", "LOYAL", "MEMBER", "FIRST", "NEW",
    "EXTRA", "BONUS", "SPECIAL", "SECRET", "LUCKY", "WIN",
)


def looks_like_measurement(token):
    """
    Filters tokens like '35cm', '10in' — dimension measurements
    common in product descriptions, not codes.
    """
    match = re.match(r'^(\d+)(cm|mm|in|kg|lb|oz)$', token.lower())
    return match is not None


def looks_like_serial_number(token):
    """
    Filters manufacturer serial numbers / authentication codes.
    These are dense alphanumeric strings with no recognizable
    promo-word fragment and an unusually low vowel ratio — real
    promo codes are almost always brandable and readable.
    """
    upper = token.upper()

    if any(frag in upper for frag in COMMON_PROMO_FRAGMENTS):
        return False

    has_digit = any(c.isdigit() for c in token)
    if not has_digit:
        return False

    letters_only = "".join(c for c in upper if c.isalpha())
    if not letters_only:
        return True

    vowels = sum(1 for c in letters_only if c in "AEIOU")
    vowel_ratio = vowels / len(letters_only)

    return vowel_ratio < 0.2


def is_candidate_code(token):
    if token.upper() in STOPWORDS:
        return False

    if looks_like_measurement(token):
        return False

    if looks_like_serial_number(token):
        return False

    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)

    if has_digit and has_alpha:
        return True

    if token.isupper() and has_alpha and len(token) >= 4:
        return True

    return False


def find_candidate_codes(text):
    if not text:
        return set()
    tokens = TOKEN_PATTERN.findall(text)
    return {t for t in tokens if is_candidate_code(t)}


def compare_variants(product_id, incoming_variants):
    events = []
    existing = get_variants(product_id)

    for v in incoming_variants:
        old = existing.get(v["variant_id"])

        if old is None:
            events.append({
                "type": "NEW_VARIANT",
                "variant_id": v["variant_id"],
                "variant_title": v["title"],
                "price": v["price"],
            })
            continue

        if old["price"] != v["price"]:
            old_price = float(old["price"])
            new_price = float(v["price"])

            if new_price <= DOLLAR_THRESHOLD < old_price:
                events.append({
                    "type": "DOLLAR_ITEM",
                    "variant_id": v["variant_id"],
                    "variant_title": v["title"],
                    "old_price": old_price,
                    "new_price": new_price,
                })
            else:
                events.append({
                    "type": "PRICE_CHANGED",
                    "variant_id": v["variant_id"],
                    "variant_title": v["title"],
                    "old_price": old_price,
                    "new_price": new_price,
                })

        old_available = int(old["available"])
        new_available = int(v["available"])

        if old_available != new_available:
            if old_available == 0 and new_available == 1:
                events.append({
                    "type": "BACK_IN_STOCK",
                    "variant_id": v["variant_id"],
                    "variant_title": v["title"],
                })
            else:
                events.append({
                    "type": "SOLD_OUT",
                    "variant_id": v["variant_id"],
                    "variant_title": v["title"],
                })

    return events


def _diff_added_text(old_text, new_text):
    old_chunks = (old_text or "").split(". ")
    new_chunks = (new_text or "").split(". ")
    diff = difflib.unified_diff(old_chunks, new_chunks, lineterm="")
    return [
        line[1:].strip()
        for line in diff
        if line.startswith("+") and not line.startswith("+++")
    ]


def compare_description(old_text, new_text):
    events = []

    if old_text == new_text:
        return events

    added_segments = _diff_added_text(old_text, new_text)
    added_text = " ".join(added_segments)

    old_codes = find_candidate_codes(old_text)
    new_codes = find_candidate_codes(new_text)
    new_only_codes = new_codes - old_codes

    if new_only_codes:
        for code in new_only_codes:
            events.append({
                "type": "NEW_DISCOUNT_CODE",
                "code": code,
                "context": (added_text or new_text)[:300],
            })

    events.append({
        "type": "DESCRIPTION_CHANGED",
        "diff_snippet": added_text[:300] if added_text else "(text removed or reordered)",
    })

    return events


def compare_product(product_id, incoming_variants, old_description_text, new_description_text):
    events = []
    events.extend(compare_variants(product_id, incoming_variants))
    events.extend(compare_description(old_description_text, new_description_text))
    return events