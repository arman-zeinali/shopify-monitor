# shopify-monitor

A set of Python services that watched a luxury resale store's Shopify catalog (about 2,700 products) around the clock and sent Telegram alerts the moment anything changed. Ran unattended on AWS EC2 for about a month in summer 2026.

## Why I built it

The store ran surprise giveaways hidden inside its own product listings. Whoever found it first won. Over a few rounds the giveaway showed up in three different forms:

- a product price dropped to $1
- a checkout code added to an existing product's description, at a normal price
- a riddle printed inside a product photo

No one can watch thousands of listings by hand, so each form got its own detector.

## How it works

Four independent services, each run by systemd so a crash in one can't take down the others:

| Service | What it watches | How often |
|---|---|---|
| `app.py` | Every product and variant: new products, price changes, availability, description edits | every 30s |
| `site_monitor.py` | Non-product pages (homepage banner, FAQs, About) for any text change | every 60s |
| `image_monitor.py` | New product images from the main luxury brands, sent straight to Telegram | every 2 min |
| `ocr_monitor.py` | Reads the text in every new product image with OCR | every 15 min |

Supporting pieces:

- `poller.py` pulls the store's public Shopify product feed
- `database.py` stores products, variants and image history in SQLite, so each run compares against the last
- `diff_engine.py` compares old and new product data and flags anything that looks like a discount code
- `notifier.py` sends alerts (text and photo groups) to multiple Telegram recipients, with retries
- `backfill_ocr.py` and `scan_existing.py` are one-off scans of the existing catalog

## Design decisions

**Catch codes without knowing the words.** A keyword list misses a code nobody guessed in advance. `diff_engine.py` treats any token that doesn't read like normal prose (all caps, mixed letters and digits) as a candidate code. The store's own 6-character SKU format is filtered out based on what was observed in the real catalog, to keep false alarms down.

**Stop relying on keywords at all.** After the store said the next giveaway wouldn't be findable by searching for words like "giveaway" or "$1", keyword matching was a dead end. `ocr_monitor.py` now alerts on a second signal: how much text an image contains. A handbag photo has a few words at most (a brand stamp). An announcement graphic has many. So the alert is "this image contains writing", not "this image contains a word I guessed".

**Collect now, analyse later.** Every OCR result is stored whether it alerts or not, so the text can be searched afterwards without downloading anything again. `backfill_ocr.py --report` ranks every scanned image by how much text it holds.

**Be a polite client.** Images come from Shopify's CDN, not the store's site. Requests are spaced out with random gaps and capped per cycle.

## What happened

It caught both July giveaway items in the database. One was the $1 price. The other was a normal price with a checkout code in the description, which a price-only check would have missed.

It also missed one. The riddle image reached my Telegram alerts, but OCR couldn't read pale serif text on a cream background, so it was never flagged as text-heavy. Detection worked; extraction failed. The fix I'd make next is image preprocessing (contrast boost, thresholding) before OCR, plus a fallback to a vision model for low-confidence images.

## How I built it

I'm not an engineer. I wrote the specs, made the design calls, tested against the live store and debugged in production. Claude Code wrote the Python. I started this with no technical background.

## Run it

Requires Python 3 and Tesseract (`apt install tesseract-ocr`).

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml   # add your Telegram bot token and chat IDs
python app.py
```

Example systemd unit files for all four services are in `systemd/`.
