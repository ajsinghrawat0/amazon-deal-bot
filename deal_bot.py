"""
Amazon India price-drop bot -> Telegram channel.

How it works (every run):
  1. Read the product list from products.csv (ASIN + optional target price).
  2. Get the current Amazon.in price of every product (free Amazon
     Creators API first, Keepa as a paid backup).
  3. Compare with the price saved last time (state.json).
  4. If the price dropped enough, post a deal to the Telegram channel
     with an "Buy Now" button that carries your Amazon affiliate tag.
  5. Save the new prices to state.json for the next run.

Settings come from environment variables (see README.md).
"""

import csv
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- settings
# Price source 1 (free): Amazon Creators API - needs 10+ sales in last 30 days
AMAZON_CLIENT_ID = os.getenv("AMAZON_CLIENT_ID", "")
AMAZON_CLIENT_SECRET = os.getenv("AMAZON_CLIENT_SECRET", "")
# Price source 2 (paid backup): Keepa
KEEPA_API_KEY = os.getenv("KEEPA_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL = os.getenv("TELEGRAM_CHANNEL", "")      # e.g. @mydealschannel
AMAZON_TAG = os.getenv("AMAZON_TAG", "")                  # e.g. yourtag-21

MIN_DROP_PERCENT = float(os.getenv("MIN_DROP_PERCENT", "2"))   # post only if drop >= 2%
MIN_DROP_RUPEES = float(os.getenv("MIN_DROP_RUPEES", "100"))   # ...and drop >= ₹100
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "8"))  # avoid spamming the channel
REPOST_COOLDOWN_HOURS = float(os.getenv("REPOST_COOLDOWN_HOURS", "24"))
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"               # 1 = print posts, don't send

# Auto-discovery: the bot searches Amazon with the keywords in searches.csv
# once a day and tracks the results (on top of products.csv).
SEARCHES_FILE = os.getenv("SEARCHES_FILE", "searches.csv")
MAX_AUTO_PRODUCTS = int(os.getenv("MAX_AUTO_PRODUCTS", "1100"))
DISCOVERY_EVERY_HOURS = float(os.getenv("DISCOVERY_EVERY_HOURS", "24"))
AUTO_MIN_DROP_PERCENT = float(os.getenv("AUTO_MIN_DROP_PERCENT", "5"))  # stricter for auto products
MIN_RATING = int(os.getenv("MIN_RATING", "4"))   # only products rated 4+ stars

PRODUCTS_FILE = os.getenv("PRODUCTS_FILE", "products.csv")
STATE_FILE = os.getenv("STATE_FILE", "state.json")

KEEPA_DOMAIN_INDIA = 10        # Keepa's code for amazon.in
KEEPA_BATCH = 100              # Keepa allows up to 100 ASINs per request
IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------- helpers
def rupees(amount: float) -> str:
    """Format in Indian style: 123456 -> ₹1,23,456"""
    amount = int(round(amount))
    s = str(abs(amount))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        s = ",".join(groups) + "," + tail
    return ("-₹" if amount < 0 else "₹") + s


def short_title(title: str, limit: int = 75) -> str:
    """Amazon titles are very long. Keep the useful front part, cut at a natural break."""
    t = " ".join((title or "").split())
    if len(t) <= limit:
        return t
    cuts = [t.find(sep, 40) for sep in (" | ", " - ", ", ", " (", " with ", " for ")]
    cuts = [i for i in cuts if 0 < i <= limit]
    if cuts:
        return t[:min(cuts)].strip()
    return t[:limit].rsplit(" ", 1)[0].strip()


def affiliate_link(asin: str) -> str:
    link = f"https://www.amazon.in/dp/{asin}"
    return f"{link}?tag={AMAZON_TAG}" if AMAZON_TAG else link


def now_ist() -> datetime:
    return datetime.now(IST)


def load_products(path: str) -> list:
    products = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            asin = (row.get("asin") or "").strip().upper()
            if not asin or asin.startswith("#"):
                continue
            target = (row.get("target_price") or "").strip().replace(",", "")
            products.append({
                "asin": asin,
                "name": (row.get("name") or "").strip(),
                "target_price": float(target) if target else None,
                "note": (row.get("note") or "").strip(),
                "link": (row.get("link") or "").strip(),   # optional link.amazon / amzn.to short link
            })
    return products


def load_state(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False, sort_keys=True)


# ---------------------------------------------------------------- Keepa
def _last_valid(csv_series):
    """Keepa history is [time, price, time, price, ...]; take the newest price."""
    if not csv_series or len(csv_series) < 2:
        return None
    value = csv_series[-1]
    return value if value is not None and value > 0 else None


def parse_keepa_product(p: dict) -> dict:
    """Pull title, current price (₹), and image from one Keepa product."""
    price_paise = None
    current = (p.get("stats") or {}).get("current") or []
    # index 0 = sold by Amazon, index 1 = lowest new offer (any seller)
    for idx in (0, 1):
        if len(current) > idx and current[idx] is not None and current[idx] > 0:
            price_paise = current[idx]
            break
    if price_paise is None:
        series = p.get("csv") or []
        for idx in (0, 1):
            if len(series) > idx:
                price_paise = _last_valid(series[idx])
                if price_paise:
                    break

    image = None
    imgs = p.get("images")
    if isinstance(imgs, list) and imgs:
        first = imgs[0]
        if isinstance(first, dict):
            image = first.get("l") or first.get("m")
        elif isinstance(first, str):
            image = first
    if not image and p.get("imagesCSV"):
        image = p["imagesCSV"].split(",")[0]
    if image and not image.startswith("http"):
        image = f"https://m.media-amazon.com/images/I/{image}"

    return {
        "asin": p.get("asin"),
        "title": p.get("title") or "",
        "price": price_paise / 100 if price_paise else None,   # paise -> rupees
        "mrp": None,
        "image": image,
    }


def fetch_prices(asins: list) -> dict:
    results = {}
    for i in range(0, len(asins), KEEPA_BATCH):
        batch = asins[i:i + KEEPA_BATCH]
        r = requests.get(
            "https://api.keepa.com/product",
            params={
                "key": KEEPA_API_KEY,
                "domain": KEEPA_DOMAIN_INDIA,
                "asin": ",".join(batch),
                "stats": 1,
            },
            timeout=60,
        )
        data = r.json()
        if r.status_code != 200 or "error" in data:
            raise RuntimeError(f"Keepa error ({r.status_code}): {data.get('error', data)}")
        print(f"Keepa: {len(batch)} products, tokens left {data.get('tokensLeft')}")
        for p in data.get("products") or []:
            info = parse_keepa_product(p)
            if info["asin"]:
                results[info["asin"]] = info
    return results


# ---------------------------------------------------------------- Amazon Creators API
AMAZON_TOKEN_URL = "https://api.amazon.co.uk/auth/o2/token"   # India uses the EU endpoint
AMAZON_ITEMS_URL = "https://creatorsapi.amazon/catalog/v1/getItems"
AMAZON_BATCH = 10                                               # max 10 ASINs per request
AMAZON_SEARCH_URL = "https://creatorsapi.amazon/catalog/v1/searchItems"


def _get(d, *keys):
    """Safe nested lookup that accepts camelCase or PascalCase keys."""
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k, d.get(k[:1].upper() + k[1:]))
    return d


def amazon_token() -> str:
    r = requests.post(AMAZON_TOKEN_URL, json={
        "grant_type": "client_credentials",
        "client_id": AMAZON_CLIENT_ID,
        "client_secret": AMAZON_CLIENT_SECRET,
        "scope": "creatorsapi::default",
    }, timeout=30)
    data = r.json()
    if "access_token" not in data:
        raise RuntimeError(f"Amazon login failed ({r.status_code}): {data}")
    return data["access_token"]


def parse_amazon_item(item: dict) -> dict:
    offers = _get(item, "offersV2") or _get(item, "offers") or {}
    listings = _get(offers, "listings") or []
    listing = next((l for l in listings if _get(l, "isBuyBoxWinner")), listings[0] if listings else {})
    price = _get(listing, "price", "money", "amount")
    mrp = _get(listing, "price", "savingBasis", "money", "amount")
    image = (_get(item, "images", "primary", "large", "url")
             or _get(item, "images", "primary", "medium", "url"))
    return {
        "asin": _get(item, "asin"),
        "title": _get(item, "itemInfo", "title", "displayValue") or "",
        "price": float(price) if price else None,
        "mrp": float(mrp) if mrp else None,
        "badge": _get(listing, "dealDetails", "badge") or "",
        "image": image,
    }


def amazon_headers() -> dict:
    return {
        "Authorization": f"Bearer {amazon_token()}",
        "Content-Type": "application/json",
        "x-marketplace": "www.amazon.in",
    }


def amazon_post(url: str, headers: dict, body: dict):
    """POST with a polite retry when Amazon says 'too many requests'."""
    for attempt in range(4):
        r = requests.post(url, headers=headers, json=body, timeout=60)
        if r.status_code != 429:
            return r
        time.sleep(2 * (attempt + 1))
    return r


def search_amazon(headers: dict, row: dict, limit: int = 10) -> list:
    """Run one keyword search. Returns up to `limit` (asin, title), most relevant first."""
    found = []
    pages = max(1, -(-limit // 10))
    for page in range(1, pages + 1):
        body = {
            "keywords": row["keywords"],
            "searchIndex": row["category"] or "All",
            "itemCount": min(10, limit - len(found)),
            "itemPage": page,
            "marketplace": "www.amazon.in",
            "partnerTag": AMAZON_TAG,
            "condition": "New",
            "minReviewsRating": MIN_RATING,
            "resources": ["itemInfo.title"],
        }
        if row["min_price"]:
            body["minPrice"] = int(row["min_price"] * 100)   # rupees -> paise
        if row["max_price"]:
            body["maxPrice"] = int(row["max_price"] * 100)
        r = amazon_post(AMAZON_SEARCH_URL, headers, body)
        data = r.json()
        items = _get(data, "searchResult", "items") or []
        if r.status_code != 200 and not items:
            print(f"  search '{row['keywords']}' p{page} failed ({r.status_code}): {str(data)[:200]}")
            break
        for item in items:
            asin = _get(item, "asin")
            if asin:
                found.append((asin, _get(item, "itemInfo", "title", "displayValue") or ""))
        time.sleep(1.1)
        if len(items) < body["itemCount"] or len(found) >= limit:
            break
    return found[:limit]


def load_searches(path: str) -> list:
    if not os.path.exists(path):
        return []
    rows = []
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            kw = (row.get("keywords") or "").strip()
            if not kw or kw.startswith("#"):
                continue
            num = lambda k: float((row.get(k) or "0").strip() or 0)
            rows.append({
                "keywords": kw,
                "category": (row.get("category") or "").strip(),
                "min_price": num("min_price"),
                "max_price": num("max_price"),
                "pages": max(1, min(10, int(num("pages") or 1))),
            })
    return rows


def discover_products(state: dict) -> None:
    """Once a day, refresh the auto product list from searches.csv."""
    meta = state.setdefault("_meta", {})
    last = meta.get("last_discovery")
    try:
        import hashlib
        with open(SEARCHES_FILE, "rb") as f:
            searches_hash = hashlib.md5(f.read()).hexdigest()
    except OSError:
        searches_hash = ""
    if searches_hash != meta.get("searches_hash"):
        last = None   # searches.csv was edited: search again now
    if last:
        hours = (now_ist() - datetime.fromisoformat(last)).total_seconds() / 3600
        if hours < DISCOVERY_EVERY_HOURS:
            return
    searches = load_searches(SEARCHES_FILE)
    if not searches or not (AMAZON_CLIENT_ID and AMAZON_CLIENT_SECRET):
        return
    print(f"Finding products: {len(searches)} searches...")
    headers = amazon_headers()
    auto = {}
    total_weight = sum(r["pages"] for r in searches)
    for row in searches:
        share = max(1, round(MAX_AUTO_PRODUCTS * row["pages"] / total_weight))
        try:
            results = search_amazon(headers, row, share)
        except Exception as e:
            print(f"  search '{row['keywords']}' error: {e}")
            continue
        for asin, title in results:
            if len(auto) >= MAX_AUTO_PRODUCTS:
                break
            auto.setdefault(asin, title[:90])
        print(f"  '{row['keywords']}': {len(results)} found (total {len(auto)})")
    if auto:   # keep the old list if every search failed
        meta["auto_products"] = auto
        meta["last_discovery"] = now_ist().isoformat()
        meta["searches_hash"] = searches_hash
        print(f"Auto list updated: {len(auto)} products")


def fetch_prices_amazon(asins: list) -> dict:
    headers = amazon_headers()
    results = {}
    for i in range(0, len(asins), AMAZON_BATCH):
        batch = asins[i:i + AMAZON_BATCH]
        r = amazon_post(AMAZON_ITEMS_URL, headers, {
            "itemIds": batch,
            "itemIdType": "ASIN",
            "marketplace": "www.amazon.in",
            "partnerTag": AMAZON_TAG,
            "resources": [
                "itemInfo.title",
                "images.primary.large",
                "offersV2.listings.price",
                "offersV2.listings.availability",
                "offersV2.listings.isBuyBoxWinner",
                "offersV2.listings.dealDetails",
            ],
        })
        data = r.json()
        items = _get(data, "itemsResult", "items") or _get(data, "itemResults", "items") or []
        if r.status_code != 200 and not items:
            raise RuntimeError(f"Amazon API error ({r.status_code}): {data}")
        for err in _get(data, "errors") or []:
            print(f"Amazon note: {_get(err, 'message')}")
        for item in items:
            info = parse_amazon_item(item)
            if info["asin"]:
                results[info["asin"]] = info
        time.sleep(1)   # Amazon allows about 1 request per second
    print(f"Amazon API: got {len(results)} of {len(asins)} products")
    return results


def get_prices(asins: list) -> dict:
    """Use the free Amazon API first. If it fails, fall back to Keepa (if set up)."""
    if AMAZON_CLIENT_ID and AMAZON_CLIENT_SECRET:
        try:
            return fetch_prices_amazon(asins)
        except Exception as e:
            print(f"Amazon API failed: {e}")
            if not KEEPA_API_KEY:
                raise
            print("Using Keepa as backup...")
    return fetch_prices(asins)


# ---------------------------------------------------------------- deal logic
def check_deal(product: dict, info: dict, old: dict) -> tuple:
    """
    Decide if this is a deal worth posting.
    Returns (should_post, details_dict).
    """
    price = info["price"]
    # Compare with the "reference" price: the highest price since our last post.
    # This also catches slow drops (₹10 today, ₹20 tomorrow...) once they add up.
    last_price = old.get("ref_price") or old.get("last_price")
    target = product.get("target_price")

    if price is None or last_price is None:
        return False, {}

    drop = last_price - price
    drop_pct = (drop / last_price * 100) if last_price else 0
    min_pct = AUTO_MIN_DROP_PERCENT if product.get("auto") else MIN_DROP_PERCENT
    # Cheap daily-use items rarely drop ₹100, so a 10%+ drop also counts.
    big_drop = drop_pct >= min_pct and (drop >= MIN_DROP_RUPEES or drop_pct >= 10)
    hit_target = target is not None and price <= target < last_price

    if not (big_drop or hit_target):
        return False, {}

    # Don't post the same price again within the cooldown window
    posted_price = old.get("last_posted_price")
    posted_at = old.get("last_posted_at")
    if posted_price is not None and posted_at and price >= posted_price:
        hours = (now_ist() - datetime.fromisoformat(posted_at)).total_seconds() / 3600
        if hours < REPOST_COOLDOWN_HOURS:
            return False, {}

    lowest = old.get("lowest_price")
    return True, {
        "was": last_price,
        "now": price,
        "drop": drop,
        "drop_pct": drop_pct,
        "is_lowest": lowest is None or price < lowest,
        "hit_target": hit_target,
        "mrp": info.get("mrp"),
        "badge": info.get("badge") or "",
        "note": product.get("note") or "",
        "link": product.get("link") or "",
    }


def build_caption(name: str, asin: str, d: dict) -> str:
    """Short deal-channel style:

    Product name at ₹39,000 💥

    📌 Price dropped ₹1,000 (was ₹40,000)
    📌 Limited time deal
    • any extra note from products.csv

    Link: https://www.amazon.in/dp/...
    """
    e = html.escape
    lines = [f"{e(short_title(name))} at {rupees(d['now'])} 💥", ""]
    if d["drop"] > 0:
        low = " · lowest price yet" if d["is_lowest"] else ""
        lines.append(f"📌 <b>Price dropped {rupees(d['drop'])} (was {rupees(d['was'])}){low}</b>")
    if d.get("mrp") and d["mrp"] > d["now"]:
        off = round((d["mrp"] - d["now"]) / d["mrp"] * 100)
        if off >= 10:
            lines.append(f"📌 {off}% off on MRP {rupees(d['mrp'])}")
    if d.get("badge"):
        lines.append(f"📌 {e(d['badge'])}")
    for note in filter(None, (d.get("note") or "").split(";")):
        lines.append(f"• {e(note.strip())}")
    if d.get("link"):   # your own short link from SiteStripe / Amazon app
        link_html = e(d["link"])
    else:               # short-looking text, full affiliate link underneath
        link_html = f'<a href="{e(affiliate_link(asin))}">https://amazon.in/dp/{asin}</a>'
    lines += ["", f"Link: {link_html}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
def telegram(method: str, payload: dict) -> dict:
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/{method}",
        json=payload, timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {data}")
    return data


def post_deal(asin: str, caption: str, image) -> None:
    if DRY_RUN:
        print("---- DRY RUN post ----\n" + caption + "\n")
        return
    if image and len(caption) <= 1000:   # photo on top, text below (Telegram caption limit 1024)
        try:
            telegram("sendPhoto", {
                "chat_id": TELEGRAM_CHANNEL,
                "parse_mode": "HTML",
                "photo": image,
                "caption": caption,
            })
            return
        except RuntimeError as e:
            print(f"Photo failed, sending text instead: {e}")
    telegram("sendMessage", {
        "chat_id": TELEGRAM_CHANNEL,
        "parse_mode": "HTML",
        "text": caption,
        "link_preview_options": {"is_disabled": False, "prefer_small_media": True},
    })


# ---------------------------------------------------------------- main
def run() -> int:
    has_source = (AMAZON_CLIENT_ID and AMAZON_CLIENT_SECRET) or KEEPA_API_KEY
    missing = [n for n, v in [("AMAZON_CLIENT_ID/SECRET or KEEPA_API_KEY", has_source),
                              ("AMAZON_TAG", AMAZON_TAG if AMAZON_CLIENT_ID else "ok"),
                              ("TELEGRAM_BOT_TOKEN", TELEGRAM_BOT_TOKEN),
                              ("TELEGRAM_CHANNEL", TELEGRAM_CHANNEL)] if not v]
    if missing and not DRY_RUN:
        print("Missing settings: " + ", ".join(missing))
        return 1
    if not AMAZON_TAG:
        print("Warning: AMAZON_TAG is empty, links will not earn commission.")

    state = load_state(STATE_FILE)
    products = load_products(PRODUCTS_FILE)
    try:
        discover_products(state)
    except Exception as e:
        print(f"Product search skipped: {e}")
    manual = {p["asin"] for p in products}
    for asin, title in (state.get("_meta", {}).get("auto_products") or {}).items():
        if asin not in manual:
            products.append({"asin": asin, "name": "", "target_price": None, "auto": True})
    print(f"Checking {len(products)} products ({len(manual)} from products.csv)...")

    prices = get_prices([p["asin"] for p in products])
    stamp = now_ist().isoformat()
    deals = []

    for product in products:
        asin = product["asin"]
        info = prices.get(asin)
        if not info:
            continue
        old = state.get(asin, {})
        name = product["name"] or info["title"][:90] or asin

        if info["price"] is None:
            old["in_stock"] = False
            state[asin] = old
            continue

        should_post, details = check_deal(product, info, old)
        if should_post:
            deals.append((details["drop_pct"], asin, name, details, info))

        ref = old.get("ref_price")
        if ref is None or info["price"] > ref:
            old["ref_price"] = info["price"]     # new baseline when price goes up
        old.update({
            "name": name,
            "last_price": info["price"],
            "lowest_price": min(info["price"], old.get("lowest_price") or info["price"]),
            "in_stock": True,
            "checked_at": stamp,
        })
        state[asin] = old

    # Post the biggest drops first; the rest wait for a later run.
    deals.sort(key=lambda d: d[0], reverse=True)
    posts = 0
    for drop_pct, asin, name, details, info in deals[:MAX_POSTS_PER_RUN]:
        try:
            post_deal(asin, build_caption(name, asin, details), info["image"])
        except Exception as e:
            print(f"{asin}: post failed: {e}")
            continue
        posts += 1
        state[asin].update({
            "last_posted_price": info["price"],
            "last_posted_at": stamp,
            "ref_price": info["price"],
        })
        print(f"{asin}: POSTED {rupees(details['was'])} -> {rupees(details['now'])} ({drop_pct:.0f}% off)")
        time.sleep(3)   # stay well inside Telegram's rate limits
    if len(deals) > posts:
        print(f"{len(deals) - posts} more deal(s) waiting for the next run")

    save_state(STATE_FILE, state)
    print(f"Done. Posted {posts} deal(s).")
    return 0


def send_test_post() -> int:
    """python deal_bot.py --test : checks the bot can post in your channel."""
    telegram("sendMessage", {
        "chat_id": TELEGRAM_CHANNEL,
        "text": "✅ Deal bot connected. Price-drop alerts will appear here.",
    })
    print("Test message sent.")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(line_buffering=True)   # show log lines live in GitHub
    sys.exit(send_test_post() if "--test" in sys.argv else run())
