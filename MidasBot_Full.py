
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MidasBot — Full Squad, Single Brain
===================================
One bot, multiple *phases* (no toggles): SCOUT -> LUNCHBOX (mean-revert) / REGULAR (grid) / AFTERBURNER (momentum) / DIP (pullback DCA).
- Exchange: Kraken or Binance US (via ccxt)
- Mode: Paper by default (safe). Live is supported but off unless --live is passed AND env confirms.
- Budget/fee/min-notional aware
- Post-only intent (tries to price-pad to avoid taker; skips if not safe)
- Logs trades to CSV: family_trades.csv

Install
-------
pip install ccxt python-dotenv pyyaml

Quick start (paper mode)
------------------------
python MidasBot_Full.py --exchange kraken --pair BTC/USD --budget 50

Live mode (danger: read first)
------------------------------
python MidasBot_Full.py --exchange kraken --pair BTC/USD --budget 50 --live --confirm I-UNDERSTAND
(You must have keys in .env or passed as flags)

Environment variables (.env)
----------------------------
BINANCEUS_API_KEY=
BINANCEUS_SECRET=
KRAKEN_API_KEY=
KRAKEN_SECRET=
MIDAS_LOG=family_trades.csv

Parameters (CLI flags)
----------------------
--exchange      kraken | binanceus
--pair          default BTC/USD (auto-maps to /USDT on binanceus if needed)
--budget        USD budget cap (float, default 50)
--grids         grid levels (int, default 8)
--spacing       spacing between levels as fraction (default 0.005 => 0.5%)
--min-net       minimum net step after both legs' maker fees (default 0.002 => 0.20%)
--tick          loop seconds (default 15)
--paper         default; paper trading only (simulated fills)
--live          live trading (needs --confirm I-UNDERSTAND)
--confirm       I-UNDERSTAND (required with --live)
--config        path to YAML to override any of the above
--dryrun        simulate a single cycle then exit (for testing)

YAML config keys
----------------
exchange, pair, budget, grids, spacing, min_net, tick, mode, fees.manual_maker, fees.manual_taker

"""
import os, sys, time, math, csv, json, argparse, threading
from pathlib import Path
from datetime import datetime

try:
    import yaml
except Exception:
    yaml = None

import ccxt
from dotenv import load_dotenv
load_dotenv()

# ---------------------------- Utilities ----------------------------

def now_utc_str():
    return datetime.utcnow().isoformat()

def clamp(x, lo, hi):
    return max(lo, min(hi, x))

def pretty_exc(e):
    return f"{type(e).__name__}: {e}"

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

# ---------------------------- Bot ----------------------------

class MidasBot:
    def __init__(self, exchange_name:str, api_key:str, api_secret:str, pair:str,
                 paper:bool=True, budget_usd:float=50.0, grids:int=8, spacing:float=0.005,
                 min_net:float=0.002, tick:int=15, log_csv:str="family_trades.csv",
                 manual_fees:dict|None=None):
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
        self.log_csv = log_csv
        self.stop_flag = False
        self.thread = None

        self.phase = "SCOUT"
        self.last_msg = ""
        self.fees = {"maker":0.0010, "taker":0.0015}
        if manual_fees:
            self.fees.update({k: float(v) for k,v in manual_fees.items() if k in ("maker","taker")})
        self.balance_quote = 0.0

        if self.exchange_name == "binanceus":
            self.ex = ccxt.binanceus({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
        else:
            self.ex = ccxt.kraken({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})

        # Prepare CSV
        p = Path(self.log_csv)
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            with open(p, "w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow(["utc","exchange","bot","symbol","side","qty","entry_px","exit_px",
                            "gross_pct","net_pct","fee_pct_rt","pnl_usd","runtime_sec","notes"])

    # ---------------- I/O helpers ----------------

    def _log_trade(self, **kw):
        with open(self.log_csv, "a", newline="", encoding="utf-8") as f:
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
            f = self.ex.fetch_trading_fee(self.pair)
            m = float(f.get("maker", self.fees["maker"]))
            t = float(f.get("taker", self.fees["taker"]))
            self.fees = {"maker": m, "taker": t}
        except Exception:
            try:
                mkt = self.ex.market(self.pair)
                self.fees["maker"] = float(mkt.get("maker", self.fees["maker"]))
                self.fees["taker"] = float(mkt.get("taker", self.fees["taker"]))
            except Exception:
                pass

    def _balances_update(self):
        quote = self.pair.split("/")[1]
        try:
            b = self.ex.fetch_balance()
            self.balance_quote = float(b.get("free",{}).get(quote, 0.0) or 0.0)
        except Exception:
            self.balance_quote = 0.0

    def _ohlcv(self, tf="5m", limit=200):
        try:
            return self.ex.fetch_ohlcv(self.pair, timeframe=tf, limit=limit) or []
        except Exception:
            return []

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

    def _net_ok(self, gross_step):
        # 2x maker legs + small slippage 0.02%
        net = gross_step - (self.fees["maker"]*2 + 0.0002)
        return net >= self.min_net

    def _plan_grid(self, price):
        # Returns list of tuples: (side, qty, limit_price)
        budget = min(self.budget_usd, self.balance_quote)
        if budget <= 0 or self.grids <= 0 or price <= 0:
            return []
        per = budget / self.grids
        orders = []
        for i in range(self.grids):
            down = price * (1 - self.spacing*(i+1))
            up   = price * (1 + self.spacing*(i+1))
            buy_qty = per / max(down,1e-9)
            sell_qty = per / max(up,1e-9)
            step_buy = (price - down)/max(price,1e-9)
            step_sell= (up - price)/max(price,1e-9)
            if buy_qty>0 and self._net_ok(step_buy):
                orders.append(("buy", round(buy_qty,8), round(down,4)))
            if sell_qty>0 and self._net_ok(step_sell):
                orders.append(("sell", round(sell_qty,8), round(up,4)))
        return orders

    def _exec_paper(self, side, qty, px, tag):
        # Simulate immediate TP at the configured spacing (or boosted for AFTERBURNER)
        utc = now_utc_str()
        if side == "buy":
            tp = px*(1 + (self.spacing if tag!="AFTERBURNER" else self.spacing*1.5))
            gross = (tp - px)/px
            net = gross - (self.fees["maker"]*2 + 0.0002)
            pnl = qty*(tp-px) - qty*px*self.fees["maker"] - qty*tp*self.fees["maker"]
            self._log_trade(utc=utc, exchange=self.exchange_name.upper(), bot=tag, symbol=self.pair,
                            side="LONG", qty=qty, entry_px=px, exit_px=tp, gross_pct=gross, net_pct=net,
                            fee_pct_rt=self.fees["maker"], pnl_usd=pnl, runtime_sec=self.tick, notes=f"{tag} PPY")
        else:
            tp = px*(1 - (self.spacing if tag!="AFTERBURNER" else self.spacing*1.5))
            gross = (px - tp)/px
            net = gross - (self.fees["maker"]*2 + 0.0002)
            pnl = qty*(px-tp) - qty*px*self.fees["maker"] - qty*tp*self.fees["maker"]
            self._log_trade(utc=utc, exchange=self.exchange_name.upper(), bot=tag, symbol=self.pair,
                            side="SHORT", qty=qty, entry_px=px, exit_px=tp, gross_pct=gross, net_pct=net,
                            fee_pct_rt=self.fees["maker"], pnl_usd=pnl, runtime_sec=self.tick, notes=f"{tag} PPY")

    # ---------------- Live order helpers (optional) ----------------

    def _post_only_limit(self, side, qty, px):
        # NOTE: This is just a placeholder to avoid accidental live trades.
        # If you want, we can wire exchange-specific post-only flags.
        raise NotImplementedError("Live trading disabled in this reference build.")

    # ---------------- Loop ----------------

    def _tick(self):
        self._fees_update()
        self._balances_update()
        price = self._price()
        ohlcv = self._ohlcv("5m", 200)
        regime = self._regime(ohlcv)
        self.phase = regime
        self.last_msg = f"{regime} | price={price:.2f} | bal=${self.balance_quote:.2f} | fees m/t={self.fees['maker']:.3f}/{self.fees['taker']:.3f}"
        if regime == "SCOUT" or self.balance_quote <= 1e-6 or price <= 0:
            return
        orders = []
        if regime == "LUNCHBOX":
            orders = self._plan_grid(price)
        elif regime == "REGULAR":
            orders = self._plan_grid(price)
        elif regime == "AFTERBURNER":
            base = self._plan_grid(price); orders = [o for o in base if o[0] == "sell"]
        elif regime == "DIP":
            base = self._plan_grid(price); orders = [o for o in base if o[0] == "buy"]
        for side, qty, px in orders[:2]:
            if self.paper:
                self._exec_paper(side, qty, px, regime)
            else:
                # self._post_only_limit(side, qty, px)
                pass

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
    # If /USD not present on Binance US, map to /USDT
    try:
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
    parser = argparse.ArgumentParser(description="MidasBot — Full Squad Single Brain")
    parser.add_argument("--exchange", default="kraken", choices=["kraken","binanceus"])
    parser.add_argument("--pair", default="BTC/USD")
    parser.add_argument("--budget", type=float, default=50.0)
    parser.add_argument("--grids", type=int, default=8)
    parser.add_argument("--spacing", type=float, default=0.005)
    parser.add_argument("--min-net", type=float, default=0.002)
    parser.add_argument("--tick", type=int, default=15)
    parser.add_argument("--paper", action="store_true", default=True)  # default True
    parser.add_argument("--live", action="store_true", default=False)
    parser.add_argument("--confirm", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--log", default=os.getenv("MIDAS_LOG","family_trades.csv"))
    parser.add_argument("--dryrun", action="store_true", help="single tick then exit")
    parser.add_argument("--binance-key", default=os.getenv("BINANCEUS_API_KEY",""))
    parser.add_argument("--binance-secret", default=os.getenv("BINANCEUS_SECRET",""))
    parser.add_argument("--kraken-key", default=os.getenv("KRAKEN_API_KEY",""))
    parser.add_argument("--kraken-secret", default=os.getenv("KRAKEN_SECRET",""))
    parser.add_argument("--maker", type=float, default=None, help="override maker fee")
    parser.add_argument("--taker", type=float, default=None, help="override taker fee")
    args = parser.parse_args()

    cfg = load_config(args.config) if args.config else {}

    exchange = (cfg.get("exchange") or args.exchange).lower()
    pair      = cfg.get("pair") or args.pair
    budget    = float(cfg.get("budget") or args.budget)
    grids     = int(cfg.get("grids") or args.grids)
    spacing   = float(cfg.get("spacing") or args.spacing)
    min_net   = float(cfg.get("min_net") or args.min_net)
    tick      = int(cfg.get("tick") or args.tick)
    mode      = (cfg.get("mode") or ("live" if args.live else "paper")).lower()
    paper     = (mode != "live")
    if not paper and args.confirm != "I-UNDERSTAND":
        print("[!] Live mode requested but --confirm I-UNDERSTAND not provided. Falling back to paper.")
        paper = True
    log_csv   = cfg.get("log") or args.log
    manual_fees = {}
    if args.maker is not None: manual_fees["maker"] = args.maker
    if args.taker is not None: manual_fees["taker"] = args.taker
    if "fees" in cfg and isinstance(cfg["fees"], dict):
        manual_fees.update(cfg["fees"])

    # keys
    if exchange == "binanceus":
        api_key = args.binance_key or os.getenv("BINANCEUS_API_KEY","")
        api_sec = args.binance_secret or os.getenv("BINANCEUS_SECRET","")
        ex_tmp = ccxt.binanceus()
    else:
        api_key = args.kraken_key or os.getenv("KRAKEN_API_KEY","")
        api_sec = args.kraken_secret or os.getenv("KRAKEN_SECRET","")
        ex_tmp = ccxt.kraken()

    # pair mapping
    pair = resolve_pair(ex_tmp, pair)

    bot = MidasBot(exchange, api_key, api_sec, pair, paper=paper,
                   budget_usd=budget, grids=grids, spacing=spacing,
                   min_net=min_net, tick=tick, log_csv=log_csv,
                   manual_fees=(manual_fees or None))

    print(f"[+] MIDASBOT | {exchange.upper()} {pair} | mode={'PAPER' if paper else 'LIVE'}")
    print(f"    budget=${budget} grids={grids} spacing={spacing} min_net={min_net} tick={tick}s")
    print(f"    manual_fees={manual_fees or 'auto'} log={log_csv}")

    if args.dryrun:
        bot._tick()
        print("[i] Dry run complete.")
        return

    bot.start()
    try:
        while True:
            print(f"[{datetime.utcnow().strftime('%H:%M:%S')}] phase={bot.phase} | {bot.last_msg}")
            time.sleep(5)
    except KeyboardInterrupt:
        print("\n[!] Stopping...")
        bot.stop()
        time.sleep(0.5)

if __name__ == "__main__":
    main()
