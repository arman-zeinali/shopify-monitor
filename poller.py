import time
import requests

MAX_RETRIES = 3
BACKOFF_SECONDS = 2


def fetch_page(url):
    """
    Fetches a single page with retry-on-failure. Raises only after
    exhausting retries — a transient network blip should not kill
    the whole product list.
    """
    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            last_error = e
            if attempt < MAX_RETRIES:
                wait = BACKOFF_SECONDS * attempt
                print(f"Page fetch failed (attempt {attempt}/{MAX_RETRIES}): {e}. Retrying in {wait}s.")
                time.sleep(wait)

    raise last_error


def fetch_products(base_url):
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

    return products