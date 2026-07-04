"""
diff_engine.py

Change detection for the Shopify product monitor.

Two independent detection paths, matching how LDJ actually runs the
giveaway:
  1. Variant-level price/availability changes (the $1 drop path).
  2. Description text changes, specifically new discount-code-like
     tokens appearing where they weren't before (the hidden-code path).

Both return structured event dicts, not pre-formatted strings, so
message formatting stays decoupled from detection logic.
"""

import difflib
import re

from database import get_variants

DOLLAR_THRESHOLD = 1.00

CODE_TOKEN_PATTERN = re.compile(r'\b[A-Z0-9]{4,15}\b')
CODE_CONTEXT_KEYWORDS = ("code", "off", "discount", "promo", "coupon")


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


def _find_codes(text):
    """
    Heuristic only. Requires a nearby keyword (code/off/discount/etc.)
    somewhere in the text before treating all-caps tokens as candidate
    codes. Will produce false positives — alerts include surrounding
    context specifically so you can eyeball it in under two seconds.
    """
    if not text:
        return set()

    lowered = text.lower()
    if not any(kw in lowered for kw in CODE_CONTEXT_KEYWORDS):
        return set()

    return set(CODE_TOKEN_PATTERN.findall(text))


def compare_description(old_text, new_text):
    events = []

    if old_text == new_text:
        return events

    added_segments = _diff_added_text(old_text, new_text)
    added_text = " ".join(added_segments)

    old_codes = _find_codes(old_text)
    new_codes = _find_codes(new_text)
    new_only_codes = new_codes - old_codes

    if new_only_codes:
        for code in new_only_codes:
            events.append({
                "type": "NEW_DISCOUNT_CODE",
                "code": code,
                "context": (added_text or new_text)[:200],
            })
    else:
        events.append({
            "type": "DESCRIPTION_CHANGED",
            "diff_snippet": added_text[:300] if added_text else "(text removed or reordered, no new keywords detected)",
        })

    return events


def compare_product(product_id, incoming_variants, old_description_text, new_description_text):
    events = []
    events.extend(compare_variants(product_id, incoming_variants))
    events.extend(compare_description(old_description_text, new_description_text))
    return events