import time
import requests

MAX_RETRIES = 3
BACKOFF_SECONDS = 2

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
}

def fetch_page(url):
    """
    Fetches a single page with retry-on-failure. Raises only after
    exhausting retries — a transient network blip should not kill
    the whole product list.
    """
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=30, headers=HEADERS)
            if response.status_code == 429:
                print("429 rate limit hit -- aborting cycle, will resume next cycle.")
                raise requests.exceptions.RequestException("429 rate limited (no retry)")
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if "429" in str(e):
                raise
            if attempt < MAX_RETRIES:
                wait = BACKOFF_SECONDS * attempt
                print(f"Page fetch failed (attempt {attempt}/{MAX_RETRIES}): {e}. Retrying in {wait}s.")
                time.sleep(wait)

    raise last_error


def fetch_products(base_url):
    """
    Pages are fetched with a deliberate pause between requests.
    Shopify's storefront rate limiter is a leaky bucket (~2 req/s
    sustained); firing all ~10 pages back-to-back drains it in one
    burst and triggers 429s that then break every subsequent cycle.
    0.7s between pages keeps us safely under the refill rate while
    adding only ~7s to a full crawl.
    """
    import time as _time
    products = []
    page = 1

    while True:
        url = f"{base_url}/products.json?limit=250&page={page}"

        data = fetch_page(url)
        batch = data.get("products", [])

        if not batch:
            break

        products.extend(batch)
        page += 1
        _time.sleep(0.7)

    return products
