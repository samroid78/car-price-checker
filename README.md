# 🚗 UK Car Price Checker

A Flask web app that compares **trade buying prices** vs **retail listing prices** for any UK registered vehicle.

## What it does

| Source | Type | What it gives you |
|---|---|---|
| **WBAC** (We Buy Any Car) | Trade buyer | Instant guaranteed offer |
| **Carwow** | Trade auction | Multiple dealer bids (highest offer) |
| **Motorway** | Trade buyer | Dealer marketplace valuation |
| **AutoTrader UK** | Retail listings | What the same car sells for publicly |
| **DVLA** | Official record | Make, colour, fuel, tax & MOT status |

## Features

- Enter registration + mileage → get **live trade and retail prices** in one view
- AutoTrader search uses **exact filters**: same make, model, year, fuel, gearbox, body type, ±10k miles
- Live **screenshot of AutoTrader results** embedded directly in the app
- **Match confidence scoring** (High/Medium/Low) for each comparable listing
- **Price gap insight**: how much below retail the trade offers are
- Session persistence for Motorway (skip re-registration after first run)

## Setup

### Requirements
- Python 3.11+
- Google Chrome installed (for AutoTrader Cloudflare bypass)

### Install

```bash
pip install -r requirements.txt
playwright install chromium
```

### Configure

Create a `.env` file:
```
FIRECRAWL_API_KEY=your_key_here
```

Get a free Firecrawl API key at [firecrawl.dev](https://firecrawl.dev)

### Run

```bash
python app.py
```

Then open **http://localhost:5000**

Or double-click **`start.bat`** on Windows.

## Usage

1. Enter the **vehicle registration** (e.g. `FG63 ACY`)
2. Enter **current mileage**
3. Enter **make** and **model** (DVLA doesn't provide the model — needed for AutoTrader search)
4. Click **"Get Trade & Retail Prices"**

Results appear in ~60 seconds covering all 4 sources in parallel.

## Architecture

```
app.py                          Flask backend + DVLA lookup
wbac_scraper.py                 WBAC Playwright automation
services/
  autotrader_service.py         AutoTrader via real Chrome + Cloudflare handling
  motorway_service.py           Motorway form flow + session save/reuse
  carwow_service.py             Carwow SourcePoint iframe + form automation
  vehicle_match_service.py      Confidence scoring + mileage/year filtering
templates/
  index.html                    Single-page frontend
```

## Deployment

> ⚠️ **Vercel is NOT recommended** — Playwright requires ~300MB browser binaries which exceed Vercel's 50MB serverless limit. The DVLA lookup will work but all scraping features will fail.

**Recommended platforms (Python + long-running processes):**

| Platform | Command |
|---|---|
| **Railway** | Connect GitHub repo → deploy |
| **Render** | New Web Service → Python → `python app.py` |
| **Fly.io** | `fly launch` → `fly deploy` |

For any of these, set the `FIRECRAWL_API_KEY` environment variable in the platform dashboard.

## Tech stack

- **Backend**: Python 3.11, Flask
- **Browser automation**: Playwright (Chromium)
- **Web scraping**: Firecrawl (Carwow, Motors.co.uk fallback)
- **HTML parsing**: BeautifulSoup4
- **Frontend**: Vanilla JS, CSS (no frameworks)
