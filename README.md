# Amazon Price-Drop Bot for Telegram

This bot checks Amazon.in prices every hour. When a price drops, it posts the deal to your Telegram channel with the product photo, the old and new price, and a **Buy Now** button that has your affiliate link.

```
🔥 PRICE DROP ALERT

Apple iPhone 15 (128 GB)

🏷️ MRP: ₹79,900
❌ Was: ₹40,000
✅ Now: ₹39,000
📉 You save ₹1,000 (3% off)
⭐ Lowest price we have tracked!

🛒 Buy Now on Amazon
Price as of 06 Nov, 08:46 PM IST. Prices can change anytime.
#ad - we may earn a small commission from this link.
            [ 🛒 Buy Now ]
```

It runs free on GitHub and uses Amazon's free API, so it costs nothing to run.

---

## What you need (one time)

| Item | Where to get it | Cost |
|---|---|---|
| Amazon affiliate tag | affiliate-program.amazon.in (looks like `yourname-21`) | Free |
| Amazon Creators API keys | Associates Central → Tools → Creators API | Free |
| Telegram bot | Telegram → @BotFather → `/newbot` | Free |
| GitHub account | github.com | Free |
| Keepa API key (optional backup) | keepa.com → API | about €49/month |

**About the free Amazon API:** your account needs **10+ sales on amazon.in in the last 30 days**. If your sales drop below that, Amazon pauses the API until sales come back.
- If you also add a Keepa key, the bot switches to Keepa by itself during that pause, so posts never stop.
- Without Keepa, the bot just skips runs until Amazon turns the API back on.

---

## Setup (about 15 minutes)

### 1. Make the Telegram bot
1. Open **@BotFather** in Telegram, send `/newbot`, and pick a name.
2. Copy the **bot token** it gives you (looks like `123456:ABC-xyz...`).
3. Open your deals channel → **Admins** → **Add Admin** → add your bot → turn on **Post Messages**.

### 2. Put the code on GitHub
1. Create a new repository (Private is fine).
2. Upload all the files from this folder, including the `.github/workflows` folder.

### 3. Get your free Amazon API keys
1. Log in to Associates Central → **Tools → Creators API**. Only the main account owner can do this.
2. Create an application, then create **credentials**.
3. Copy the **Client ID** and **Client Secret** and keep them safe. The secret is shown only once.

Tip: make a separate tracking ID for this channel (**Account Settings → Manage Tracking IDs**, for example `asrdeals-21`). Your reports will then show the channel's sales on their own.

### 4. Add your secret keys
In your repository go to **Settings → Secrets and variables → Actions → New repository secret** and add:

| Name | Value |
|---|---|
| `AMAZON_CLIENT_ID` | Client ID from step 3 |
| `AMAZON_CLIENT_SECRET` | Client Secret from step 3 |
| `AMAZON_TAG` | your tracking ID, e.g. `asrdeals-21` |
| `TELEGRAM_BOT_TOKEN` | the token from BotFather |
| `TELEGRAM_CHANNEL` | your channel username, e.g. `@j4fdeals` |
| `KEEPA_API_KEY` | optional backup, leave out if you don't have it |

### 5. Add products
Open `products.csv` and add one line per product:

```
asin,name,target_price
B0ABCD1234,Apple iPhone 15 (128 GB),39000
B0EFGH5678,boAt Airdopes 141,
```

- **asin**: the 10-letter code after `/dp/` in the Amazon link.
- **name**: a short name for the post. Leave it empty to use Amazon's title.
- **target_price**: optional. The bot posts when the price reaches this, even if the drop is small.

### 6. Start it
Go to the **Actions** tab → **Amazon deal bot** → **Run workflow**. After that it runs every hour by itself.

> The **first run only saves prices** and does not post anything. Deals start coming from the next run, when a price is lower than the saved one.

---

## When does it post?

It posts when **both** of these are true:
- The price dropped by at least **2%**
- The price dropped by at least **₹100**

It also posts when the price reaches your **target_price**.

It does **not** post:
- The same price again within 24 hours
- More than 15 deals in one run (so your channel doesn't get spammed)
- Products that are out of stock

Slow drops are counted too. If a price goes down ₹200 today and ₹800 tomorrow, the bot adds them up and posts once the total is big enough.

To change these numbers, edit `MIN_DROP_PERCENT` and `MIN_DROP_RUPEES` in `.github/workflows/deal-bot.yml`.

---

## Test on your computer (optional)

```bash
pip install requests
export TELEGRAM_BOT_TOKEN="..."  TELEGRAM_CHANNEL="@yourchannel"
python deal_bot.py --test          # sends a test message to the channel

export AMAZON_CLIENT_ID="..."  AMAZON_CLIENT_SECRET="..."  AMAZON_TAG="yourname-21"  DRY_RUN=1
python deal_bot.py                 # checks prices and prints posts without sending them
```

---

## Good to know
- **Keep the affiliate disclosure.** Amazon Associates requires the "#ad / we may earn a commission" line, and Amazon also wants the "price as of" time shown, because prices change.
- GitHub sometimes runs scheduled jobs a few minutes late. That's normal.
- `state.json` is the bot's memory of past prices. It updates by itself. Don't delete it, or the bot will start over and skip one round of posts.
