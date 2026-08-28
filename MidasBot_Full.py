#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MidasBot v2 — Full Squad, Single Brain
======================================
One bot, multiple *phases* (no toggles):
SCOUT -> LUNCHBOX (mean-revert) / REGULAR (grid) / AFTERBURNER (momentum) / DIP (pullback DCA).

- Exchange: Kraken only (via ccxt) — public API only; keys optional.
- Paper trading with a REAL fill engine: resting limit orders filled against
  live candle highs/lows. Lot-based long-only accounting. The book can and
  does record losses (v1 booked every trade as an instant winner — fiction).
- Persistent: midas_state.json survives restarts (atomic writes). --fresh resets.
- Measured: equity_curve.csv every tick, fee-viability banner at startup,
  session summary (win rate, realized P/L, max drawdown) on exit.
- Live order execution is intentionally NOT implemented in this build.

Quick start (paper mode)
------------------------
python MidasBot_Full.py --exchange kraken --pair BTC/USD --budget 50

Parameters (CLI flags)
----------------------
--exchange      kraken (only)
--pair          default BTC/USD (auto-maps to /USDT if /USD unlisted)
--pairs         comma-separated pairs, or 'all' for the full PAIR_CATALOG
--budget        USD budget cap / starting paper cash (default 50)
--grids         grid levels (int, default 8)
--spacing       spacing between levels as fraction (default 0.005 => 0.5%)
--min-net       minimum net step after both legs' maker fees (default 0.002)
--tick          loop seconds (default 15)
--stop-mult     stop distance = stop_mult * spacing below entry (default 3.0; 0 disables)
--hyst          consecutive ticks required to switch phase (default 3)
--state         state file path (default midas_state.json)
--fresh         discard saved state and start a new paper book
--config        path to YAML to override any of the above
--dryrun        simulate a single cycle then exit (for testing)
--maker/--taker manual fee overrides
"""
import os, sys, time, math, csv, json, argparse, threading, webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from datetime import datetime, timezone

try:
    import yaml
except Exception:
    yaml = None

import ccxt
from dotenv import load_dotenv
load_dotenv()

__version__ = "2.3.0"

# Kraken margin pair catalog — max leverage the exchange offers per pair,
# as read off the operator's margin UI (2026-08-27). Each pair trades at its
# catalog max by default; --leverage overrides (1 = spot).
PAIR_CATALOG = {
    "AVAX/USD":   {"name": "Avalanche",       "max_leverage": 10},
    "UNI/USD":    {"name": "Uniswap",         "max_leverage": 5},
    "CRV/USD":    {"name": "Curve DAO Token", "max_leverage": 5},
    "AAVE/USD":   {"name": "Aave",            "max_leverage": 5},
    "LTC/USD":    {"name": "Litecoin",        "max_leverage": 10},
    "NEAR/USD":   {"name": "NEAR Protocol",   "max_leverage": 5},
    "RENDER/USD": {"name": "Render",          "max_leverage": 5},
    "PEPE/USD":   {"name": "Pepe",            "max_leverage": 5},
    "HBAR/USD":   {"name": "Hedera",          "max_leverage": 5},
    "DOT/USD":    {"name": "Polkadot",        "max_leverage": 5},
    "SHIB/USD":   {"name": "Shiba Inu",       "max_leverage": 5},
    "TRX/USD":    {"name": "TRON",            "max_leverage": 5},
    "BCH/USD":    {"name": "Bitcoin Cash",    "max_leverage": 5},
    "WLD/USD":    {"name": "Worldcoin",       "max_leverage": 3},
    "USDC/USD":   {"name": "USDC",            "max_leverage": 10},
    "ALGO/USD":   {"name": "Algorand",        "max_leverage": 5},
}

# shared across bot threads: CSV appends must not interleave
LOG_LOCK = threading.Lock()

# ---------------------------- Utilities ----------------------------

def now_utc_str():
    return datetime.now(timezone.utc).isoformat()

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def pretty_exc(e):
    return f"{type(e).__name__}: {e}"

def fmt_px(x):
    # dynamic precision: 80254.4 -> '80254.4', PEPE -> '3.924e-06', never '0.00'
    return f"{x:.6g}"

def sig_round(x, sig=8):
    # round to significant figures — fixed decimals would turn sub-penny
    # prices (PEPE, SHIB) into 0.0
    if x <= 0:
        return 0.0
    return round(x, max(0, sig - 1 - int(math.floor(math.log10(x)))))

def atomic_write_json(path, obj):
    # write-then-replace so a crash mid-write never corrupts saved state
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=1)
    os.replace(tmp, path)

# ---------------------------- Indicators (numpy-lite) ----------------------------

def ema_list(values, n):
    if not values or n <= 1:
        return values[:]
    k = 2.0 / (n + 1.0)
    ema = []
    s = values[0]
    ema.append(s)
    for v in values[1:]:
        s = (v - s) * k + s
        ema.append(s)
    return ema

def rsi_list(values, n=14):
    if len(values) < n + 1:
        return None
    gains = 0.0; losses = 0.0
    for i in range(1, n+1):
        d = values[i] - values[i-1]
        if d >= 0: gains += d
        else: losses -= d
    avg_gain = gains / n
    avg_loss = losses / n if losses > 0 else 0.0
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))

def atrp_list(ohlcv, n=14):
    if len(ohlcv) < n + 1:
        return 0.0
    trs = []
    prev_close = ohlcv[0][4]
    for i in range(1, len(ohlcv)):
        _ts, o, h, l, c, *rest = ohlcv[i]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)
        prev_close = c
    k = 2.0 / (n + 1.0)
    s = trs[0]
    for v in trs[1:]:
        s = (v - s) * k + s
    last_close = ohlcv[-1][4] or 1.0
    return s / last_close

# ---------------------------- Paper book ----------------------------

class PaperBook:
    """Simulated long-only spot account.

    Resting limit orders fill against real candle highs/lows — never against a
    price we invented. Every filled entry becomes a lot with its own TP (and
    optional stop). Sells only ever close held lots, so the book is genuinely
    long-only spot. Losses are booked when a stop fills.
    """

    # Kraken margin costs (fraction of notional)
    MARGIN_OPEN_FEE = 0.0002       # one-time on open
    ROLLOVER_PER_4H = 0.0002       # charged while the position is open
    LIQ_MARGIN_FRAC = 0.8          # forced close when loss eats 80% of margin

    def __init__(self, cash, lev=1):
        self.cash = float(cash)
        self.lev = max(1, float(lev))
        self.lots = {}        # lot_id -> {qty, entry_px, margin, notional, lev, fees_open, tag, ts}
        self.orders = {}      # order_id -> {side, kind, px, qty, tag, lot_id, ts}
        self.next_id = 1
        self.realized = 0.0
        self.peak_equity = float(cash)

    # ---- ids / state ----

    def _nid(self, prefix):
        i = self.next_id
        self.next_id += 1
        return f"{prefix}{i}"

    def to_state(self):
        return {"cash": self.cash, "lots": self.lots, "orders": self.orders,
                "next_id": self.next_id, "realized": self.realized,
                "peak_equity": self.peak_equity}

    @classmethod
    def from_state(cls, st):
        b = cls(st.get("cash", 0.0))
        b.lots = st.get("lots", {})
        b.orders = st.get("orders", {})
        b.next_id = st.get("next_id", 1)
        b.realized = st.get("realized", 0.0)
        b.peak_equity = st.get("peak_equity", b.cash)
        return b

    # ---- accounting ----

    def inventory(self):
        qty = sum(l["qty"] for l in self.lots.values())
        ntl = sum(l["notional"] for l in self.lots.values())
        avg = (ntl / qty) if qty > 0 else 0.0
        return qty, avg

    def committed(self):
        # margin already spoken for: held lots + resting entries at leverage
        resting = sum(o["px"] * o["qty"] / self.lev for o in self.orders.values()
                      if o["kind"] == "ENTRY")
        held = sum(l["margin"] for l in self.lots.values())
        return resting + held

    def accrued_costs(self, now=None):
        """Costs already locked into open lots: open fees paid + rollover
        accrued so far. Ignoring these makes uPnL read optimistically."""
        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)
        tot = 0.0
        for l in self.lots.values():
            tot += l["fees_open"]
            if l["lev"] > 1:
                try:
                    hours = max(0.0, (now_dt - datetime.fromisoformat(l["ts"])).total_seconds() / 3600)
                except Exception:
                    hours = 0.0
                tot += l["notional"] * self.ROLLOVER_PER_4H * (hours / 4.0)
        return tot

    def equity(self, price, now=None):
        held_margin = sum(l["margin"] for l in self.lots.values())
        return self.cash + held_margin + self.unrealized(price, now)

    def unrealized(self, price, now=None):
        gross = sum(l["qty"] * (price - l["entry_px"]) for l in self.lots.values())
        return gross - self.accrued_costs(now)

    # ---- order placement ----

    def place_entry(self, px, qty, tag):
        oid = self._nid("O")
        self.orders[oid] = {"side": "buy", "kind": "ENTRY", "px": px, "qty": qty,
                            "tag": tag, "lot_id": None, "ts": now_utc_str()}
        return oid

    def _place_exits(self, lot_id, lot, tp_px, stop_px):
        oid = self._nid("O")
        self.orders[oid] = {"side": "sell", "kind": "TP", "px": tp_px,
                            "qty": lot["qty"], "tag": lot["tag"],
                            "lot_id": lot_id, "ts": now_utc_str()}
        if stop_px is not None:
            oid = self._nid("O")
            self.orders[oid] = {"side": "sell", "kind": "STOP", "px": stop_px,
                                "qty": lot["qty"], "tag": lot["tag"],
                                "lot_id": lot_id, "ts": now_utc_str()}

    def cancel(self, oid):
        self.orders.pop(oid, None)

    def cancel_entries(self, predicate=lambda o: True):
        for oid in [k for k, o in self.orders.items()
                    if o["kind"] == "ENTRY" and predicate(o)]:
            self.cancel(oid)

    # ---- fills ----

    def process_fills(self, candle, last_px, fees, tp_step_fn, stop_mult, spacing,
                      on_round_trip, now=None):
        """Fill resting orders against the latest candle's high/low (falling
        back to last price). When both a lot's TP and STOP are inside the same
        candle, the stop wins — pessimistic on purpose."""
        if candle:
            hi, lo = float(candle[2]), float(candle[3])
            hi, lo = max(hi, last_px), min(lo, last_px)
        else:
            hi = lo = last_px
        maker, taker = fees["maker"], fees["taker"]
        events = []

        now_dt = datetime.fromisoformat(now) if now else datetime.now(timezone.utc)

        def rollover(lot):
            if lot["lev"] <= 1:
                return 0.0
            try:
                opened = datetime.fromisoformat(lot["ts"])
                hours = max(0.0, (now_dt - opened).total_seconds() / 3600)
            except Exception:
                hours = 0.0
            return lot["notional"] * self.ROLLOVER_PER_4H * (hours / 4.0)

        def close_lot(lot_id, lot, exit_px, reason, fee_rate):
            self.lots.pop(lot_id)
            exit_fee = lot["qty"] * exit_px * fee_rate
            roll = rollover(lot)
            pnl = lot["qty"] * (exit_px - lot["entry_px"]) - lot["fees_open"] - exit_fee - roll
            self.cash += lot["margin"] + pnl
            self.realized += pnl
            for sid, so in list(self.orders.items()):
                if so.get("lot_id") == lot_id:
                    self.cancel(sid)
            events.append(f"{reason} {lot['tag']} pnl=${pnl:+.4f}")
            on_round_trip(lot, exit_px, reason, fee_rate, pnl)

        # exits first (pessimistic: stops before TPs)
        for kind in ("STOP", "TP"):
            for oid, o in list(self.orders.items()):
                if o["kind"] != kind or o["lot_id"] not in self.lots:
                    continue
                hit = (lo <= o["px"]) if kind == "STOP" else (hi >= o["px"])
                if not hit:
                    continue
                close_lot(o["lot_id"], self.lots[o["lot_id"]], o["px"], kind,
                          taker if kind == "STOP" else maker)

        # forced liquidation: loss reached LIQ_MARGIN_FRAC of the lot's margin
        # (covers gaps below the stop, and stop_mult=0 with leverage)
        for lot_id, lot in list(self.lots.items()):
            if lot["lev"] <= 1:
                continue
            liq_px = lot["entry_px"] - self.LIQ_MARGIN_FRAC * lot["margin"] / lot["qty"]
            if lo <= liq_px:
                close_lot(lot_id, lot, liq_px, "LIQ", taker)

        # entries
        for oid, o in list(self.orders.items()):
            if o["kind"] != "ENTRY" or lo > o["px"]:
                continue
            if o["px"] <= 0:
                self.cancel(oid)   # malformed level; never fill at zero
                continue
            notional = o["qty"] * o["px"]
            margin = notional / self.lev
            fees_open = notional * maker + (notional * self.MARGIN_OPEN_FEE
                                            if self.lev > 1 else 0.0)
            if margin + fees_open > self.cash:
                self.cancel(oid)   # would overdraw the paper account
                continue
            self.cancel(oid)
            self.cash -= margin + fees_open
            lot_id = self._nid("L")
            lot = {"qty": o["qty"], "entry_px": o["px"], "margin": margin,
                   "notional": notional, "lev": self.lev, "fees_open": fees_open,
                   "tag": o["tag"], "ts": (now or now_utc_str())}
            self.lots[lot_id] = lot
            step = tp_step_fn(o["tag"])
            tp_px = o["px"] * (1 + step)
            stop_px = o["px"] * (1 - stop_mult * spacing) if stop_mult > 0 else None
            self._place_exits(lot_id, lot, tp_px, stop_px)
            events.append(f"FILL {o['tag']} buy {o['qty']:.8f}@{fmt_px(o['px'])} {self.lev:g}x")
        return events

# ---------------------------- Bot ----------------------------

class MidasBot:
    def __init__(self, exchange_name:str, api_key:str, api_secret:str, pair:str,
                 paper:bool=True, budget_usd:float=50.0, grids:int=8, spacing:float=0.005,
                 min_net:float=0.002, tick:int=15, log_csv:str="family_trades.csv",
                 manual_fees:dict|None=None, stop_mult:float=3.0, hyst:int=3,
                 state_path:str="midas_state.json", fresh:bool=False,
                 equity_csv:str="equity_curve.csv", ex=None, leverage:float|None=None,
                 persist:bool=True):
        self.exchange_name = exchange_name.lower()
        self.api_key = api_key
        self.api_secret = api_secret
        self.pair = pair
        self.paper = paper
        self.budget_usd = float(budget_usd)
        self.grids = int(grids)
        self.spacing = float(spacing)
        self.min_net = float(min_net)
        self.tick = int(tick)
        self.stop_mult = float(stop_mult)
        self.hyst = max(1, int(hyst))
        # leverage: explicit arg wins; else the pair's catalog max; else 1x spot
        cat = PAIR_CATALOG.get(pair)
        self.leverage = float(leverage) if leverage else float(cat["max_leverage"]) if cat else 1.0
        self.leverage = max(1.0, self.leverage)
        self.log_csv = log_csv
        self.equity_csv = equity_csv
        self.state_path = state_path
        self.stop_flag = False
        self.thread = None

        self.phase = "SCOUT"
        self._pending_regime = "SCOUT"
        self._pending_count = 0
        self.last_msg = ""
        self.last_price = 0.0
        self.last_tick_utc = None
        self.persist = persist
        self._bt_now = None   # backtest sets this so trade rows carry candle time
        self.fees = {"maker":0.0010, "taker":0.0015}
        if manual_fees:
            self.fees.update({k: float(v) for k,v in manual_fees.items() if k in ("maker","taker")})
        self.session = {"start": now_utc_str(), "wins": 0, "losses": 0,
                        "realized": 0.0, "max_dd": 0.0, "by_tag": {}}

        if ex is not None:
            self.ex = ex
        else:
            self.ex = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})

        # paper book: restore saved state unless told to start fresh
        self.book = None
        if not fresh and self.persist and Path(self.state_path).exists():
            try:
                with open(self.state_path, encoding="utf-8") as f:
                    st = json.load(f)
                if st.get("pair") == self.pair and st.get("exchange") == self.exchange_name:
                    self.book = PaperBook.from_state(st["book"])
                    print(f"[i] Restored state: cash=${self.book.cash:.2f} "
                          f"lots={len(self.book.lots)} orders={len(self.book.orders)} "
                          f"realized=${self.book.realized:+.2f}")
                    if abs(self.book.cash - self.budget_usd) > 0.005 and not self.book.lots:
                        print(f"[!] {self.pair}: restored cash ${self.book.cash:.2f} != "
                              f"budget ${self.budget_usd:.2f}; planning cap uses the budget. "
                              f"Use --fresh to reset the book.")
                else:
                    print(f"[!] State file is for {st.get('exchange')}/{st.get('pair')}; starting fresh.")
            except Exception as e:
                print(f"[!] Could not restore state ({pretty_exc(e)}); starting fresh.")
        if self.book is None:
            self.book = PaperBook(self.budget_usd, lev=self.leverage)
        else:
            self.book.lev = self.leverage
            # migrate v2.0 spot lots into margin fields
            for lot in self.book.lots.values():
                lot.setdefault("margin", lot.get("cost", lot["qty"] * lot["entry_px"]))
                lot.setdefault("notional", lot["qty"] * lot["entry_px"])
                lot.setdefault("lev", 1)
                lot.setdefault("fees_open", 0.0)

        # Prepare CSVs (log_csv=None -> no trade logging, e.g. parameter sweeps)
        if self.log_csv:
            p = Path(self.log_csv)
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                with open(p, "w", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    w.writerow(["utc","exchange","bot","symbol","side","qty","entry_px","exit_px",
                                "gross_pct","net_pct","fee_pct_rt","pnl_usd","runtime_sec","notes"])
        q = Path(self.equity_csv)
        if self.persist and not q.exists():
            with open(q, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["utc","pair","price","cash","inventory_qty","equity",
                                        "realized","unrealized","drawdown_pct","phase"])

    # ---------------- I/O helpers ----------------

    def _log_trade(self, **kw):
        if not self.log_csv:
            return
        with LOG_LOCK, open(self.log_csv, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([kw.get("utc"), kw.get("exchange"), kw.get("bot"), kw.get("symbol"),
                        kw.get("side"), kw.get("qty"), kw.get("entry_px"), kw.get("exit_px"),
                        kw.get("gross_pct"), kw.get("net_pct"), kw.get("fee_pct_rt"),
                        kw.get("pnl_usd"), kw.get("runtime_sec"), kw.get("notes")])

    def _price(self):
        try:
            t = self.ex.fetch_ticker(self.pair)
            return float(t.get("last") or t.get("close") or 0.0)
        except Exception as e:
            self.last_msg = f"Price err: {pretty_exc(e)}"
            return 0.0

    def _fees_update(self):
        if "manual" in self.fees:  # reserved
            return
        try:
            if not self.api_key:
                raise PermissionError("no API key; use market metadata")
            f = self.ex.fetch_trading_fee(self.pair)
            m = float(f.get("maker", self.fees["maker"]))
            t = float(f.get("taker", self.fees["taker"]))
            self.fees = {"maker": m, "taker": t}
        except Exception:
            try:
                if not self.ex.markets:
                    self.ex.load_markets()
                mkt = self.ex.market(self.pair)
                self.fees["maker"] = float(mkt.get("maker", self.fees["maker"]))
                self.fees["taker"] = float(mkt.get("taker", self.fees["taker"]))
            except Exception:
                pass

    def _ohlcv(self, tf="5m", limit=200):
        try:
            return self.ex.fetch_ohlcv(self.pair, timeframe=tf, limit=limit) or []
        except Exception:
            return []

    def _save_state(self):
        if not self.persist:
            return
        try:
            atomic_write_json(self.state_path, {
                "version": __version__, "exchange": self.exchange_name,
                "pair": self.pair, "saved": now_utc_str(),
                "book": self.book.to_state()})
        except Exception as e:
            self.last_msg = f"state save err: {pretty_exc(e)}"

    # ---------------- Brain ----------------

    def _regime(self, ohlcv):
        if len(ohlcv) < 50:
            return "SCOUT"
        closes = [c[4] for c in ohlcv]
        ema_fast = ema_list(closes, 12)[-1]
        ema_slow = ema_list(closes, 48)[-1]
        slope = (ema_fast - ema_slow) / (ema_slow + 1e-12)
        atrp = atrp_list(ohlcv, 14)
        r = rsi_list(closes, 14)
        if r is None:
            return "SCOUT"
        # Momentum regime
        if slope > 0.0008 and r > 55 and atrp > 0.003:
            return "AFTERBURNER"
        # Mean-revert regime
        if abs(slope) < 0.0004 and 35 < r < 65 and atrp < 0.005:
            return "LUNCHBOX"
        # Balanced grid
        if abs(slope) < 0.0015 and atrp >= 0.003:
            return "REGULAR"
        # Deep pullback
        if r < 32 and slope > -0.002:
            return "DIP"
        return "SCOUT"

    def _hysteresis(self, raw):
        # require self.hyst consecutive ticks before switching phase
        if raw == self.phase:
            self._pending_count = 0
            return self.phase
        if raw == self._pending_regime:
            self._pending_count += 1
        else:
            self._pending_regime = raw
            self._pending_count = 1
        if self._pending_count >= self.hyst:
            self._pending_count = 0
            return raw
        return self.phase

    def _tp_step(self, tag):
        return self.spacing * (1.5 if tag == "AFTERBURNER" else 1.0)

    def _net_of(self, tag):
        # what one booked round trip actually nets after both maker legs + slippage
        return self._tp_step(tag) - (self.fees["maker"]*2 + 0.0002)

    def viability(self):
        rows = []
        for tag in ("LUNCHBOX", "REGULAR", "DIP", "AFTERBURNER"):
            net = self._net_of(tag)
            rows.append((tag, self._tp_step(tag), net, net >= self.min_net))
        return rows

    # ---------------- Planning (long-only) ----------------

    def _plan(self, price, regime):
        """Place buy levels below price; sells exist only as lot TP/STOP exits."""
        if regime == "SCOUT":
            self.book.cancel_entries()
            return []
        if self._net_of(regime) < self.min_net:
            return [f"{regime} idle: spacing {self.spacing:.3%} can't clear "
                    f"round-trip fees (net {self._net_of(regime):.4%} < min_net {self.min_net:.4%})"]
        # cancel entries that no longer fit the phase or drifted out of band
        band = self.spacing * (self.grids + 1)
        self.book.cancel_entries(lambda o: o["tag"] != regime
                                 or abs(price - o["px"]) / price > band)
        per = self.budget_usd / self.grids
        placed = []
        if regime == "AFTERBURNER":
            levels = [price * (1 - 0.0005)]          # ride momentum: enter at touch
        else:
            levels = [price * (1 - self.spacing * i) for i in range(1, self.grids + 1)]
        near = self.spacing * 0.4
        existing = [o["px"] for o in self.book.orders.values() if o["kind"] == "ENTRY"]
        for lvl in levels:
            if len(placed) >= 2:                     # max 2 new orders per tick
                break
            if any(abs(lvl - px) / price < near for px in existing):
                continue
            if self.book.committed() + per > self.budget_usd + 1e-9:
                break                                # budget fully deployed
            qty = sig_round(per * self.leverage / lvl, 10)   # margin per level x leverage
            if qty <= 0:
                continue
            self.book.place_entry(sig_round(lvl), qty, regime)
            existing.append(lvl)
            placed.append(f"ENTRY {regime} {qty:.8f}@{lvl:.2f}")
        return placed

    # ---------------- Round-trip bookkeeping ----------------

    def _on_round_trip(self, lot, exit_px, reason, exit_fee_rate, pnl):
        entry_px = lot["entry_px"]
        gross = (exit_px - entry_px) / entry_px          # price move
        net = pnl / lot["margin"] if lot["margin"] else 0.0   # return on margin
        self.session["realized"] += pnl
        self.session["wins" if pnl >= 0 else "losses"] += 1
        t = self.session["by_tag"].setdefault(lot["tag"],
            {"n": 0, "wins": 0, "win_sum": 0.0, "loss_sum": 0.0})
        t["n"] += 1
        if pnl >= 0:
            t["wins"] += 1; t["win_sum"] += pnl
        else:
            t["loss_sum"] += pnl
        self._log_trade(utc=(self._bt_now or now_utc_str()), exchange=self.exchange_name.upper(),
                        bot=lot["tag"], symbol=self.pair, side="LONG",
                        qty=lot["qty"], entry_px=entry_px, exit_px=exit_px,
                        gross_pct=round(gross, 6), net_pct=round(net, 6),
                        fee_pct_rt=self.fees["maker"] + exit_fee_rate,
                        pnl_usd=round(pnl, 6), runtime_sec=self.tick,
                        notes=f"{lot['tag']} {reason} {lot['lev']:g}x")

    # ---------------- Loop ----------------

    def _tick(self):
        self._fees_update()
        price = self._price()
        if price <= 0:
            return
        ohlcv = self._ohlcv("5m", 200)
        regime = self._hysteresis(self._regime(ohlcv))
        self.phase = regime
        candle = ohlcv[-1] if ohlcv else None

        events = self.book.process_fills(candle, price, self.fees, self._tp_step,
                                         self.stop_mult, self.spacing,
                                         self._on_round_trip)
        notes = self._plan(price, regime)

        eq = self.book.equity(price)
        self.book.peak_equity = max(self.book.peak_equity, eq)
        dd = (self.book.peak_equity - eq) / self.book.peak_equity if self.book.peak_equity > 0 else 0.0
        self.session["max_dd"] = max(self.session["max_dd"], dd)
        qty, avg = self.book.inventory()
        upnl = self.book.unrealized(price)

        try:
            with LOG_LOCK, open(self.equity_csv, "a", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow([now_utc_str(), self.pair, price, round(self.book.cash, 6),
                                        qty, round(eq, 6), round(self.book.realized, 6),
                                        round(upnl, 6), round(dd, 6), regime])
        except Exception:
            pass
        self._save_state()

        extra = ("; ".join(events + notes))[:120]
        self.last_price = price
        self.last_tick_utc = now_utc_str()
        ntl = sum(l["notional"] for l in self.book.lots.values())
        self.last_msg = (f"{regime} {self.leverage:g}x | px={fmt_px(price)} eq=${eq:.2f} "
                         f"cash=${self.book.cash:.2f} ntl=${ntl:.2f} "
                         f"inv={fmt_px(qty)}@{fmt_px(avg)} uPnL=${upnl:+.3f} rPnL=${self.book.realized:+.3f} "
                         f"orders={len(self.book.orders)} dd={dd:.2%}"
                         + (f" | {extra}" if extra else ""))

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run_loop, daemon=True)
        self.thread.start()

    def _run_loop(self):
        while not self.stop_flag:
            try:
                self._tick()
            except Exception as e:
                self.last_msg = f"tick err: {pretty_exc(e)}"
            time.sleep(max(5, self.tick))

    def stop(self):
        self.stop_flag = True

    def summary(self):
        s = self.session
        n = s["wins"] + s["losses"]
        wr = (s["wins"] / n * 100) if n else 0.0
        return (f"session: {n} round trips ({s['wins']}W/{s['losses']}L, {wr:.0f}% win) | "
                f"realized ${s['realized']:+.4f} | max drawdown {s['max_dd']:.2%} | "
                f"book realized ${self.book.realized:+.4f} cash ${self.book.cash:.2f} "
                f"lots {len(self.book.lots)} orders {len(self.book.orders)}")

    def phase_table(self):
        """Per-phase expectancy lines from this session's round trips."""
        out = []
        for tag, t in sorted(self.session["by_tag"].items()):
            n, w = t["n"], t["wins"]
            pnl = t["win_sum"] + t["loss_sum"]
            avg_w = t["win_sum"] / w if w else 0.0
            nl = n - w
            avg_l = t["loss_sum"] / nl if nl else 0.0
            pf = (t["win_sum"] / abs(t["loss_sum"])) if t["loss_sum"] < 0 else float("inf")
            out.append(f"{tag:<12} n={n:<4} win={w/n:6.1%} avgW=${avg_w:+.4f} "
                       f"avgL=${avg_l:+.4f} PF={pf:5.2f} exp=${pnl/n:+.5f}/trade "
                       f"total=${pnl:+.4f}")
        return out

# ---------------------------- Backtest ----------------------------

def fetch_history(ex, pair, days):
    """Kraken's OHLC endpoint serves only the most recent ~720 candles per
    timeframe (deep `since` is ignored), so pick the smallest timeframe that
    covers the requested span and say which one was used."""
    tf = "1d"
    for cand, sec in (("5m", 300), ("15m", 900), ("30m", 1800),
                      ("1h", 3600), ("4h", 14400), ("1d", 86400)):
        if days * 86400 / sec <= 700:
            tf = cand
            break
    candles = ex.fetch_ohlcv(pair, tf, limit=720) or []
    cutoff = ex.milliseconds() - int(days * 86400 * 1000)
    return [c for c in candles if c[0] >= cutoff], tf

def run_backtest(bot, candles, warmup=50):
    """Replay history through the live brain + fill engine. Same regimes, same
    leverage, same fees/rollover/liquidation — only the clock is the candle's."""
    peak = bot.book.equity(float(candles[warmup][4]))
    occupancy = {}
    for i in range(warmup, len(candles)):
        window = candles[max(0, i - 200):i + 1]
        c = candles[i]
        price = float(c[4])
        regime = bot._hysteresis(bot._regime(window))
        bot.phase = regime
        occupancy[regime] = occupancy.get(regime, 0) + 1
        now_iso = datetime.fromtimestamp(c[0] / 1000, tz=timezone.utc).isoformat()
        bot._bt_now = now_iso
        bot.book.process_fills(c, price, bot.fees, bot._tp_step, bot.stop_mult,
                               bot.spacing, bot._on_round_trip, now=now_iso)
        bot._plan(price, regime)
        eq = bot.book.equity(price, now_iso)
        peak = max(peak, eq)
        if peak > 0:
            bot.session["max_dd"] = max(bot.session["max_dd"], (peak - eq) / peak)
    last_px = float(candles[-1][4])
    last_iso = datetime.fromtimestamp(candles[-1][0] / 1000, tz=timezone.utc).isoformat()
    return {"final_equity": bot.book.equity(last_px, last_iso),
            "open_lots": len(bot.book.lots),
            "unrealized": bot.book.unrealized(last_px, last_iso),
            "candles": len(candles) - warmup,
            "span_days": (candles[-1][0] - candles[warmup][0]) / 86400000,
            "occupancy": occupancy}

SWEEP_SPACINGS = [0.005, 0.008, 0.012, 0.02]
SWEEP_STOPS = [2.0, 3.0, 6.0, 0.0]   # 0 = no stop (liquidation still applies at leverage)

def _sweep_bot(exchange, pair, budget, grids, min_net, tick, hyst,
               spacing, stop_mult, leverage, manual_fees, ex):
    return MidasBot(exchange, "", "", pair, paper=True, budget_usd=budget,
                    grids=grids, spacing=spacing, min_net=min_net, tick=tick,
                    log_csv=None, manual_fees=manual_fees, stop_mult=stop_mult,
                    hyst=hyst, state_path="_sw_unused.json", fresh=True,
                    equity_csv="_sw_unused.csv", persist=False,
                    leverage=leverage, ex=ex)

def _bt_stats(bot, r):
    s = bot.session
    n = s["wins"] + s["losses"]
    win_sum = sum(t["win_sum"] for t in s["by_tag"].values())
    loss_sum = sum(t["loss_sum"] for t in s["by_tag"].values())
    pf = (win_sum / abs(loss_sum)) if loss_sum < 0 else (float("inf") if win_sum > 0 else 0.0)
    return {"n": n, "win": (s["wins"] / n) if n else 0.0, "pf": pf,
            "pnl": s["realized"], "dd": s["max_dd"], "open": r["open_lots"],
            "upnl": r["unrealized"]}

def run_sweep(exchange, pair_list, budget, grids, min_net, tick, hyst, days,
              manual_fees):
    """Walk-forward parameter sweep: rank (spacing, stop, leverage) combos on
    the first 70% of history, then re-run the top 3 on the last 30% they never
    saw. In-sample rank alone is curve-fitting — the holdout column is the one
    that matters, and even it only says 'held up once', not 'will hold up'."""
    print(f"[+] SWEEP {days:g}d | {len(pair_list)} pair(s) | ${budget:g}/pair | "
          f"grid: spacing {SWEEP_SPACINGS} x stop {SWEEP_STOPS} x lev [1, catalog]")
    print("    NOTE: results are historical replay, not prediction. "
          "A config that fails holdout is noise; one that passes has survived exactly one out-of-sample test.")
    for p in pair_list:
        ex = ccxt.kraken()
        candles, tf = fetch_history(ex, p, days)
        if len(candles) < 150:
            print(f"    {p:<12} not enough history ({len(candles)} candles)")
            continue
        split = int(len(candles) * 0.7)
        train, hold = candles[:split], candles[split - 50:]
        cat = PAIR_CATALOG.get(p)
        levs = sorted({1.0, float(cat["max_leverage"]) if cat else 1.0})
        results = []
        for sp in SWEEP_SPACINGS:
            for st in SWEEP_STOPS:
                for lv in levs:
                    b = _sweep_bot(exchange, p, budget, grids, min_net, tick,
                                   hyst, sp, st, lv, manual_fees, ex)
                    b._fees_update()
                    r = run_backtest(b, train)
                    results.append(({"sp": sp, "st": st, "lv": lv}, _bt_stats(b, r)))
        results.sort(key=lambda x: -x[1]["pnl"])
        span = (candles[-1][0] - candles[0][0]) / 86400000
        print(f"\n    {p} — {span:.1f}d of {tf} candles, train 70% / holdout 30%. Top by train P/L:")
        print(f"      {'spacing':>8} {'stop':>5} {'lev':>4} | {'trips':>5} {'win%':>6} "
              f"{'PF':>5} {'P/L':>9} {'maxDD':>7}")
        for cfg, t in results[:8]:
            print(f"      {cfg['sp']:>8.3%} {cfg['st']:>5g} {cfg['lv']:>3g}x | "
                  f"{t['n']:>5} {t['win']:>6.1%} {min(t['pf'],99.99):>5.2f} "
                  f"{t['pnl']:>+9.4f} {t['dd']:>7.2%}")
        print(f"      -- holdout (last 30%, unseen) for the top 3 --")
        for cfg, t in results[:3]:
            b = _sweep_bot(exchange, p, budget, grids, min_net, tick, hyst,
                           cfg["sp"], cfg["st"], cfg["lv"], manual_fees, ex)
            b._fees_update()
            r = run_backtest(b, hold)
            h = _bt_stats(b, r)
            if t["pnl"] <= 0:
                verdict = "NEGATIVE in train — nothing to validate"
            elif h["n"] == 0 and h["open"] == 0:
                verdict = "NO ACTIVITY in holdout — unvalidated"
            elif h["n"] == 0:
                verdict = "UNVALIDATED — holdout closed nothing (open lots only)"
            elif h["pnl"] > 0:
                verdict = "HELD UP"
            else:
                verdict = "OVERFIT (train +, holdout -)"
            print(f"      {cfg['sp']:>8.3%} {cfg['st']:>5g} {cfg['lv']:>3g}x | "
                  f"train {t['pnl']:+.4f} -> holdout {h['pnl']:+.4f} "
                  f"({h['n']} trips, {h['open']} open uPnL {h['upnl']:+.4f}, "
                  f"dd {h['dd']:.2%})  {verdict}")

def report_csv(path):
    """Expectancy table grouped by (symbol, phase) from a trade log."""
    if not Path(path).exists():
        print(f"[!] No trade log at {path}")
        return
    groups = {}
    with open(path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        try:
            pnl = float(r["pnl_usd"])
        except (KeyError, ValueError):
            continue
        g = groups.setdefault((r["symbol"], r["bot"]),
                              {"n": 0, "wins": 0, "win_sum": 0.0, "loss_sum": 0.0})
        g["n"] += 1
        if pnl >= 0:
            g["wins"] += 1; g["win_sum"] += pnl
        else:
            g["loss_sum"] += pnl
    if not groups:
        print(f"[i] {path}: no completed round trips to report.")
        return
    print(f"[+] Expectancy report — {path} ({sum(g['n'] for g in groups.values())} round trips)")
    print(f"    {'symbol':<12} {'phase':<12} {'n':>4} {'win%':>6} {'avgW':>9} "
          f"{'avgL':>9} {'PF':>6} {'exp/trade':>10} {'total':>9}")
    total = 0.0
    for (sym, tag), g in sorted(groups.items()):
        n, w = g["n"], g["wins"]
        nl = n - w
        avg_w = g["win_sum"] / w if w else 0.0
        avg_l = g["loss_sum"] / nl if nl else 0.0
        pnl = g["win_sum"] + g["loss_sum"]
        total += pnl
        pf = (g["win_sum"] / abs(g["loss_sum"])) if g["loss_sum"] < 0 else float("inf")
        print(f"    {sym:<12} {tag:<12} {n:>4} {w/n:>6.1%} {avg_w:>+9.4f} "
              f"{avg_l:>+9.4f} {pf:>6.2f} {pnl/n:>+10.5f} {pnl:>+9.4f}")
    print(f"    {'TOTAL':<30} {'':>21} {total:>+9.4f}  (gross of nothing — fees/rollover already in pnl)")

# ---------------------------- Web dashboard ----------------------------

DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MidasBot Squad</title>
<style>
:root{--bg:#0d1017;--panel:#151a23;--line:#232a37;--tx:#d6dae3;--dim:#8b94a3;
--gold:#e8b84b;--green:#3fb970;--red:#e5534b;--blue:#58a6ff}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--tx);font:14px/1.45 'Segoe UI',system-ui,sans-serif;padding:18px}
h1{font-size:19px;letter-spacing:.12em;color:var(--gold)}
h1 small{color:var(--dim);letter-spacing:0;font-weight:400;margin-left:10px}
#top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin-bottom:14px}
#conn{margin-left:auto;font-size:12px;color:var(--dim)}
#conn.dead{color:var(--red);font-weight:600}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .k{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.08em}
.card .v{font-size:20px;font-weight:600;margin-top:2px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;background:var(--panel);border:1px solid var(--line);
border-radius:8px;overflow:hidden;font-variant-numeric:tabular-nums}
th{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em;text-align:right;
padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:7px 10px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
.wrap{overflow-x:auto;margin-bottom:16px}
.pos{color:var(--green)}.neg{color:var(--red)}.dim{color:var(--dim)}
.pair{font-weight:600}.lev{color:var(--gold);font-weight:600}
.ph{padding:2px 7px;border-radius:4px;font-size:11px;font-weight:600}
.ph-SCOUT{background:#20242e;color:var(--dim)}
.ph-LUNCHBOX{background:#12324f;color:var(--blue)}
.ph-REGULAR{background:#123a33;color:#4dd0b1}
.ph-AFTERBURNER{background:#43290f;color:#f0883e}
.ph-DIP{background:#2d1f47;color:#b18aff}
.stale{color:var(--red);font-weight:600}
.liq{background:#3a1210}
#chartwrap{background:var(--panel);border:1px solid var(--line);border-radius:8px;
padding:12px;margin-bottom:16px}
#chartbox{position:relative}
#chart{display:block;width:100%}
#tip{position:absolute;pointer-events:none;background:#0b0e14;border:1px solid var(--line);
border-radius:6px;padding:4px 8px;font-size:11px;display:none;white-space:nowrap}
#xline{position:absolute;top:0;bottom:0;width:1px;background:#4a5262;display:none;pointer-events:none}
#cmeta{font-size:11px;color:var(--dim);margin-top:6px}
h2{font-size:12px;color:var(--dim);text-transform:uppercase;letter-spacing:.1em;margin:0 0 8px}
#foot{font-size:11px;color:var(--dim);margin-top:10px}
</style></head><body>
<div id="top"><h1>&#9670; MIDASBOT<small id="meta"></small></h1><div id="conn">connecting&hellip;</div></div>
<div class="cards" id="cards"></div>
<div id="chartwrap"><h2>Total equity (live samples)</h2>
<div id="chartbox"><canvas id="chart" height="160"></canvas>
<div id="xline"></div><div id="tip"></div></div><div id="cmeta"></div></div>
<div class="wrap"><h2>Squad</h2><table><thead><tr>
<th>Pair</th><th>Lev</th><th>Phase</th><th>Price</th><th>Equity</th><th>Cash</th>
<th>Notional</th><th>uP/L</th><th>rP/L</th><th>Lots</th><th>Orders</th><th>DD</th><th>Age</th>
</tr></thead><tbody id="rows"></tbody></table></div>
<div class="wrap"><h2>Recent round trips</h2><table><thead><tr>
<th>Time (UTC)</th><th>Pair</th><th>Phase</th><th>Entry</th><th>Exit</th><th>P/L</th><th>Note</th>
</tr></thead><tbody id="trades"></tbody></table></div>
<div id="foot"></div>
<script>
const $=id=>document.getElementById(id);
const px=v=>{v=Number(v);if(!isFinite(v)||v===0)return'--';return parseFloat(v.toPrecision(6)).toString()}
const usd=v=>(v<0?'-$':'$')+Math.abs(v).toFixed(2);
const sgn=v=>{const n=Number(v);return `<span class="${n>=0?'pos':'neg'}">${n>=0?'+':''}${n.toFixed(3)}</span>`}
function card(k,v,cls){return `<div class="card"><div class="k">${k}</div><div class="v ${cls||''}">${v}</div></div>`}
let series=[];
function renderChart(){
 const cv=$('chart'),box=$('chartbox');
 const W=cv.width=box.clientWidth,H=cv.height=160;
 const g=cv.getContext('2d');g.clearRect(0,0,W,H);
 if(series.length<2){g.fillStyle='#8b94a3';g.font='12px Segoe UI';
  g.fillText('collecting samples… (one every 10s)',10,24);$('cmeta').textContent='';return}
 const vs=series.map(s=>s[1]);let lo=Math.min(...vs),hi=Math.max(...vs);
 if(hi-lo<1e-9){lo-=0.5;hi+=0.5}
 const pad=(hi-lo)*0.12;lo-=pad;hi+=pad;
 const X=i=>i/(series.length-1)*(W-2)+1,Y=v=>H-8-(v-lo)/(hi-lo)*(H-16);
 g.strokeStyle='#232a37';g.lineWidth=1;
 [0.25,0.5,0.75].forEach(f=>{const y=8+f*(H-16);g.beginPath();g.moveTo(0,y);g.lineTo(W,y);g.stroke()});
 const base=series[0][1];
 g.strokeStyle='#4a5262';g.setLineDash([4,4]);g.beginPath();
 g.moveTo(0,Y(base));g.lineTo(W,Y(base));g.stroke();g.setLineDash([]);
 g.beginPath();series.forEach((s,i)=>i?g.lineTo(X(i),Y(s[1])):g.moveTo(X(0),Y(s[1])));
 g.strokeStyle='#e8b84b';g.lineWidth=2;g.stroke();
 g.lineTo(X(series.length-1),H);g.lineTo(X(0),H);g.closePath();
 g.fillStyle='rgba(232,184,75,0.07)';g.fill();
 g.fillStyle='#8b94a3';g.font='11px Segoe UI';
 const vmax=Math.max(...vs),vmin=Math.min(...vs);
 g.fillText('$'+vmax.toFixed(2),4,Math.max(12,Y(vmax)-5));
 g.fillText('$'+vmin.toFixed(2),4,Math.min(H-4,Y(vmin)+13));
 const t0=new Date(series[0][0]*1000),t1=new Date(series[series.length-1][0]*1000);
 $('cmeta').textContent=`${series.length} samples · ${t0.toLocaleTimeString()} → `+
  `${t1.toLocaleTimeString()} · dashed = first sample $${base.toFixed(2)}`;
}
document.addEventListener('DOMContentLoaded',()=>{
 $('chartbox').addEventListener('mousemove',e=>{
  if(series.length<2)return;
  const r=$('chart').getBoundingClientRect();
  const i=Math.max(0,Math.min(series.length-1,Math.round((e.clientX-r.left)/r.width*(series.length-1))));
  const s=series[i];if(!s)return;
  const x=e.clientX-r.left;
  $('xline').style.display='block';$('xline').style.left=x+'px';
  const tip=$('tip');tip.style.display='block';
  tip.style.left=Math.min(x+10,r.width-130)+'px';tip.style.top='6px';
  tip.textContent=new Date(s[0]*1000).toLocaleTimeString()+' · $'+s[1].toFixed(2);
 });
 $('chartbox').addEventListener('mouseleave',()=>{
  $('tip').style.display='none';$('xline').style.display='none'});
});
async function load(){
 try{
  const r=await fetch('/api/status');const d=await r.json();
  $('conn').textContent='live \\u00b7 refreshed '+new Date().toLocaleTimeString();
  $('conn').className='';
  $('meta').textContent=` v${d.version} \\u00b7 KRAKEN \\u00b7 PAPER \\u00b7 tick ${d.tick}s`;
  series=d.equity_series||[];renderChart();
  const t=d.totals;
  $('cards').innerHTML=
   card('Total equity',usd(t.equity))+
   card('Realized P/L',usd(t.realized),t.realized>=0?'pos':'neg')+
   card('Unrealized',usd(t.unrealized),t.unrealized>=0?'pos':'neg')+
   card('Open notional',usd(t.notional))+
   card('Open lots',t.lots)+card('Resting orders',t.orders)+
   card('Round trips',`${t.wins}W / ${t.losses}L`);
  $('rows').innerHTML=d.pairs.map(p=>`<tr>
   <td class="pair">${p.pair}</td><td class="lev">${p.lev}x</td>
   <td><span class="ph ph-${p.phase}">${p.phase}</span></td>
   <td>${px(p.price)}</td><td>${usd(p.equity)}</td><td>${usd(p.cash)}</td>
   <td>${usd(p.notional)}</td><td>${sgn(p.upnl)}</td><td>${sgn(p.rpnl)}</td>
   <td>${p.lots}</td><td>${p.orders}</td><td>${(p.dd*100).toFixed(2)}%</td>
   <td class="${p.stale?'stale':'dim'}">${p.stale?'STALE ':''}${p.age==null?'--':p.age+'s'}</td>
  </tr>`).join('');
  $('trades').innerHTML=(d.trades.length?d.trades:[]).map(x=>`<tr class="${x.note.includes('LIQ')?'liq':''}">
   <td class="dim">${x.utc.slice(11,19)}</td><td class="pair">${x.symbol}</td><td>${x.bot}</td>
   <td>${px(x.entry)}</td><td>${px(x.exit)}</td><td>${sgn(x.pnl)}</td><td class="dim">${x.note}</td>
  </tr>`).join('')||'<tr><td colspan="7" class="dim">no round trips booked yet</td></tr>';
  $('foot').textContent=`data: /api/status \\u00b7 server time ${d.utc} \\u00b7 trades from ${d.log}`;
 }catch(e){
  $('conn').textContent='DISCONNECTED \\u2014 bot not running';
  $('conn').className='dead';
 }
}
load();setInterval(load,5000);
</script></body></html>"""

def build_status(bots, log_csv, samples=None):
    now = datetime.now(timezone.utc)
    pairs = []
    for b in bots:
        p = b.last_price
        held_margin = sum(l["margin"] for l in b.book.lots.values())
        eq = b.book.equity(p) if p > 0 else b.book.cash + held_margin
        age = None
        if b.last_tick_utc:
            try:
                age = int((now - datetime.fromisoformat(b.last_tick_utc)).total_seconds())
            except Exception:
                pass
        dd = ((b.book.peak_equity - eq) / b.book.peak_equity) if b.book.peak_equity > 0 else 0.0
        pairs.append({
            "pair": b.pair, "lev": f"{b.leverage:g}", "phase": b.phase,
            "price": p, "equity": round(eq, 4), "cash": round(b.book.cash, 4),
            "notional": round(sum(l["notional"] for l in b.book.lots.values()), 4),
            "upnl": round(b.book.unrealized(p) if p > 0 else 0.0, 4),
            "rpnl": round(b.book.realized, 4),
            "lots": len(b.book.lots), "orders": len(b.book.orders),
            "dd": round(max(0.0, dd), 6),
            "age": age, "stale": age is None or age > max(2 * b.tick, 30)})
    trades = []
    try:
        with LOG_LOCK, open(log_csv, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()[1:]
        for r in lines[-15:][::-1]:
            c = r.split(",")
            trades.append({"utc": c[0], "bot": c[2], "symbol": c[3],
                           "entry": c[6], "exit": c[7], "pnl": c[11], "note": c[13]})
    except Exception:
        pass
    wins = sum(b.session["wins"] for b in bots)
    losses = sum(b.session["losses"] for b in bots)
    return {"version": __version__, "utc": now_utc_str(), "tick": bots[0].tick,
            "log": log_csv, "pairs": pairs,
            "equity_series": list(samples) if samples else [],
            "totals": {"equity": round(sum(x["equity"] for x in pairs), 4),
                       "realized": round(sum(x["rpnl"] for x in pairs), 4),
                       "unrealized": round(sum(x["upnl"] for x in pairs), 4),
                       "notional": round(sum(x["notional"] for x in pairs), 4),
                       "lots": sum(x["lots"] for x in pairs),
                       "orders": sum(x["orders"] for x in pairs),
                       "wins": wins, "losses": losses},
            "trades": trades}

def make_handler(bots, log_csv, samples=None):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):   # keep the console for the bot, not HTTP noise
            pass
        def _send(self, data, ctype):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            try:
                if self.path.startswith("/api/status"):
                    self._send(json.dumps(build_status(bots, log_csv, samples)).encode("utf-8"),
                               "application/json")
                else:
                    self._send(DASH_HTML.encode("utf-8"), "text/html; charset=utf-8")
            except Exception:
                try:
                    self.send_error(500)
                except Exception:
                    pass
    return Handler

def start_dashboard(bots, log_csv, port, auto_open=True):
    samples = deque(maxlen=4320)   # 12h of 10s equity samples
    def sampler():
        while True:
            tot = 0.0
            for b in bots:
                p = b.last_price
                held = sum(l["margin"] for l in b.book.lots.values())
                tot += b.book.equity(p) if p > 0 else b.book.cash + held
            samples.append([int(time.time()), round(tot, 4)])
            time.sleep(10)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(bots, log_csv, samples))
    except OSError as e:
        print(f"[!] Dashboard could not bind 127.0.0.1:{port} ({e}); running without web UI.")
        return None
    threading.Thread(target=sampler, daemon=True).start()
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    print(f"[+] Dashboard: {url}")
    if auto_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    return httpd

# ---------------------------- CLI & config ----------------------------

def load_config(path:str|None):
    if not path: return {}
    if not Path(path).exists():
        print(f"[!] Config not found: {path}")
        return {}
    if yaml is None:
        print("[!] pyyaml not installed; ignoring config file.")
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}

def resolve_pair(ex, pair):
    # If /USD not present on the exchange, map to /USDT
    try:
        if not ex.markets:
            ex.load_markets()
        ex.market(pair)
        return pair
    except Exception:
        if pair.endswith("/USD"):
            alt = pair.replace("/USD","/USDT")
            try:
                ex.market(alt); return alt
            except Exception:
                pass
    return pair  # fallback; later calls may still fail

def main():
    parser = argparse.ArgumentParser(description="MidasBot v2 — Full Squad Single Brain (Kraken)")
    parser.add_argument("--exchange", default="kraken", choices=["kraken"])
    parser.add_argument("--pair", default="BTC/USD")
    parser.add_argument("--pairs", default="",
                        help="comma-separated pairs, or 'all' for the full catalog; overrides --pair")
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--grids", type=int, default=8)
    parser.add_argument("--spacing", type=float, default=0.005)
    parser.add_argument("--min-net", type=float, default=0.002)
    parser.add_argument("--tick", type=int, default=15)
    parser.add_argument("--leverage", type=float, default=None,
                        help="override leverage for all pairs; default = each pair's catalog max (1 = spot)")
    parser.add_argument("--stop-mult", type=float, default=3.0,
                        help="stop distance = stop_mult * spacing below entry; 0 disables")
    parser.add_argument("--hyst", type=int, default=3,
                        help="consecutive ticks required to switch phase")
    parser.add_argument("--state", default="midas_state.json")
    parser.add_argument("--fresh", action="store_true", help="discard saved state")
    parser.add_argument("--paper", action="store_true", default=True)  # default True
    parser.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--log", default=os.getenv("MIDAS_LOG","family_trades.csv"))
    parser.add_argument("--equity-log", default="equity_curve.csv")
    parser.add_argument("--web", type=int, default=8901,
                        help="dashboard port on 127.0.0.1 (0 disables)")
    parser.add_argument("--no-open", action="store_true",
                        help="don't auto-open the dashboard in the browser")
    parser.add_argument("--backtest", type=float, default=0, metavar="DAYS",
                        help="replay N days of real Kraken history through the live brain, then exit")
    parser.add_argument("--sweep", type=float, default=0, metavar="DAYS",
                        help="walk-forward parameter sweep (spacing x stop x leverage) over N days, then exit")
    parser.add_argument("--report", nargs="?", const="family_trades.csv", default=None,
                        metavar="CSV", help="print expectancy report from a trade log and exit")
    parser.add_argument("--dryrun", action="store_true", help="single tick then exit")
    parser.add_argument("--kraken-key", default=os.getenv("KRAKEN_API_KEY",""))
    parser.add_argument("--kraken-secret", default=os.getenv("KRAKEN_SECRET",""))
    parser.add_argument("--maker", type=float, default=None, help="override maker fee")
    parser.add_argument("--taker", type=float, default=None, help="override taker fee")
    args = parser.parse_args()

    if args.report is not None:
        report_csv(args.report)
        return

    cfg = load_config(args.config) if args.config else {}

    exchange = (cfg.get("exchange") or args.exchange).lower()
    pair      = cfg.get("pair") or args.pair
    budget    = float(cfg.get("budget") or args.budget)
    grids     = int(cfg.get("grids") or args.grids)
    spacing   = float(cfg.get("spacing") or args.spacing)
    min_net   = float(cfg.get("min_net") or args.min_net)
    tick      = int(cfg.get("tick") or args.tick)
    stop_mult = float(cfg["stop_mult"]) if "stop_mult" in cfg else args.stop_mult
    hyst      = int(cfg.get("hyst") or args.hyst)
    mode      = (cfg.get("mode") or ("live" if args.live else "paper")).lower()
    paper     = (mode != "live")
    if not paper and args.confirm != "I-UNDERSTAND":
        print("[!] Live mode requested but --confirm I-UNDERSTAND not provided. Falling back to paper.")
        paper = True
    if not paper:
        print("[!] Live order execution is not implemented in this build. Running paper.")
        paper = True
    log_csv   = cfg.get("log") or args.log
    manual_fees = {}
    if args.maker is not None: manual_fees["maker"] = args.maker
    if args.taker is not None: manual_fees["taker"] = args.taker
    if "fees" in cfg and isinstance(cfg["fees"], dict):
        manual_fees.update(cfg["fees"])

    # keys (Kraken only)
    api_key = args.kraken_key or os.getenv("KRAKEN_API_KEY","")
    api_sec = args.kraken_secret or os.getenv("KRAKEN_SECRET","")
    ex_tmp = ccxt.kraken()

    # pair selection: --pairs (list or 'all') overrides --pair
    pairs_arg = (cfg.get("pairs") or args.pairs or "").strip()
    if pairs_arg.lower() == "all":
        pair_list = list(PAIR_CATALOG)
    elif pairs_arg:
        pair_list = [p.strip().upper() for p in pairs_arg.split(",") if p.strip()]
    else:
        pair_list = [pair]
    pair_list = [resolve_pair(ex_tmp, p) for p in pair_list]
    multi = len(pair_list) > 1

    if args.sweep > 0:
        run_sweep(exchange, pair_list, budget / len(pair_list) if multi else budget,
                  grids, min_net, tick, hyst, args.sweep, manual_fees or None)
        return

    if args.backtest > 0:
        bt_log = "backtest_trades.csv"
        Path(bt_log).unlink(missing_ok=True)
        print(f"[+] BACKTEST {args.backtest:g}d | {len(pair_list)} pair(s) | "
              f"budget ${budget:g} (${budget/len(pair_list):.2f}/pair) | leverage per catalog")
        grand = 0.0
        for p in pair_list:
            b = MidasBot(exchange, api_key, api_sec, p, paper=True,
                         budget_usd=budget / len(pair_list), grids=grids, spacing=spacing,
                         min_net=min_net, tick=tick, log_csv=bt_log,
                         manual_fees=(manual_fees or None), stop_mult=stop_mult,
                         hyst=hyst, state_path="_bt_unused.json", fresh=True,
                         equity_csv="_bt_unused.csv", persist=False,
                         leverage=(cfg.get("leverage") or args.leverage))
            b._fees_update()
            candles, tf = fetch_history(b.ex, p, args.backtest)
            if len(candles) < 60:
                print(f"    {p:<12} not enough history ({len(candles)} candles)")
                continue
            r = run_backtest(b, candles)
            s = b.session
            n = s["wins"] + s["losses"]
            grand += s["realized"]
            occ = " ".join(f"{k}:{v}" for k, v in
                           sorted(r["occupancy"].items(), key=lambda x: -x[1]))
            print(f"    {p:<12} {b.leverage:g}x | {r['span_days']:.1f}d of {tf} candles | "
                  f"{n} trips ({s['wins']}W/{s['losses']}L) | realized ${s['realized']:+.4f} "
                  f"uPnL ${r['unrealized']:+.4f} openLots={r['open_lots']} | "
                  f"maxDD {s['max_dd']:.2%} | final eq ${r['final_equity']:.2f}")
            print(f"        regime occupancy: {occ}")
            for line in b.phase_table():
                print(f"        {line}")
        print(f"[+] Backtest total realized: ${grand:+.4f}")
        report_csv(bt_log)
        return

    # multiple bots share one public-API rate budget: slow the tick so the
    # whole squad stays around ~0.5 requests/sec against Kraken
    if multi and tick < 4 * len(pair_list):
        tick = 4 * len(pair_list)
        print(f"[i] {len(pair_list)} pairs -> tick raised to {tick}s to respect Kraken rate limits")

    per_budget = budget / len(pair_list)
    bots = []
    for p in pair_list:
        base = p.split("/")[0]
        # derive per-pair paths from --state/--equity-log so a second instance
        # pointed at different paths can never write the same files
        st = Path(args.state)
        eqp = Path(args.equity_log)
        state = str(st.with_name(f"{st.stem}_{base}.json")) if multi else args.state
        eq_csv = str(eqp.with_name(f"{eqp.stem}_{base}.csv")) if multi else args.equity_log
        bots.append(MidasBot(exchange, api_key, api_sec, p, paper=paper,
                             budget_usd=per_budget, grids=grids, spacing=spacing,
                             min_net=min_net, tick=tick, log_csv=log_csv,
                             manual_fees=(manual_fees or None), stop_mult=stop_mult,
                             hyst=hyst, state_path=state, fresh=args.fresh,
                             equity_csv=eq_csv,
                             leverage=(cfg.get("leverage") or args.leverage)))

    print(f"[+] MIDASBOT v{__version__} | KRAKEN | {len(pair_list)} pair(s) | mode={'PAPER' if paper else 'LIVE'}")
    print(f"    budget=${budget} (${per_budget:.2f}/pair) grids={grids} spacing={spacing} "
          f"min_net={min_net} tick={tick}s stop_mult={stop_mult} hyst={hyst}")
    print(f"    manual_fees={manual_fees or 'auto'} log={log_csv}")
    print("    pairs (leverage APPLIED to sizing; liq at 80% margin, rollover 0.02%/4h):")
    for b in bots:
        meta = PAIR_CATALOG.get(b.pair)
        name = meta["name"] if meta else "(not in catalog)"
        print(f"      {b.pair:<12} {b.leverage:g}x  {name}")
    bots[0]._fees_update()
    print(f"    fee viability (maker={bots[0].fees['maker']:.4f}):")
    for tag, step, net, ok in bots[0].viability():
        print(f"      {tag:<12} books {step:.3%}/trip -> net {net:+.4%}  "
              f"{'OK' if ok else 'BLOCKED (< min_net)'}")

    if args.dryrun:
        for b in bots:
            b._tick()
            print(f"[i] {b.pair:<12} {b.last_msg}")
        print(f"[i] {bots[0].summary() if not multi else f'{len(bots)} bots ticked once'}")
        print("[i] Dry run complete.")
        return

    if args.web > 0:
        start_dashboard(bots, log_csv, args.web, auto_open=not args.no_open)
    for b in bots:
        b.start()
        if multi:
            time.sleep(1)   # stagger threads so API calls don't burst together
    interval = 5 if not multi else 15
    try:
        while True:
            ts = datetime.now(timezone.utc).strftime('%H:%M:%S')
            if not multi:
                print(f"[{ts}] {bots[0].last_msg or bots[0].phase}")
            else:
                print(f"[{ts}] ---- {len(bots)} pairs ----")
                for b in bots:
                    print(f"  {b.pair:<12} {b.last_msg or b.phase}")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[!] Stopping...")
        for b in bots:
            b.stop()
        time.sleep(0.5)
        for b in bots:
            print(f"[i] {b.pair:<12} {b.summary()}")

if __name__ == "__main__":
    main()
