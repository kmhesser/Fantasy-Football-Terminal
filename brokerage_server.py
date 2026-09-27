#!/usr/bin/env python3
"""
Brokerage Ring — live quote server

Serves brokerage_ring.html and feeds it your holdings with the latest prices,
so the ring drifts with the market through the trading day.

    python brokerage_server.py            →  http://localhost:8889
    python brokerage_server.py --check    →  fetch once, print a table, exit

Holdings
  holdings.csv next to this script, or any file named by HOLDINGS_FILE.
  Either the simple format (ticker,shares; a CASH row holds your cash balance)
  or a positions export from your brokerage: the header row is found
  automatically, Symbol/Quantity/Market Value style columns are recognised,
  money-market and "Cash & Cash Investments" rows count as cash, totals rows
  are skipped and the same symbol across accounts is added together. The file
  is re-read on every poll, so saving a fresh export updates the ring.
  Without a file, a sample portfolio is used.

Quotes (QUOTES_PROVIDER)
  yahoo    (default) Yahoo Finance chart API. No key; unofficial, and some
           exchanges' quotes are delayed. Includes pre/after-market prices,
           today's volume and the 20-day average volume.
  finnhub  Finnhub /quote with FINNHUB_API_KEY. Free tier allows 60 calls/min,
           so polling slows down for big portfolios. No volume or extended hours.
  sim      Random-walk prices, for trying it out offline. SIM_PHASE=pre, post,
           closed or half forces a market phase for testing.

BENCHMARK (default SPY; empty to turn off) is fetched alongside your holdings
so the page can show your day against the market.

Market hours follow the NYSE calendar: weekends, the ten regular holidays and
the 1:00 PM early closes. One-off closures (e.g. national days of mourning)
are not known in advance and are not handled.

After each close the day's prices and volume are appended to
brokerage_history.csv, which the page's Daily replay mode reads back.
"""

import csv
import io
import math
import os
import random
import sys
import threading
import time
from datetime import date, datetime, timedelta, time as dtime
from zoneinfo import ZoneInfo

import requests

# ── Config ─────────────────────────────────────────────────────────────────
HERE = os.path.dirname(os.path.abspath(__file__))
HOLDINGS_FILE = os.environ.get('HOLDINGS_FILE') or os.path.join(HERE, 'holdings.csv')
HISTORY_CSV = os.path.join(HERE, 'brokerage_history.csv')
PORT = int(os.environ.get('BROKERAGE_PORT', 8889))
FINNHUB_API_KEY = os.environ.get('FINNHUB_API_KEY', '')
PROVIDER = os.environ.get('QUOTES_PROVIDER', 'finnhub' if FINNHUB_API_KEY else 'yahoo').lower()
BENCHMARK = os.environ.get('BENCHMARK', 'SPY').upper().strip()
SIM_PHASE = os.environ.get('SIM_PHASE', '').lower()
POLL_SECONDS = {'regular': 15, 'pre': 60, 'post': 60, 'closed': 300}
VOL_LOOKBACK_DAYS = 20      # days in the average-volume baseline
PAGE_POLL_SECONDS = 5       # how often the page asks this server for the latest state

ET = ZoneInfo('America/New_York')
PRE_OPEN, OPEN, CLOSE, EARLY_CLOSE, POST_CLOSE = dtime(4), dtime(9, 30), dtime(16), dtime(13), dtime(20)

SAMPLE_HOLDINGS = [
    ('VTI', 140), ('NVDA', 95), ('AAPL', 60), ('MSFT', 28), ('AMZN', 45), ('GOOGL', 50),
    ('META', 12), ('BRK.B', 20), ('JPM', 30), ('COST', 8), ('LLY', 9), ('XOM', 40),
    ('SCHD', 180), ('AMD', 40), ('TSLA', 18), ('V', 22), ('F', 60), ('SOFI', 40), ('PFE', 25), ('T', 20),
]
SAMPLE_CASH = 6500.0


# ── NYSE calendar ──────────────────────────────────────────────────────────
def _easter(y):
    """Gregorian Easter Sunday (anonymous algorithm)."""
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(y, month, (h + l - 7 * m + 114) % 31 + 1)


def _nth_weekday(y, month, weekday, n):
    d = date(y, month, 1)
    d += timedelta((weekday - d.weekday()) % 7)
    return d + timedelta(weeks=n - 1)


def _last_weekday(y, month, weekday):
    d = date(y, month + 1, 1) - timedelta(1) if month < 12 else date(y, 12, 31)
    return d - timedelta((d.weekday() - weekday) % 7)


def _observed(d):
    """Saturday holidays are observed Friday, Sunday holidays Monday."""
    return d - timedelta(1) if d.weekday() == 5 else d + timedelta(1) if d.weekday() == 6 else d


def nyse_holidays(y):
    h = {}
    ny = date(y, 1, 1)
    if ny.weekday() != 5:  # NYSE does not close the Friday before a Saturday New Year's Day
        h[_observed(ny)] = "New Year's Day"
    h[_nth_weekday(y, 1, 0, 3)] = 'Martin Luther King Jr. Day'
    h[_nth_weekday(y, 2, 0, 3)] = "Washington's Birthday"
    h[_easter(y) - timedelta(2)] = 'Good Friday'
    h[_last_weekday(y, 5, 0)] = 'Memorial Day'
    if y >= 2022:
        h[_observed(date(y, 6, 19))] = 'Juneteenth'
    h[_observed(date(y, 7, 4))] = 'Independence Day'
    h[_nth_weekday(y, 9, 0, 1)] = 'Labor Day'
    h[_nth_weekday(y, 11, 3, 4)] = 'Thanksgiving Day'
    h[_observed(date(y, 12, 25))] = 'Christmas Day'
    return h


def nyse_early_closes(y):
    e = set()
    jul3 = date(y, 7, 3)
    if jul3.weekday() < 4:            # July 4 falls Tuesday–Friday
        e.add(jul3)
    e.add(_nth_weekday(y, 11, 3, 4) + timedelta(1))   # day after Thanksgiving
    dec24 = date(y, 12, 24)
    if dec24.weekday() < 4:
        e.add(dec24)
    return e


def market_day(d):
    """(is_trading_day, close_time, holiday_name)"""
    if d.weekday() >= 5:
        return False, None, 'Weekend'
    name = nyse_holidays(d.year).get(d)
    if name:
        return False, None, name
    return True, EARLY_CLOSE if d in nyse_early_closes(d.year) else CLOSE, None


def market_phase(now=None):
    """Returns dict: phase (pre/regular/post/closed), session_minutes, early_close, holiday."""
    now = now or datetime.now(ET)
    trading, close, holiday = market_day(now.date())
    if PROVIDER == 'sim':  # the simulator trades around the clock unless SIM_PHASE says otherwise
        half = SIM_PHASE == 'half'
        return {'phase': SIM_PHASE if SIM_PHASE and not half else 'regular', 'session_minutes': 210 if half else 390,
                'early_close': half, 'holiday': None}
    if not trading:
        return {'phase': 'closed', 'session_minutes': 390, 'early_close': False, 'holiday': holiday}
    minutes = (close.hour * 60 + close.minute) - (9 * 60 + 30)
    t = now.time()
    phase = ('pre' if PRE_OPEN <= t < OPEN else 'regular' if OPEN <= t < close
             else 'post' if close <= t < POST_CLOSE else 'closed')
    return {'phase': phase, 'session_minutes': minutes, 'early_close': close != CLOSE, 'holiday': None}


# ── Holdings ───────────────────────────────────────────────────────────────
SYMBOL_COLS = ('ticker', 'symbol')
QTY_COLS = ('shares', 'quantity', 'qty', 'qty (quantity)')
VALUE_COLS = ('market value', 'current value', 'total value', 'value', 'mkt val (market value)', 'market value ($)')
DESC_COLS = ('description', 'name', 'security description')
SKIP_SYMBOLS = ('ACCOUNT TOTAL', 'TOTAL', 'PENDING ACTIVITY', 'PENDING ACTIVITIES')


def _num(s):
    s = (s or '').replace('$', '').replace(',', '').strip()
    neg = s.startswith('(') and s.endswith(')')
    try:
        v = float(s.strip('()'))
    except ValueError:
        return None
    return -v if neg else v


def parse_holdings(text):
    """Returns ([(ticker, shares)], cash). Accepts ticker,shares files and brokerage exports."""
    lines = text.splitlines()
    head_i = None
    for i, line in enumerate(lines):  # exports often start with an account-name preamble
        cells = [c.strip().strip('"').lower() for c in next(csv.reader([line]), [])]
        if any(c in SYMBOL_COLS for c in cells):
            head_i = i
            break
    if head_i is None:
        raise ValueError('no Symbol or Ticker column found')
    reader = csv.reader(io.StringIO('\n'.join(lines[head_i:])))
    head = [c.strip().strip('"').lower() for c in next(reader)]
    find = lambda names: next((head.index(n) for n in names if n in head), None)
    i_sym, i_qty, i_val, i_desc = find(SYMBOL_COLS), find(QTY_COLS), find(VALUE_COLS), find(DESC_COLS)
    shares, cash = {}, 0.0
    for row in reader:
        cell = lambda i: row[i].strip() if i is not None and i < len(row) else ''
        sym = cell(i_sym).upper()
        desc = cell(i_desc).lower()
        if not sym or sym.startswith('#') or sym in SKIP_SYMBOLS:
            continue
        is_cash = (sym == 'CASH' or sym.startswith('CASH') or sym.endswith('**')
                   or 'money market' in desc or 'cash & cash' in sym.lower())
        if is_cash:
            amount = _num(cell(i_val)) if i_val is not None else None
            if amount is None:
                amount = _num(cell(i_qty))
            cash += amount or 0.0
            continue
        qty = _num(cell(i_qty))
        if qty and qty > 0:
            shares[sym] = shares.get(sym, 0.0) + qty
    return list(shares.items()), cash


def load_holdings():
    """Returns ([(ticker, shares)], cash, label). Falls back to the sample book."""
    if not os.path.exists(HOLDINGS_FILE):
        return SAMPLE_HOLDINGS, SAMPLE_CASH, 'sample holdings (add holdings.csv)'
    with open(HOLDINGS_FILE, encoding='utf-8-sig') as f:
        book, cash = parse_holdings(f.read())
    return book, cash, os.path.basename(HOLDINGS_FILE)


# ── Quote providers: each returns {ticker: {price, prev_close, ext_price, volume, avg_volume}} ─
# price is the regular-session price and ext_price the latest pre/after-market trade;
# volume is shares traded so far today and avg_volume the average full-day volume over
# the last VOL_LOOKBACK_DAYS sessions. Anything unavailable is None.
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
        res = yahoo_chart(t, range='1d', interval='1m', includePrePost='true')
        meta = res['meta']
        price = meta.get('regularMarketPrice')
        prev = meta.get('chartPreviousClose') or meta.get('previousClose')
        q = (res.get('indicators', {}).get('quote') or [{}])[0]
        stamps, closes = res.get('timestamp') or [], q.get('close') or []
        # latest bar outside the regular session = extended-hours price
        reg = (meta.get('currentTradingPeriod') or {}).get('regular') or {}
        ext = None
        for ts, c in zip(reversed(stamps), reversed(closes)):
            if c is None:
                continue
            if reg and not (reg.get('start', 0) <= ts < reg.get('end', 0)):
                ext = float(c)
            break
        volume = meta.get('regularMarketVolume')
        if volume is None:  # fall back to adding up today's regular-session one-minute bars
            volume = sum(v for ts, v in zip(stamps, q.get('volume') or [])
                         if v and (not reg or reg.get('start', 0) <= ts < reg.get('end', 0))) or None
        try:
            avg = yahoo_avg_volume(t)
        except Exception:
            avg = None
        if price and prev:
            out[t] = {'price': float(price), 'prev_close': float(prev), 'ext_price': ext,
                      'volume': volume, 'avg_volume': avg}
    return out


def quotes_finnhub(tickers):
    out = {}
    for t in tickers:
        r = session.get('https://finnhub.io/api/v1/quote',
                        params={'symbol': t, 'token': FINNHUB_API_KEY}, timeout=10)
        r.raise_for_status()
        j = r.json()
        if j.get('c') and j.get('pc'):
            out[t] = {'price': float(j['c']), 'prev_close': float(j['pc']), 'ext_price': None,
                      'volume': None, 'avg_volume': None}
    return out


class Sim:
    """Random-walk prices and volume around made-up closes, for trying the page offline."""
    def __init__(self):
        self.prev, self.px, self.ext, self.vol, self.avg, self.busy = {}, {}, {}, {}, {}, {}

    def __call__(self, tickers):
        phase = market_phase()['phase']
        step = POLL_SECONDS['regular'] / 23400  # fraction of a 6.5-hour session per poll
        for t in tickers:
            if t not in self.px:
                self.prev[t] = self.px[t] = self.ext[t] = random.uniform(40, 600)
                self.avg[t] = random.uniform(2e6, 60e6)
                self.busy[t] = math.exp(random.gauss(0, .45))
                self.vol[t] = 0.0
            r = random.gauss(0, .015) * math.sqrt(step)
            if phase == 'regular':
                self.px[t] *= 1 + r
                self.ext[t] = self.px[t]
                self.vol[t] += self.avg[t] * step * self.busy[t] * (1 + 150 * abs(r))
            else:
                self.ext[t] *= 1 + r * 2  # thin extended-hours trading moves more
        return {t: {'price': self.px[t], 'prev_close': self.prev[t],
                    'ext_price': self.ext[t] if phase in ('pre', 'post') else None,
                    'volume': self.vol[t], 'avg_volume': self.avg[t]} for t in tickers}


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
    'holdings': [], 'holdings_label': '', 'benchmark': None, 'quotes_at': None, 'error': None,
    'snaps': [], 'session_date': None, 'market': market_phase(),
}


def save_close(day, holdings, bench):
    """Append one row per holding for `day` to the history file, once per day.
    The benchmark is stored as a $-prefixed ticker with 0 shares."""
    if os.path.exists(HISTORY_CSV):
        with open(HISTORY_CSV, newline='', encoding='utf-8') as f:
            if any(r.get('date') == day for r in csv.DictReader(f)):
                return
    new = not os.path.exists(HISTORY_CSV)
    with_volume = new
    if not new:  # files from before volume was recorded keep their four columns
        with open(HISTORY_CSV, newline='', encoding='utf-8') as f:
            with_volume = 'volume' in (next(csv.reader(f), None) or [])
    rows = [[day, h['ticker'], h['shares'], round(h['price'], 4), int(h['volume']) if h.get('volume') else '']
            for h in holdings]
    if bench:
        rows.append([day, '$' + bench['ticker'], 0, round(bench['price'], 4), ''])
    with open(HISTORY_CSV, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if new:
            w.writerow(['date', 'ticker', 'shares', 'price', 'volume'])
        for row in rows:
            w.writerow(row if with_volume else row[:4])


def poll_once():
    book, cash, label = load_holdings()
    tickers = [t for t, _ in book]
    quotes = fetch_quotes(tickers + ([BENCHMARK] if BENCHMARK and BENCHMARK not in tickers else []))
    missing = [t for t in tickers if t not in quotes]
    holdings = [{'ticker': t, 'shares': s, **quotes[t]} for t, s in book if t in quotes]
    if cash:
        holdings.append({'ticker': 'CASH', 'shares': cash, 'price': 1.0, 'prev_close': 1.0, 'ext_price': None,
                         'volume': None, 'avg_volume': None, 'cash': True})
    bench = {'ticker': BENCHMARK, **quotes[BENCHMARK]} if BENCHMARK in quotes else None
    now = datetime.now(ET)
    mkt = market_phase(now)
    with lock:
        state['holdings'], state['holdings_label'], state['benchmark'] = holdings, label, bench
        state['market'] = mkt
        state['quotes_at'] = int(time.time() * 1000)
        state['error'] = f'No quote for {", ".join(missing)}' if missing else None
        today = now.date().isoformat()
        if state['session_date'] != today:
            state['snaps'], state['session_date'] = [], today
        if mkt['phase'] == 'regular' and holdings:
            # one snapshot per poll: prices and volume for every holding, for rewinding the day
            state['snaps'].append([state['quotes_at'],
                                   {h['ticker']: [round(h['price'], 4), h.get('volume')] for h in holdings if not h.get('cash')},
                                   round(bench['price'], 4) if bench else None])
    trading, close, _ = market_day(now.date())
    if holdings and PROVIDER != 'sim' and trading and now.time() >= close:
        save_close(today, [h for h in holdings if not h.get('cash')], bench)


def poller():
    while True:
        try:
            poll_once()
        except Exception as e:  # keep serving the last good quotes
            with lock:
                state['error'] = f'Quote fetch failed: {e}'
        wait = POLL_SECONDS[state['market']['phase']]
        if PROVIDER == 'finnhub':  # stay under 60 calls/minute
            wait = max(wait, math.ceil((len(state['holdings']) + 1) * 60 / 55))
        time.sleep(wait)


# ── --check: one fetch, printed ────────────────────────────────────────────
def vol_curve(x):
    """Share of a day's volume traded by fraction x of the session (U-shaped; matches the page)."""
    x = max(0.0, min(1.0, x))
    return (x + ((2 * x - 1) ** 3 + 1) / 2) / 2


def check():
    mkt = market_phase()
    print(f'Market: {mkt["phase"]}' + (f' ({mkt["holiday"]})' if mkt['holiday'] else '')
          + (' · early close 1:00 PM' if mkt['early_close'] else '') + f' · quotes: {PROVIDER_LABEL}')
    book, cash, label = load_holdings()
    print(f'Holdings: {label} · {len(book)} positions' + (f' · cash ${cash:,.2f}' if cash else ''))
    tickers = [t for t, _ in book] + ([BENCHMARK] if BENCHMARK else [])
    try:
        q = fetch_quotes(tickers)
    except Exception as e:
        print(f'\nCould not fetch quotes from {PROVIDER_LABEL}: {e}')
        return
    now = datetime.now(ET)
    elapsed = max(15, min(mkt['session_minutes'], now.hour * 60 + now.minute - 570)) if mkt['phase'] == 'regular' \
        else mkt['session_minutes']
    print(f'\n{"TICKER":8}{"SHARES":>10}{"PRICE":>11}{"PREV":>11}{"CHG%":>8}{"EXT":>11}{"VOLUME":>14}{"AVG VOL":>14}{"REL":>6}')
    for t in tickers:
        x = q.get(t)
        shares = dict(book).get(t, 0)
        if not x:
            print(f'{t:8}{shares:>10g}   no quote')
            continue
        rel = (x['volume'] / (x['avg_volume'] * vol_curve(elapsed / mkt['session_minutes']))
               if x['volume'] and x['avg_volume'] else None)
        fmt = lambda v, spec: format(v, spec) if v else '—'
        print(f'{t:8}{shares:>10g}{x["price"]:>11.2f}{x["prev_close"]:>11.2f}'
              f'{(x["price"] / x["prev_close"] - 1) * 100:>+8.2f}'
              f'{fmt(x.get("ext_price"), ".2f"):>11}{fmt(x.get("volume"), ",.0f"):>14}'
              f'{fmt(x.get("avg_volume"), ",.0f"):>14}{(fmt(rel, ".1f") + "x" if rel else "—"):>6}')
    missing = [t for t in tickers if t not in q]
    if missing:
        print(f'\nNo quote for: {", ".join(missing)}')


# ── API ────────────────────────────────────────────────────────────────────
def make_app():
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
    app = FastAPI()

    @app.get('/api/portfolio')
    def portfolio():
        with lock:
            mkt = state['market']
            return JSONResponse({
                'asof': int(time.time() * 1000),
                'phase': mkt['phase'], 'market_open': mkt['phase'] == 'regular',
                'session_minutes': mkt['session_minutes'], 'early_close': mkt['early_close'],
                'holiday': mkt['holiday'],
                'provider': PROVIDER, 'provider_label': PROVIDER_LABEL,
                'holdings': state['holdings'], 'holdings_label': state['holdings_label'],
                'benchmark': state['benchmark'], 'quotes_at': state['quotes_at'],
                'snap_count': len(state['snaps']), 'session_date': state['session_date'],
                'error': state['error'], 'page_poll_seconds': PAGE_POLL_SECONDS,
            })

    @app.get('/api/snaps')
    def snaps(since: int = 0):
        """Today's per-holding snapshots after `since` (epoch ms): [[ms, {ticker: [price, volume]}, benchmark]]."""
        with lock:
            return JSONResponse({'session_date': state['session_date'],
                                 'snaps': [s for s in state['snaps'] if s[0] > since]})

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

    return app


if __name__ == '__main__':
    if '--check' in sys.argv:
        check()
        sys.exit()
    import uvicorn
    threading.Thread(target=poller, daemon=True).start()
    print(f'Brokerage Ring on http://localhost:{PORT}  ·  quotes: {PROVIDER_LABEL}')
    uvicorn.run(make_app(), host='127.0.0.1', port=PORT, log_level='warning')
