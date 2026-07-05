"""
diff_engine.py

Change detection for the Shopify product monitor.

Two independent detection paths:
  1. Variant-level price/availability changes (the $1 drop path).
  2. Description text anomalies — any token that doesn't read as
     normal prose (all-caps, mixed alphanumeric, or otherwise
     structurally unusual) is treated as a candidate discount code,
     regardless of what words surround it. This does NOT require a
     trigger word like "code" or "off" — LDJ may phrase this any way
     they choose, so detection is based on token shape, not vocabulary.

Both return structured event dicts, not pre-formatted strings, so
message formatting stays decoupled from detection logic.
"""

import difflib
import re

from database import get_variants

DOLLAR_THRESHOLD = 1.00

# Matches any standalone alphanumeric token 4-20 characters long.
# Deliberately broad — filtering happens in is_candidate_code(),
# not in this pattern.
TOKEN_PATTERN = re.compile(r'\b[A-Za-z0-9]{4,20}\b')

# Known non-code tokens specific to luxury resale descriptions.
# These appear constantly in normal product text and would otherwise
# flood you with false positives. Extend this list as you observe
# more noise in real alerts.
STOPWORDS = {
    # Condition abbreviations
    "NWT", "NWOT", "VGC", "EUC", "GUC", "FGC", "GENTLY", "USED",
    # Hardware finish abbreviations
    "GHW", "SHW", "RHW", "PHW",
    # Sizes
    "XS", "XXS", "SM", "MD", "LG", "XL", "XXL", "OS",
    # Currency / misc
    "USD", "CAD", "LDJ", "SKU", "ID",
    # Common capitalized words that appear in normal marketing prose
    "NEW", "SALE", "SHIP", "SHIPPING", "FREE", "GIFT", "LIMITED",
    "ONLY", "TODAY", "NOW", "HOT", "SOLD", "OUT",
}


def is_candidate_code(token):
    """
    Returns True if a token looks structurally like a promo code
    rather than a normal English word — based on shape, not on
    matching a specific vocabulary of trigger words.

    Signals used:
      - Mixed letters + digits (e.g. VIP20, SAVE50) — strong signal,
        almost never occurs in normal prose.
      - All-caps word not in the stopword list — normal descriptions
        are sentence-case, so an all-caps word stands out on its own.
    """
    if token.upper() in STOPWORDS:
        return False

    has_digit = any(c.isdigit() for c in token)
    has_alpha = any(c.isalpha() for c in token)

    if has_digit and has_alpha:
        return True

    if token.isupper() and has_alpha and len(token) >= 4:
        return True

    return False


def find_candidate_codes(text):
    """
    Scans arbitrary text and returns the set of tokens that look
    like promo codes, using shape-based detection only. No keyword
    gating — this runs on every description regardless of phrasing.
    """
    if not text:
        return set()

    tokens = TOKEN_PATTERN.findall(text)
    return {t for t in tokens if is_candidate_code(t)}


def compare_variants(product_id, incoming_variants):
    """
    incoming_variants: list of dicts with keys
        variant_id, product_id, title, price, available
    """
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
    """
    Detects two independent things:
      1. Any new candidate code appearing that wasn't there before
         (NEW_DISCOUNT_CODE) — shape-based, no keyword requirement.
      2. Any other description change with no new code (DESCRIPTION_CHANGED)
         — still surfaced, since a code could be phrased in a way that
         doesn't trip the shape detector, and you should see the raw
         text regardless.
    """
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

    # Always also surface the raw change, even if a code was found,
    # in case the shape detector missed something in the same edit.
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