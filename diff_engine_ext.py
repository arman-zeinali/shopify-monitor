"""
Extension module: scans title and variant titles for the same promo
keywords already used elsewhere, using data already present in the feed.
No new network requests.
"""
import re

PROMO_KEYWORDS = [
    "giveaway", "prize", "$1", "win", "winner", "contest", "coupon",
    "discount code", "promo code", "july", "instagram", "snatch", "annual",
]


def check_title_keywords(text, keywords=None):
    keywords = keywords or PROMO_KEYWORDS
    lower = (text or "").lower()
    matched = []
    for kw in keywords:
        k = kw.lower()
        if k == "$1":
            if re.search(r"\$\s?1(?![\d,])", lower):
                matched.append(kw)
        elif re.search(r"\b" + re.escape(k) + r"\b", lower):
            matched.append(kw)
    return matched


def compare_title_and_variants(old_title, new_title, old_variant_titles, new_variant_titles):
    events = []

    old_matches = set(check_title_keywords(old_title))
    new_matches = set(check_title_keywords(new_title))
    newly = new_matches - old_matches
    if newly and old_title != new_title:
        events.append({
            "type": "TITLE_KEYWORD_MATCH",
            "field": "title",
            "matched": sorted(newly),
            "text": new_title,
        })

    old_v_matches = set()
    for t in (old_variant_titles or []):
        old_v_matches |= set(check_title_keywords(t))
    for t in (new_variant_titles or []):
        m = set(check_title_keywords(t))
        n = m - old_v_matches
        if n:
            events.append({
                "type": "TITLE_KEYWORD_MATCH",
                "field": "variant_title",
                "matched": sorted(n),
                "text": t,
            })

    return events
