#!/usr/bin/env python3
"""
Brokerage Ring — live quote server

Serves brokerage_ring.html and feeds it your holdings with the latest prices,
so the ring drifts with the market through the trading day.

    python brokerage_server.py        →  http://localhost:8889

Holdings come from holdings.csv next to this script (columns: ticker,shares).
Copy holdings.example.csv to get started. If the file is missing, a sample
portfolio is used so you can see it working.

Quotes:
  QUOTES_PROVIDER=yahoo    (default) Yahoo Finance chart API. No key; unofficial,
                           and some exchanges' quotes are delayed. Includes today's
                           volume and the 20-day average volume the ring uses for
                           slice thickness.
  QUOTES_PROVIDER=finnhub  Finnhub /quote. Set FINNHUB_API_KEY. Free tier allows
                           60 calls/min, so polling slows down for big portfolios.
                           No volume, so slices keep their normal thickness.
  QUOTES_PROVIDER=sim      Random-walk prices, for trying it out offline.

After each close the server appends that day's prices to brokerage_history.csv,
which the page's Daily replay mode reads back.
"""

import csv
import math
import os
import random
import threading
import time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import requests
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

# ── Config ─────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_CSV = os.path.join(HERE, 'holdings.csv')
HISTORY_CSV = os.path.join(HERE, 'brokerage_history.csv')
PORT = int(os.environ.get('BROKERAGE_PORT', 8889))
FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY', '')
PROVIDER = os.environ.get('QUOTES_PROVIDER', 'finnhub' if FINNHUB_API_KEY else 'yahoo').lower()
POLL_SECONDS_OPEN = 15      # how often to fetch quotes while the market is open
POLL_SECONDS_CLOSED = 300   # and while it's closed
VOL_LOOKBACK_DAYS = 20      # days in the average-volume baseline
PAGE_POLL_SECONDS = 5       # how often the page asks this server for the latest state

ET = ZoneInfo('America/New_York')
OPEN, CLOSE = dtime(9, 30), dtime(16, 0)

SAMPLE_HOLDINGS = [
    ('VTI', 140), ('NVDA', 95), ('AAPL', 60), ('MSFT', 28), ('AMZN', 45), ('GOOGL', 50),
    ('META', 12), ('BRK.B', 20), ('JPM', 30), ('COST', 8), ('LLY', 9), ('XOM', 40),
    ('SCHD', 180), ('AMD', 40), ('TSLA', 18), ('V', 22),
]


def market_open(now=None):
    """Regular US session, weekdays 9:30–16:00 ET. Exchange holidays are not handled."""
    now = now or datetime.now(ET)
    return now.weekday() < 5 and OPEN <= now.time() < CLOSE


def load_holdings():
    """Returns ([(ticker, shares)], label). Falls back to the sample book."""
    if not os.path.exists(HOLDINGS_CSV):
        return SAMPLE_HOLDINGS, 'sample holdings (add holdings.csv)'
    rows = []
    with open(HOLDINGS_CSV, newline='', encoding='utf-8-sig') as f:
        for r in csv.DictReader(f):
            r = {k.strip().lower(): (v or '').strip() for k, v in r.items() if k}
            ticker = (r.get('ticker') or r.get('symbol') or '').upper()
            try:
                shares = float((r.get('shares') or r.get('quantity') or '0').replace(',', ''))
            except ValueError:
                continue
            if ticker and not ticker.startswith('#') and shares > 0:
                rows.append((ticker, shares))
    return rows, 'holdings.csv'


# ── Quote providers: each returns {ticker: {price, prev_close, volume, avg_volume}} ─
# volume is shares traded so far today and avg_volume the average full-day volume
# over the last VOL_LOOKBACK_DAYS sessions; either may be None when unavailable.
session = requests.Session()
session.headers['User-Agent'] = 'Mozilla/5.0 (brokerage-ring)'


avg_volume_cache = {}  # ticker -> (date, average daily volume)


def yahoo_chart(ticker, **params):
    sym = ticker.replace('.', '-')  # BRK.B → BRK-B
    r = session.get(f'https://query1.finance.yahoo.com/v8/finance/chart/{sym}', params=params, timeout=10)
    r.raise_for_status()
    return r.json()['chart']['result'][0]


def yahoo_avg_volume(ticker):
    """Average full-day volume over the last VOL_LOOKBACK_DAYS sessions before today, cached per day."""
    today = datetime.now(ET).date()
    if ticker in avg_volume_cache and avg_volume_cache[ticker][0] == today:
        return avg_volume_cache[ticker][1]
    res = yahoo_chart(ticker, range='3mo', interval='1d')
    stamps = res.get('timestamp') or []
    vols = (res.get('indicators', {}).get('quote') or [{}])[0].get('volume') or []
    past = [v for ts, v in zip(stamps, vols)
            if v and datetime.fromtimestamp(ts, ET).date() < today][-VOL_LOOKBACK_DAYS:]
    avg = sum(past) / len(past) if past else None
    avg_volume_cache[ticker] = (today, avg)
    return avg


def quotes_yahoo(tickers):
    out = {}
    for t in tickers:
        res = yahoo_chart(t, range='1d', interval='1m')
        meta = res['meta']
        price = meta.get('regularMarketPrice')
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        volume = meta.get('regularMarketVolume')
        if volume is None:  # fall back to adding up today's one-minute bars
            bars = (res.get('indicators', {}).get('quote') or [{}])[0].get('volume') or []
            volume = sum(v for v in bars if v) or None
        try:
            avg = yahoo_avg_volume(t)
        except Exception:
            avg = None
        if price and prev:
            out[t] = {'price': float(price), 'prev_close': float(prev), 'volume': volume, 'avg_volume': avg}
    return out


def quotes_finnhub(tickers):
    out = {}
    for t in tickers:
        r = session.get('https://finnhub.io/api/v1/quote',
                        params={'symbol': t, 'token': FINNHUB_API_KEY}, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get('c') and j.get('pc'):
            out[t] = {'price': float(j['c']), 'prev_close': float(j['pc']), 'volume': None, 'avg_volume': None}
    return out


class Sim:
    """Random-walk prices and volume around made-up closes, for trying the page offline."""
    def __init__(self):
        self.prev, self.px, self.vol, self.avg, self.busy = {}, {}, {}, {}, {}

    def __call__(self, tickers):
        step = POLL_SECONDS_OPEN / 23400  # fraction of a 6.5-hour session per poll
        for t in tickers:
            if t not in self.px:
                self.prev[t] = self.px[t] = random.uniform(40, 600)
                self.avg[t] = random.uniform(2e6, 60e6)
                self.busy[t] = math.exp(random.gauss(0, .45))
                self.vol[t] = 0.0
            r = random.gauss(0, .015) * math.sqrt(step)
            self.px[t] *= 1 + r
            self.vol[t] += self.avg[t] * step * self.busy[t] * (1 + 150 * abs(r))
        return {t: {'price': self.px[t], 'prev_close': self.prev[t], 'volume': self.vol[t],
                    'avg_volume': self.avg[t]} for t in tickers}


PROVIDERS = {
    'yahoo': (quotes_yahoo, 'Yahoo Finance (may be delayed)'),
    'finnhub': (quotes_finnhub, 'Finnhub'),
    'sim': (Sim(), 'simulated prices'),
}
if PROVIDER not in PROVIDERS:
    raise SystemExit(f'Unknown QUOTES_PROVIDER {PROVIDER!r}; use yahoo, finnhub or sim.')
fetch_quotes, PROVIDER_LABEL = PROVIDERS[PROVIDER]


# ── State + poller ─────────────────────────────────────────────────────────
lock = threading.Lock()
state = {
    'holdings': [], 'holdings_label': '', 'quotes_at': None, 'error': None,
    'session': [], 'session_date': None,
}


def save_close(day, holdings):
    """Append one row per holding for `day` to the history file, once per day."""
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline='', encoding='utf-8') as f:
            if any(r.get('date') == day for r in csv.DictReader(f)):
                return
    new = not os.path.exists(HISTORY_CSV)
    with_volume = new
    if not new:  # files from before volume was recorded keep their four columns
        with open(HISTORY_CSV, newline='', encoding='utf-8') as f:
            with_volume = 'volume' in (next(csv.reader(f), None) or [])
    with open(HISTORY_CSV, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['date', 'ticker', 'shares', 'price', 'volume'])
        for h in holdings:
            row = [day, h['ticker'], h['shares'], round(h['price'], 4)]
            w.writerow(row + [int(h['volume']) if h.get('volume') else ''] if with_volume else row)


def poll_once():
    book, label = load_holdings()
    tickers = [t for t, _ in book]
    quotes = fetch_quotes(tickers)
    missing = [t for t in tickers if t not in quotes]
    holdings = [{'ticker': t, 'shares': s, **quotes[t]} for t, s in book if t in quotes]
    now = datetime.now(ET)
    is_open = PROVIDER == 'sim' or market_open(now)
    with lock:
        state['holdings'], state['holdings_label'] = holdings, label
        state['quotes_at'] = int(time.time() * 1000)
        state['error'] = f'No quote for {", ".join(missing)}' if missing else None
        today = now.date().isoformat()
        if state['session_date'] != today:
            state['session'], state['session_date'] = [], today
        if is_open and holdings:
            total = sum(h['shares'] * h['price'] for h in holdings)
            state['session'].append([state['quotes_at'], round(total, 2)])
    if holdings and PROVIDER != 'sim' and now.weekday() < 5 and now.time() >= CLOSE:
        save_close(now.date().isoformat(), holdings)


def poller():
    while True:
        try:
            poll_once()
        except Exception as e:  # keep serving the last good quotes
            with lock:
                state['error'] = f'Quote fetch failed: {e}'
        wait = POLL_SECONDS_OPEN if (PROVIDER == 'sim' or market_open()) else POLL_SECONDS_CLOSED
        if PROVIDER == 'finnhub':  # stay under 60 calls/minute
            wait = max(wait, math.ceil(len(state['holdings'] or [0]) * 60 / 55))
        time.sleep(wait)


# ── API ────────────────────────────────────────────────────────────────────
app = FastAPI()


@app.get('/api/portfolio')
def portfolio():
    with lock:
        return JSONResponse({
            'asof': int(time.time() * 1000),
            'market_open': PROVIDER == 'sim' or market_open(),
            'provider': PROVIDER, 'provider_label': PROVIDER_LABEL,
            'holdings': state['holdings'], 'holdings_label': state['holdings_label'],
            'quotes_at': state['quotes_at'], 'session': state['session'],
            'error': state['error'], 'page_poll_seconds': PAGE_POLL_SECONDS,
        })


@app.get('/api/history', response_class=PlainTextResponse)
def history():
    if not os.path.exists(HISTORY_CSV):
        return PlainTextResponse('', status_code=404)
    with open(HISTORY_CSV, encoding='utf-8') as f:
        return f.read()


@app.get('/', response_class=HTMLResponse)
def index():
    with open(os.path.join(HERE, 'brokerage_ring.html'), encoding='utf-8') as f:
        return f.read()


if __name__ == '__main__':
    threading.Thread(target=poller, daemon=True).start()
    print(f'Brokerage Ring on http://localhost:{PORT}  ·  quotes: {PROVIDER_LABEL}')
    uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning')
