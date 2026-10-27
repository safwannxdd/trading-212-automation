"""Auto-generated from 1st_Paper_Trial.ipynb -- do not edit directly.
Regenerate with export_bot_script() after changing the notebook."""

# ---------- notebook cell 1 ----------
import os
import time
import json
import base64
import logging
import requests
import pandas as pd
import yfinance as yf
from datetime import datetime, timezone
from dotenv import load_dotenv

load_dotenv()

# ---------- notebook cell 3 ----------
T212_API_KEY    = os.environ["T212_API_KEY"]
T212_API_SECRET = os.environ["T212_API_SECRET"]

ENVIRONMENT = "demo"   # "demo" or "live" -- switch only after a full demo soak period
BASE_URL = {
    "demo": "https://demo.trading212.com/api/v0",
    "live": "https://live.trading212.com/api/v0",
}[ENVIRONMENT]

# Nothing is sent to the broker while this is True. The response field names below
# (fillPrice / ticker / quantity / freeFunds) have NEVER been seen against a real order,
# so leave this on until one dry-run cycle has been read in the log and the names confirmed.
DRY_RUN = True

WATCHLIST = ["NVDA", "PATH", "INTC", "AAL", "TSLA", "AMC", "SMCI", "NIO", "NU"]

# Strategy params match the config selected on VALIDATION data in Backtest_1st_Paper_Trial.ipynb
# (Section 16). The previous settings here -- 15% stop with the momentum filter disabled -- came
# from the old "Best Tuned" config, which was chosen by reading the 2026 test window it was then
# reported as validated on. Scored honestly on 2025 it returns +0.5% against this config's +36.5%.
MOMENTUM_LOOKBACK = 63   # trading days (~3 months)
MOMENTUM_SKIP = 5        # skip most recent days to dodge short-term reversal
TOP_N = 3                # positions to hold at once
REQUIRE_POSITIVE_MOMENTUM = True    # only hold names with positive absolute momentum

STOP_LOSS_PCT = 0.10          # sell if a position drops 10% from its average cost
DAILY_LOSS_LIMIT_PCT = 0.03   # halt new buys for the day if account equity drops this much intraday

# Broker-side stop orders. The backtest assumes a RESTING stop that triggers intraday; polling
# a price once an hour (on data that itself lags ~15 min) cannot reproduce that. With this on, a
# stop-sell is parked at T212 for every open position, and the polled check becomes a safety net
# rather than the mechanism. If a polled stop ever fires, the broker-side one failed -- that is
# logged as an error, not a warning.
USE_BROKER_STOPS = True
STOP_ORDER_VALIDITY = "GTC"    # if T212 only honours DAY, the per-cycle sync re-places it anyway
STOP_PRICE_TOLERANCE = 0.005   # leave an existing stop alone if within 0.5% of target

# Execution guards -- these enforce the backtest's no-leverage invariant in live trading
CASH_SAFETY_BUFFER = 0.98     # only ever commit 98% of free cash; leaves room for slippage
MIN_ORDER_VALUE_GBP = 1.00    # ignore dust rebalances not worth the spread
SETTLE_SECONDS = 5            # pause after sells before re-reading free cash
CHECK_INTERVAL_SECONDS = 3600 # poll hourly so stop-losses aren't only checked once a day

TRADE_LOG_PATH = "trade_log.xlsx"       # structured record of every executed order
ENTRY_STATE_PATH = "entry_prices.json"  # stop-loss anchors, persisted across restarts

# ---------- notebook cell 5 ----------
# --- Cell 3: Logging ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("t212_bot.log"),
    ],
)
log = logging.getLogger("t212_bot")

# ---------- notebook cell 7 ----------
class T212Client:
    def __init__(self, base_url, api_key, api_secret):
        self.base_url = base_url
        self._headers = {"Authorization": f"Basic {self._encode(api_key, api_secret)}"}

    @staticmethod
    def _encode(key, secret):
        return base64.b64encode(f"{key}:{secret}".encode()).decode()

    def _request(self, method, path, **kwargs):
        resp = requests.request(method, f"{self.base_url}{path}", headers=self._headers, **kwargs)

        remaining = resp.headers.get("x-ratelimit-remaining")
        reset = resp.headers.get("x-ratelimit-reset")
        if remaining is not None and int(remaining) <= 1 and reset:
            wait = max(0, int(reset) - int(time.time()))
            log.warning(f"Near rate limit on {path}, sleeping {wait}s")
            time.sleep(wait)

        resp.raise_for_status()
        return resp.json() if resp.content else None

    def get_account_summary(self):
        return self._request("GET", "/equity/account/summary")

    def get_positions(self):
        return self._request("GET", "/equity/positions")

    def get_orders(self):
        return self._request("GET", "/equity/orders")

    def place_market_order(self, ticker, quantity):
        return self._request("POST", "/equity/orders/market", json={"ticker": ticker, "quantity": quantity})

    def place_stop_order(self, ticker, quantity, stop_price, time_validity=None):
        """Park a resting stop-sell at the broker. `quantity` is negative to sell.

        Body field names are a best guess from the docs and are UNVERIFIED against a real
        response -- preflight_checks() is what confirms them. Every caller must treat failure as
        non-fatal and fall back to the polled stop-loss.
        """
        return self._request("POST", "/equity/orders/stop", json={
            "ticker": ticker,
            "quantity": quantity,
            "stopPrice": stop_price,
            "timeValidity": time_validity or STOP_ORDER_VALIDITY,
        })

    def cancel_order(self, order_id):
        return self._request("DELETE", f"/equity/orders/{order_id}")


client = T212Client(BASE_URL, T212_API_KEY, T212_API_SECRET)

# ---------- notebook cell 9 ----------
# --- Cell 5: Data fetching (yfinance) ---
def fetch_ohlcv(ticker, period="1y", interval="1d"):
    df = yf.download(ticker, period=period, interval=interval, auto_adjust=True, progress=False)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df[["Open", "High", "Low", "Close", "Volume"]].dropna()

def fetch_watchlist_data(watchlist=WATCHLIST):
    data = {}
    for ticker in watchlist:
        try:
            data[ticker] = fetch_ohlcv(ticker)
        except Exception as e:
            log.error(f"{ticker}: fetch failed — {e}")
    return data

# ---------- notebook cell 11 ----------
def calculate_momentum(df, lookback=MOMENTUM_LOOKBACK):
    end_price = df["Close"].iloc[-1 - MOMENTUM_SKIP]
    start_price = df["Close"].iloc[-1 - MOMENTUM_SKIP - lookback]
    return (end_price / start_price) - 1

def generate_target_portfolio(data):
    """Rank by momentum, hold the top N.

    A ticker without the FULL lookback history is skipped entirely rather than scored on a
    shortened window. The previous MIN_LOOKBACK fallback ranked, say, a 10-day score directly
    against a 63-day score -- they are not on the same scale, and the short-history name wins
    spuriously in any fast move. The backtest has no such fallback either, so this now matches it.
    """
    scores = {}
    for t, df in data.items():
        required = MOMENTUM_SKIP + MOMENTUM_LOOKBACK + 1
        if len(df) < required:
            log.warning(f"{t}: only {len(df)} bars, needs {required} for a full {MOMENTUM_LOOKBACK}-day lookback — skipping")
            continue
        scores[t] = calculate_momentum(df)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [t for t, score in ranked[:TOP_N] if not REQUIRE_POSITIVE_MOMENTUM or score > 0]

# ---------- notebook cell 13 ----------
_day_start_equity = None      # equity snapshot at the start of each trading day
_last_tracked_day = None
_halted_until = None          # date the buy-halt applies to; clears automatically next day


# --- stop-loss anchors, persisted to disk ---------------------------------------------------
# These used to live only in memory, so any kernel restart silently wiped every stop-loss anchor
# and left open positions with no downside protection at all -- while still appearing to work.
def _load_entry_prices():
    if os.path.exists(ENTRY_STATE_PATH):
        try:
            with open(ENTRY_STATE_PATH) as f:
                return {k: float(v) for k, v in json.load(f).items() if v is not None}
        except Exception as e:
            log.error(f"Could not read {ENTRY_STATE_PATH}, starting empty — {e}")
    return {}

def _save_entry_prices():
    try:
        with open(ENTRY_STATE_PATH, "w") as f:
            json.dump(_entry_prices, f, indent=1)
    except Exception as e:
        log.error(f"Could not persist entry prices — {e}")

_entry_prices = _load_entry_prices()


# --- account reads --------------------------------------------------------------------------
# Field names are unconfirmed against a real API response, so try the plausible spellings and
# fail loudly rather than silently returning None into the sizing maths.
EQUITY_FIELDS = ("totalValue", "total", "equity", "value")
CASH_FIELDS   = ("freeFunds", "free", "cash", "availableCash", "freeCash")

def _first_numeric(payload, candidates, label):
    if isinstance(payload, dict):
        for key in candidates:
            v = payload.get(key)
            if isinstance(v, (int, float)):
                return float(v)
    raise KeyError(f"No {label} field found in account summary. Saw keys: "
                   f"{list(payload) if isinstance(payload, dict) else type(payload)}")

def get_account_equity(client):
    return _first_numeric(client.get_account_summary(), EQUITY_FIELDS, "equity")

def get_free_cash(client):
    return _first_numeric(client.get_account_summary(), CASH_FIELDS, "free cash")


def check_daily_loss_limit(equity, day_start_equity):
    if not day_start_equity:
        return False
    return (day_start_equity - equity) / day_start_equity >= DAILY_LOSS_LIMIT_PCT

def desired_stop_price(ticker):
    """Where this position's resting stop belongs, from its weighted-average cost basis."""
    entry = _entry_prices.get(ticker)
    return round(entry * (1 - STOP_LOSS_PCT), 2) if entry else None

def check_stop_losses(latest_prices):
    # FALLBACK ONLY when USE_BROKER_STOPS is on -- the resting order at T212 is the real
    # mechanism. This catches the case where placing that order failed, and runs on prices that
    # may lag the market by ~15 minutes, so it is strictly the slower of the two paths.
    triggered = []
    for ticker, entry_price in _entry_prices.items():
        price = latest_prices.get(ticker)
        # entry_price can be missing if a fill response never yielded a usable price;
        # previously a None here raised TypeError and killed the whole cycle silently
        if not entry_price or not price:
            continue
        if (entry_price - price) / entry_price >= STOP_LOSS_PCT:
            triggered.append(ticker)
    return triggered

def seed_missing_entry_prices(current_holdings, latest_prices, positions):
    """Make sure every open position has a stop-loss anchor.

    If the state file was lost, an open position would otherwise carry no stop at all. Prefer the
    broker's own average price; fall back to today's close and log loudly, because a stop anchored
    at today's price is materially looser than the real one.
    """
    by_ticker = {}
    for p in positions if isinstance(positions, list) else []:
        t = p.get("ticker")
        if t:
            by_ticker[t] = p

    for ticker in current_holdings:
        if _entry_prices.get(ticker):
            continue
        avg = None
        for key in ("averagePrice", "avgPrice", "averageEntryPrice"):
            v = by_ticker.get(ticker, {}).get(key)
            if isinstance(v, (int, float)) and v > 0:
                avg = float(v); break
        if avg:
            _entry_prices[ticker] = avg
            log.warning(f"{ticker}: entry price missing, seeded from broker average {avg}")
        elif latest_prices.get(ticker):
            _entry_prices[ticker] = float(latest_prices[ticker])
            log.error(f"{ticker}: entry price missing AND no broker average — anchoring stop at "
                      f"today's close {latest_prices[ticker]}. Stop is looser than intended.")
    _save_entry_prices()

# ---------- notebook cell 15 ----------
_sent_intents = set()

FILL_PRICE_FIELDS = ("fillPrice", "averagePrice", "price", "filledPrice")

def _extract_fill_price(result, fallback):
    if isinstance(result, dict):
        for key in FILL_PRICE_FIELDS:
            v = result.get(key)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    if fallback:
        log.warning(f"No usable fill price in order response ({result!r}); "
                    f"anchoring stop-loss at last close {fallback} instead")
        return float(fallback)
    log.error(f"No fill price and no fallback for order response {result!r} — "
              f"this position will have NO stop-loss until the next successful fill")
    return None

def log_trade_to_excel(ticker, quantity, fill_price):
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ticker": ticker,
        "side": "BUY" if quantity > 0 else "SELL",
        "quantity": abs(quantity),
        "fill_price": fill_price,
        "dry_run": DRY_RUN,
    }
    if os.path.exists(TRADE_LOG_PATH):
        existing = pd.read_excel(TRADE_LOG_PATH)
        trade_log_df = pd.concat([existing, pd.DataFrame([record])], ignore_index=True)
    else:
        trade_log_df = pd.DataFrame([record])
    trade_log_df.to_excel(TRADE_LOG_PATH, index=False)

def execute_order(client, ticker, quantity, latest_price=None):
    quantity = round(float(quantity), 4)
    if latest_price and abs(quantity * latest_price) < MIN_ORDER_VALUE_GBP:
        log.info(f"Skipping dust order: {ticker} qty={quantity}")
        return
    if quantity == 0:
        return

    intent_key = (ticker, quantity, datetime.now(timezone.utc).date())
    if intent_key in _sent_intents:
        log.warning(f"Duplicate intent blocked: {intent_key}")
        return

    if DRY_RUN:
        log.info(f"[DRY RUN] would place {ticker} qty={quantity:+.4f} @ ~{latest_price}")
        _sent_intents.add(intent_key)
        if quantity > 0 and latest_price:
            _entry_prices[ticker] = float(latest_price)
        elif quantity < 0:
            _entry_prices.pop(ticker, None)
        _save_entry_prices()
        log_trade_to_excel(ticker, quantity, latest_price)
        return

    try:
        result = client.place_market_order(ticker, quantity)
        log.info(f"Order sent: {ticker} qty={quantity} -> {result}")
        _sent_intents.add(intent_key)
        fill_price = _extract_fill_price(result, latest_price)
        if quantity > 0:
            if fill_price:
                _entry_prices[ticker] = fill_price
        else:
            _entry_prices.pop(ticker, None)
        _save_entry_prices()
        log_trade_to_excel(ticker, quantity, fill_price)
    except Exception as e:
        log.error(f"Order failed: {ticker} qty={quantity} — {e}")


def _order_field(order, candidates, default=None):
    for key in candidates:
        value = order.get(key)
        if value is not None:
            return value
    return default


def _is_stop_order(order):
    kind = str(_order_field(order, ("type", "orderType", "kind"), "")).upper()
    return "STOP" in kind


def sync_stop_orders(client, current_holdings):
    """Ensure the broker holds a correct resting stop for every open position.

    Reconciled from T212's own order book each cycle rather than from local state, which makes it
    idempotent and self-healing: it recovers after a restart, after a DAY-validity order expires,
    and after any manual change made in the app. This is what lets the live stop behave like the
    backtest's -- triggering intraday in the market, not an hour later in a polling loop.
    """
    if not USE_BROKER_STOPS:
        return
    try:
        orders = client.get_orders() or []
    except Exception as e:
        log.error(f"Could not read open orders — leaving stops untouched this cycle. {e}")
        return

    live = {}
    for o in orders:
        if isinstance(o, dict) and _is_stop_order(o):
            ticker = _order_field(o, ("ticker", "instrument", "symbol"))
            if ticker:
                live[ticker] = o

    # 1. cancel stops for anything no longer held
    for ticker, o in live.items():
        if ticker in current_holdings:
            continue
        order_id = _order_field(o, ("id", "orderId"))
        if order_id is None:
            continue
        if DRY_RUN:
            log.info(f"[DRY RUN] would cancel orphaned stop on {ticker}")
            continue
        try:
            client.cancel_order(order_id)
            log.info(f"Cancelled orphaned stop on {ticker}")
        except Exception as e:
            log.error(f"Could not cancel orphaned stop on {ticker} — {e}")

    # 2. place or correct a stop for everything held
    protected, held = 0, len([q for q in current_holdings.values() if q > 0])
    for ticker, qty in current_holdings.items():
        want = desired_stop_price(ticker)
        if not want or qty <= 0:
            continue

        existing = live.get(ticker)
        if existing is not None:
            have_price = _order_field(existing, ("stopPrice", "stop", "price"))
            have_qty = abs(float(_order_field(existing, ("quantity", "qty"), 0) or 0))
            price_ok = have_price and abs(float(have_price) - want) / want <= STOP_PRICE_TOLERANCE
            qty_ok = have_qty and abs(have_qty - qty) / qty <= STOP_PRICE_TOLERANCE
            if price_ok and qty_ok:
                protected += 1
                continue                      # already correct, leave it alone
            order_id = _order_field(existing, ("id", "orderId"))
            if order_id is not None and not DRY_RUN:
                try:
                    client.cancel_order(order_id)
                except Exception as e:
                    log.error(f"Could not replace stale stop on {ticker} — {e}")
                    continue

        if DRY_RUN:
            log.info(f"[DRY RUN] would rest stop-sell {ticker} qty={-round(qty, 4)} @ {want}")
            protected += 1
            continue
        try:
            result = client.place_stop_order(ticker, -round(qty, 4), want)
            log.info(f"Stop resting at broker: {ticker} qty={-round(qty, 4)} @ {want} -> {result}")
            protected += 1
        except Exception as e:
            log.error(f"BROKER STOP FAILED for {ticker} @ {want} — this position is protected "
                      f"only by the slower polled check until it succeeds. {e}")

    # A correct, unchanged stop is otherwise silent, so a healthy steady state would log nothing
    # at all. This line is what makes ongoing protection observable (and auditable afterwards).
    if held:
        emit = log.info if protected >= held else log.warning
        emit(f"Stop coverage: {protected}/{held} positions have a resting stop")


def rebalance(client, data, current_holdings, latest_prices, equity, allow_buys=True):
    """Rebalance to a TRUE equal weight across the target names.

    The previous version sized every new buy at `equity / len(target)` while leaving already-held
    positions untouched. Because momentum retains winners that have run past their target weight,
    the committed total routinely exceeded 100% of the account. In the backtest that silently
    became leverage (cash negative on 87% of days, up to 1.49x gross); live, T212 would simply
    reject the over-sized orders, leaving a portfolio nobody ever tested. Positions are now
    trimmed and topped up to a real equal weight, and buys are capped by cash that actually exists.
    """
    target = [t for t in generate_target_portfolio(data) if t in latest_prices]
    log.info(f"Target portfolio: {target}")

    # 1. exit everything not in the target
    for ticker, qty in list(current_holdings.items()):
        if ticker not in target and qty > 0:
            execute_order(client, ticker, -qty, latest_prices.get(ticker))

    if not target:
        log.info("No name passed the momentum filter — holding cash")
        return
    if not allow_buys:
        log.warning("Buys suppressed — daily loss limit breached today")
        return

    desired_value = equity / len(target)

    # 2. trim overweight positions first, so the proceeds help fund the top-ups
    for ticker in target:
        price = latest_prices[ticker]
        excess_value = current_holdings.get(ticker, 0.0) * price - desired_value
        if excess_value > MIN_ORDER_VALUE_GBP:
            log.info(f"{ticker}: trimming £{excess_value:,.2f} back to target weight")
            execute_order(client, ticker, -(excess_value / price), price)

    # 3. re-read ACTUAL free cash after the sells rather than estimating it, then top up within it
    time.sleep(SETTLE_SECONDS)
    try:
        free_cash = get_free_cash(client)
    except Exception as e:
        log.error(f"Could not read free cash — skipping all buys this cycle rather than "
                  f"risking a rejected or over-sized order. {e}")
        return

    budget = free_cash * CASH_SAFETY_BUFFER
    log.info(f"Free cash £{free_cash:,.2f}, deployable budget £{budget:,.2f}")

    for ticker in target:
        price = latest_prices[ticker]
        shortfall = desired_value - current_holdings.get(ticker, 0.0) * price
        spend = min(shortfall, budget)
        if spend > MIN_ORDER_VALUE_GBP:
            execute_order(client, ticker, spend / price, price)
            budget -= spend

# ---------- notebook cell 17 ----------
def reconcile_state(client, expected_holdings, positions=None):
    # Accepts an already-fetched position list. /equity/positions is one of the tightest
    # rate-limited endpoints, and re-reading it here made three calls per cycle where one will do.
    if positions is None:
        try:
            positions = client.get_positions()
        except Exception as e:
            log.error(f"Reconciliation failed — {e}")
            return
    actual = {p["ticker"] for p in positions}  # TODO: confirm field name once a real position object is seen
    expected = set(expected_holdings)
    if actual != expected:
        log.error(f"Position mismatch — expected {expected}, actual {actual}")

# ---------- notebook cell 19 ----------
_last_rebalance_day = None

def is_first_trading_day_of_week(data, today):
    """True when today is the first session of its ISO week.

    Replaces `today.weekday() == REBALANCE_WEEKDAY`. A fixed Monday meant every week whose Monday
    was a market holiday got no rebalance at all -- 36 of them in the backtest window -- and the
    live bot would have fired into a closed market on those days. The backtest now uses this same
    first-session-of-week rule, so the two finally agree.
    """
    if today.weekday() >= 5:
        return False
    idx = next(iter(data.values())).index
    iso = today.isocalendar()[:2]
    earlier_this_week = [d for d in idx if d.isocalendar()[:2] == iso and d.date() < today]
    return len(earlier_this_week) == 0


def run_once():
    global _day_start_equity, _last_tracked_day, _halted_until, _last_rebalance_day

    log.info(f"=== Cycle start {'[DRY RUN]' if DRY_RUN else '[LIVE ORDERS]'} ===")
    today = datetime.now(timezone.utc).date()

    data = fetch_watchlist_data()
    if not data:
        log.error("No market data fetched — skipping cycle")
        return
    latest_prices = {t: float(df["Close"].iloc[-1]) for t, df in data.items()}
    equity = get_account_equity(client)

    if _last_tracked_day != today:
        _day_start_equity = equity
        _last_tracked_day = today
        _sent_intents.clear()
        if _halted_until and _halted_until < today:
            _halted_until = None

    positions = client.get_positions()
    current_holdings = {p["ticker"]: p["quantity"] for p in positions}  # TODO: confirm field names
    # A stop firing at the broker closes a position without the bot doing anything, which would
    # otherwise leave no trace in the log at all. A surviving entry-price anchor with no matching
    # position is the tell. Clearing it also stops the stale anchor confusing check_stop_losses.
    for ticker in [t for t in list(_entry_prices) if t not in current_holdings]:
        log.warning(f"Position closed outside the bot: {ticker} — most likely the broker stop fired")
        _entry_prices.pop(ticker, None)
    _save_entry_prices()

    seed_missing_entry_prices(current_holdings, latest_prices, positions)

    # Fallback stop check. With USE_BROKER_STOPS on this should never fire -- if it does, the
    # resting order at T212 was missing or rejected, so it is logged as an error.
    for ticker in check_stop_losses(latest_prices):
        if ticker in current_holdings:
            log.error(f"FALLBACK stop-loss fired on {ticker} — the broker-side stop did not work")
            execute_order(client, ticker, -current_holdings[ticker], latest_prices.get(ticker))

    if check_daily_loss_limit(equity, _day_start_equity):
        _halted_until = today
        log.error(f"Daily loss limit breached — halting new buys for {today}")

    allow_buys = _halted_until != today
    intents_before = len(_sent_intents)

    if _last_rebalance_day != today and is_first_trading_day_of_week(data, today):
        _last_rebalance_day = today
        rebalance(client, data, current_holdings, latest_prices, equity, allow_buys=allow_buys)
    else:
        log.info("Not a rebalance day — stop-loss/kill-switch checks only")

    # Only re-read positions if we actually traded. On a quiet cycle nothing moved, so spending
    # another /equity/positions call just burns rate limit -- which is what was forcing the
    # "Near rate limit ... sleeping" pauses on every single cycle.
    if len(_sent_intents) > intents_before:
        try:
            final_positions = client.get_positions() or []
            final_holdings = {p["ticker"]: p["quantity"] for p in final_positions}
        except Exception as e:
            log.error(f"Could not re-read positions for stop sync — {e}")
            final_positions, final_holdings = positions, current_holdings
    else:
        final_positions, final_holdings = positions, current_holdings

    sync_stop_orders(client, final_holdings)
    reconcile_state(client, final_holdings, positions=final_positions)
    log.info("=== Cycle end ===")


# RUN_MODE picks the deployment shape:
#   "loop" -- long-lived process on an always-on VM (systemd keeps it alive)
#   "once" -- single cycle then exit, for cron / GitHub Actions style schedulers
# Single-shot is the more robust of the two: a crashed long-running process stops trading
# silently, whereas a scheduler simply runs again at the next slot.
RUN_MODE = os.environ.get("RUN_MODE", "loop")

def _running_in_notebook():
    """True inside a Jupyter kernel, where __name__ is also "__main__"."""
    try:
        return get_ipython().__class__.__name__ == "ZMQInteractiveShell"
    except NameError:
        return False


# The kernel check matters as much as the __name__ one: inside Jupyter __name__ IS "__main__",
# so without this, running this cell would start the endless loop and hang the kernel. In the
# notebook this cell only defines things -- call run_once() yourself for a single cycle.
if __name__ == "__main__" and not _running_in_notebook():
    if RUN_MODE == "once":
        run_once()
    else:
        while True:
            try:
                run_once()
            except Exception as e:
                log.error(f"Cycle failed: {e}")
            time.sleep(CHECK_INTERVAL_SECONDS)
