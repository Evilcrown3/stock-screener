"""
Multi-exchange crypto arbitrage screener (terminal).

Watches 5 coins across 4 centralized exchanges + Uniswap, all over WebSockets,
and prints a live table of prices and the best buy-here / sell-there spread.

    Coins:      BTC, ETH, SOL, LINK, UNI
    Exchanges:  Coinbase, Kraken, Bitstamp, Binance.US   (public feeds, no keys)
    On-chain:   Uniswap v3 swap events (WETH, WBTC, LINK, UNI pools)
                via your RPC provider's WebSocket (Alchemy / Infura)

Run:
    pip install -r requirements.txt
    export ETH_WS_URL="wss://eth-mainnet.g.alchemy.com/v2/YOUR_KEY"
    python crypto_screener.py

    python crypto_screener.py --size 5000       # net spread for a $5,000 trade
    python crypto_screener.py --alert 0.1       # highlight net spreads >= 0.1%
    python crypto_screener.py --no-uniswap      # exchanges only
    python crypto_screener.py --demo            # fake prices, no network

Gross = sell venue's bid vs buy venue's ask (Uniswap's pool fee is built in).
Net   = gross minus each exchange's taker fee (TAKER_FEE_PCT below) and, for a
        Uniswap leg, live Ethereum gas for one swap spread over --size.
Not included: withdrawal/transfer fees (assumes you already hold funds on both
venues, which is how cross-exchange arbitrage is normally done).
"""

import argparse
import asyncio
import csv
import json
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime

import websockets
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.table import Table
from rich.text import Text

# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

COINS = ["BTC", "ETH", "SOL", "LINK", "UNI"]

# Public RPC used only if ETH_WS_URL is not set. Fine for trying it out;
# use your own Alchemy/Infura URL for reliability.
DEFAULT_ETH_WS_URL = "wss://ethereum-rpc.publicnode.com"

STALE_SECONDS = 30          # ignore a venue's quote if it hasn't updated in this long
# Coinbase's public ticker only updates on trades, so a quiet coin (e.g. LINK)
# can go 30s+ with an unchanged, still-valid quote. Give it a longer window.
STALE_BY_VENUE = {"Coinbase": 120}
REFRESH_PER_SECOND = 4      # terminal redraw rate
LOG_EVERY_SECONDS = 10      # at most one log row per coin per this many seconds

# Taker fee per trade, in percent, at each exchange's entry-level tier
# (checked Nov 2026). EDIT these to match your own account's fee tier.
# Uniswap is 0 here because its pool fee is already built into its bid/ask.
TAKER_FEE_PCT = {
    "Coinbase": 0.90,    # Coinbase Advanced, US, under $10k/month
    "Kraken": 0.80,      # Kraken Pro, Tier 1
    "Bitstamp": 0.40,    # under $10k/month
    "Binance.US": 0.02,  # flat spot taker fee
    "Uniswap": 0.0,
}
UNISWAP_SWAP_GAS = 150_000  # gas units for a typical Uniswap v3 single-pool swap
GAS_TIP_GWEI = 0.1          # priority fee added on top of the block's base fee

DEFAULT_SIZE_USD = 1000     # trade size used to turn gas ($) into a % cost

# Exchange symbol for each coin
COINBASE = {c: f"{c}-USD" for c in COINS}
KRAKEN = {c: f"{c}/USD" for c in COINS}
BITSTAMP = {c: f"{c.lower()}usd" for c in COINS}
BINANCE_US = {c: f"{c}USD" for c in COINS}

# Uniswap v3 mainnet pools. The script reads token0/token1/decimals/fee from
# the chain at startup and checks them, so a wrong address is caught, not
# silently mispriced.
WETH = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"
USDC = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
WBTC = "0x2260fac5e5542a773aa44fbcfedf7c193bc2c599"
LINK = "0x514910771af9ca656af840dff83e8264ecf986ca"
UNI = "0x1f9840a85d5af5bf1d1762f925bdaddc4201f984"

UNISWAP_POOLS = [
    # coin, pool address, coin token, quote token
    ("ETH", "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640", WETH, USDC),  # WETH/USDC 0.05%
    ("BTC", "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35", WBTC, USDC),  # WBTC/USDC 0.3%
    ("LINK", "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8", LINK, WETH),  # LINK/WETH 0.3%
    ("UNI", "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801", UNI, WETH),   # UNI/WETH 0.3%
    # SOL has no deep native pool on Ethereum mainnet, so it is CEX-only.
]

SWAP_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"
SEL_FEE = "0xddca3f43"
SEL_SLOT0 = "0x3850c7bd"

VENUES = ["Coinbase", "Kraken", "Bitstamp", "Binance.US", "Uniswap"]

# --------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------


@dataclass
class Quote:
    bid: float
    ask: float
    ts: float = field(default_factory=time.time)

    @property
    def mid(self):
        return (self.bid + self.ask) / 2


# quotes[coin][venue] -> Quote
quotes = {c: {} for c in COINS}
# status[venue] -> short text shown under the table
status = {v: "starting" for v in VENUES}
# Live Ethereum gas info from the Uniswap connection
gas = {"base_fee_wei": 0, "eth_usd": 0.0}
settings = {"size_usd": DEFAULT_SIZE_USD}


def put(coin, venue, bid, ask):
    if coin in quotes and bid > 0 and ask > 0:
        quotes[coin][venue] = Quote(bid, ask)


# --------------------------------------------------------------------------
# Message parsers (pure functions: raw message -> [(coin, bid, ask), ...])
# --------------------------------------------------------------------------

def _reverse(mapping):
    return {v: k for k, v in mapping.items()}


_CB = _reverse(COINBASE)
_KR = _reverse(KRAKEN)
_BS = _reverse(BITSTAMP)
_BN = _reverse(BINANCE_US)


def parse_coinbase(msg):
    if msg.get("type") != "ticker":
        return []
    coin = _CB.get(msg.get("product_id"))
    if not coin or not msg.get("best_bid") or not msg.get("best_ask"):
        return []
    return [(coin, float(msg["best_bid"]), float(msg["best_ask"]))]


def parse_kraken(msg):
    if msg.get("channel") != "ticker":
        return []
    out = []
    for d in msg.get("data", []):
        coin = _KR.get(d.get("symbol"))
        if coin and d.get("bid") and d.get("ask"):
            out.append((coin, float(d["bid"]), float(d["ask"])))
    return out


def parse_bitstamp(msg):
    if msg.get("event") != "data":
        return []
    pair = msg.get("channel", "").replace("order_book_", "")
    coin = _BS.get(pair)
    data = msg.get("data", {})
    if not coin or not data.get("bids") or not data.get("asks"):
        return []
    return [(coin, float(data["bids"][0][0]), float(data["asks"][0][0]))]


def parse_binance_us(msg):
    d = msg.get("data", msg)
    coin = _BN.get(d.get("s"))
    if not coin or "b" not in d or "a" not in d:
        return []
    return [(coin, float(d["b"]), float(d["a"]))]


# --------------------------------------------------------------------------
# Centralized exchange feeds
# --------------------------------------------------------------------------

def coinbase_feed():
    return (
        "wss://ws-feed.exchange.coinbase.com",
        [{"type": "subscribe", "product_ids": list(COINBASE.values()), "channels": ["ticker"]}],
        parse_coinbase,
    )


def kraken_feed():
    return (
        "wss://ws.kraken.com/v2",
        [{"method": "subscribe", "params": {"channel": "ticker", "symbol": list(KRAKEN.values()),
                                                 "event_trigger": "bbo"}}],
        parse_kraken,
    )


def bitstamp_feed():
    return (
        "wss://ws.bitstamp.net",
        [{"event": "bts:subscribe", "data": {"channel": f"order_book_{p}"}} for p in BITSTAMP.values()],
        parse_bitstamp,
    )


def binance_us_feed():
    streams = "/".join(f"{s.lower()}@bookTicker" for s in BINANCE_US.values())
    return (f"wss://stream.binance.us:9443/stream?streams={streams}", [], parse_binance_us)


async def run_exchange(venue, feed):
    """Connect, subscribe, push quotes; reconnect with backoff forever."""
    url, subs, parser = feed
    backoff = 1
    while True:
        try:
            status[venue] = "connecting"
            async with websockets.connect(url, ping_interval=20, max_size=2**22) as ws:
                for s in subs:
                    await ws.send(json.dumps(s))
                status[venue] = "live"
                backoff = 1
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("event") == "bts:request_reconnect":  # Bitstamp asks us to reconnect
                        break
                    for coin, bid, ask in parser(msg):
                        put(coin, venue, bid, ask)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            status[venue] = f"retry in {backoff}s ({type(e).__name__})"
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# --------------------------------------------------------------------------
# Uniswap v3 (on-chain) feed
# --------------------------------------------------------------------------

def sqrt_price_to_price(sqrt_price_x96, dec0, dec1):
    """Human price of token0 measured in token1."""
    return (sqrt_price_x96 / 2**96) ** 2 * 10 ** (dec0 - dec1)


def decode_swap_sqrt_price(data_hex):
    """Swap event data = amount0, amount1, sqrtPriceX96, liquidity, tick (32 bytes each)."""
    data = data_hex[2:] if data_hex.startswith("0x") else data_hex
    return int(data[128:192], 16)


@dataclass
class Pool:
    coin: str
    address: str
    coin_token: str
    quote_token: str
    coin_is_token0: bool = True
    dec0: int = 18
    dec1: int = 18
    fee: float = 0.003           # 0.003 = 0.3%
    price_in_quote: float = 0.0  # coin priced in the quote token (USDC or WETH)


def pool_price_in_quote(pool, sqrt_price_x96):
    p = sqrt_price_to_price(sqrt_price_x96, pool.dec0, pool.dec1)  # token0 in token1
    if p == 0:
        return 0.0
    return p if pool.coin_is_token0 else 1 / p


def publish_uniswap(pools):
    """Turn pool prices into USD quotes. WETH-quoted pools use Uniswap's own ETH/USDC price."""
    eth_usd = next((p.price_in_quote for p in pools if p.coin == "ETH"), 0.0)
    if eth_usd:
        gas["eth_usd"] = eth_usd
    for p in pools:
        if not p.price_in_quote:
            continue
        if p.quote_token == USDC:
            usd = p.price_in_quote
        elif p.quote_token == WETH and eth_usd:
            usd = p.price_in_quote * eth_usd
        else:
            continue
        # Swapping through the pool costs the pool fee, so build a bid/ask around the mid.
        put(p.coin, "Uniswap", usd * (1 - p.fee), usd * (1 + p.fee))


class Rpc:
    """
    Minimal JSON-RPC over one WebSocket. A background reader routes replies to
    the call waiting for them and puts subscription events on `notifications`,
    so we can make calls (e.g. read a pool's price) while subscribed.
    """

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.pending = {}
        self.notifications = asyncio.Queue()

    async def read_forever(self):
        try:
            async for raw in self.ws:
                msg = json.loads(raw)
                fut = self.pending.pop(msg.get("id"), None)
                if fut and not fut.done():
                    if "error" in msg:
                        fut.set_exception(RuntimeError(msg["error"]))
                    else:
                        fut.set_result(msg.get("result"))
                elif msg.get("method") == "eth_subscription":
                    self.notifications.put_nowait(msg.get("params", {}))
        finally:
            for fut in self.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            self.notifications.put_nowait(None)  # wake the main loop so it reconnects

    async def call(self, method, params):
        self.next_id += 1
        fut = asyncio.get_running_loop().create_future()
        self.pending[self.next_id] = fut
        await self.ws.send(json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=20)

    async def eth_call(self, to, data):
        return await self.call("eth_call", [{"to": to, "data": data}, "latest"])


def _word(hex_result, index=0):
    h = hex_result[2:]
    return h[index * 64:(index + 1) * 64]


def _addr(hex_result):
    return "0x" + _word(hex_result)[-40:]


async def read_pool_price(rpc, pool):
    slot0 = await rpc.eth_call(pool.address, SEL_SLOT0)
    pool.price_in_quote = pool_price_in_quote(pool, int(_word(slot0, 0), 16))


async def load_pool(rpc, pool):
    t0 = _addr(await rpc.eth_call(pool.address, SEL_TOKEN0)).lower()
    t1 = _addr(await rpc.eth_call(pool.address, SEL_TOKEN1)).lower()
    if {t0, t1} != {pool.coin_token, pool.quote_token}:
        raise ValueError(f"{pool.coin} pool {pool.address} has unexpected tokens {t0}, {t1}")
    pool.coin_is_token0 = t0 == pool.coin_token
    pool.dec0 = int(await rpc.eth_call(t0, SEL_DECIMALS), 16)
    pool.dec1 = int(await rpc.eth_call(t1, SEL_DECIMALS), 16)
    pool.fee = int(await rpc.eth_call(pool.address, SEL_FEE), 16) / 1_000_000
    await read_pool_price(rpc, pool)


async def run_uniswap(ws_url):
    """
    Prices come from two sources:
      1. Swap events (instant, when the RPC provider delivers them).
      2. On every new block, any pool that had no swap event since the last block
         gets its price read directly from the chain (slot0). This keeps prices
         correct even if the provider doesn't deliver swap events (some free
         public endpoints don't), and costs nothing extra for pools that did swap.
    """
    pools = [Pool(coin, addr.lower(), ct, qt) for coin, addr, ct, qt in UNISWAP_POOLS]
    by_address = {p.address: p for p in pools}
    backoff = 1
    while True:
        reader = None
        try:
            status["Uniswap"] = "connecting"
            async with websockets.connect(ws_url, ping_interval=20, max_size=2**22) as ws:
                rpc = Rpc(ws)
                reader = asyncio.create_task(rpc.read_forever())
                for p in pools:
                    await load_pool(rpc, p)
                publish_uniswap(pools)
                block = await rpc.call("eth_getBlockByNumber", ["latest", False])
                gas["base_fee_wei"] = int(block.get("baseFeePerGas", "0x0"), 16)
                swaps_sub = await rpc.call("eth_subscribe", ["logs", {"address": list(by_address), "topics": [SWAP_TOPIC]}])
                heads_sub = await rpc.call("eth_subscribe", ["newHeads"])
                status["Uniswap"] = "live"
                backoff = 1
                swaps = 0
                swapped = set()  # pools with a swap event since the last block
                while True:
                    # No block for 60s means the connection is dead: reconnect.
                    params = await asyncio.wait_for(rpc.notifications.get(), timeout=60)
                    if params is None:
                        raise ConnectionError("connection closed")
                    result = params.get("result")
                    if not isinstance(result, dict):
                        continue
                    if params.get("subscription") == heads_sub:
                        if result.get("baseFeePerGas"):
                            gas["base_fee_wei"] = int(result["baseFeePerGas"], 16)
                        for p in pools:
                            if p.address not in swapped:
                                await read_pool_price(rpc, p)
                        swapped.clear()
                        publish_uniswap(pools)  # also re-stamps quotes, so they stay fresh
                        status["Uniswap"] = f"live ({swaps} swaps)"
                    elif params.get("subscription") == swaps_sub and not result.get("removed"):
                        pool = by_address.get(result.get("address", "").lower())
                        if pool:
                            pool.price_in_quote = pool_price_in_quote(pool, decode_swap_sqrt_price(result["data"]))
                            publish_uniswap(pools)
                            swapped.add(pool.address)
                            swaps += 1
        except asyncio.CancelledError:
            raise
        except ValueError as e:      # bad pool config: don't hammer, show why
            status["Uniswap"] = f"config error: {e}"
            backoff = 60
        except Exception as e:
            status["Uniswap"] = f"retry in {backoff}s ({type(e).__name__})"
        finally:
            if reader:
                reader.cancel()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)


# --------------------------------------------------------------------------
# Spread calculation
# --------------------------------------------------------------------------

def is_stale(venue, quote, now):
    return now - quote.ts > STALE_BY_VENUE.get(venue, STALE_SECONDS)


def uniswap_gas_usd():
    """Dollar cost of one Uniswap swap at the current base fee (0 if unknown)."""
    if not gas["base_fee_wei"] or not gas["eth_usd"]:
        return 0.0
    wei = (gas["base_fee_wei"] + GAS_TIP_GWEI * 1e9) * UNISWAP_SWAP_GAS
    return wei / 1e18 * gas["eth_usd"]


def net_pct(buy_v, sell_v, gross_pct, size_usd=None):
    """Gross spread minus both taker fees and Uniswap gas (as % of trade size)."""
    size_usd = size_usd or settings["size_usd"]
    net = gross_pct - TAKER_FEE_PCT.get(buy_v, 0) - TAKER_FEE_PCT.get(sell_v, 0)
    for leg in (buy_v, sell_v):
        if leg == "Uniswap":
            net -= uniswap_gas_usd() / size_usd * 100
    return net


def best_spread(coin_quotes, now=None):
    """
    Best "buy on A at A's ask, sell on B at B's bid" across different venues,
    ranked by NET spread (after fees and gas).
    Returns (buy_venue, sell_venue, buy_price, sell_price, gross_pct, net_pct) or None.
    """
    now = now or time.time()
    fresh = {v: q for v, q in coin_quotes.items() if not is_stale(v, q, now)}
    best = None
    for buy_v, bq in fresh.items():
        for sell_v, sq in fresh.items():
            if buy_v == sell_v:
                continue
            gross = (sq.bid - bq.ask) / bq.ask * 100
            net = net_pct(buy_v, sell_v, gross)
            if best is None or net > best[5]:
                best = (buy_v, sell_v, bq.ask, sq.bid, gross, net)
    return best


# --------------------------------------------------------------------------
# Terminal display + alert log
# --------------------------------------------------------------------------

def fmt_price(x):
    if x >= 10000:
        return f"{x:,.0f}"
    if x >= 1000:
        return f"{x:,.1f}"
    if x >= 100:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.3f}"
    return f"{x:.5f}"


# Short names keep the table narrow enough for a phone / Codespace terminal.
SHORT = {"Coinbase": "Coinbase", "Kraken": "Kraken", "Bitstamp": "Bitstamp",
         "Binance.US": "Binance", "Uniswap": "Uniswap"}
CODE = {"Coinbase": "CB", "Kraken": "KRK", "Bitstamp": "BST", "Binance.US": "BIN", "Uniswap": "UNI"}


def build_view(alert_pct, venues):
    now = time.time()
    table = Table(title=f"Crypto Arb Screener  ·  {datetime.now().strftime('%I:%M:%S %p')}",
                  title_justify="left", box=box.SIMPLE_HEAD, pad_edge=False, collapse_padding=True)
    table.add_column("Coin", style="bold", no_wrap=True)
    for v in venues:
        table.add_column(SHORT[v], justify="right", no_wrap=True)
    table.add_column("Buy→Sell", no_wrap=True)
    table.add_column("Gross", justify="right", no_wrap=True)
    table.add_column("Net", justify="right", no_wrap=True)

    for coin in COINS:
        row = [coin]
        cq = quotes[coin]
        for v in venues:
            q = cq.get(v)
            if not q:
                row.append(Text("—", style="dim"))
            elif is_stale(v, q, now):
                row.append(Text(fmt_price(q.mid), style="dim strike"))
            else:
                row.append(fmt_price(q.mid))
        b = best_spread(cq, now)
        if b:
            buy_v, sell_v, buy_p, sell_p, gross, net = b
            style = "bold black on green" if net >= alert_pct else "red"
            row += [f"{CODE[buy_v]}→{CODE[sell_v]}",
                    Text(f"{gross:+.2f}%", style="green" if gross > 0 else "dim"),
                    Text(f"{net:+.2f}%", style=style)]
        else:
            row += [Text("need 2 venues", style="dim"), "", ""]
        table.add_row(*row)

    status_line = Text()
    for v in venues:
        s = status[v]
        status_line.append(f"{SHORT[v]}: ", style="bold")
        status_line.append(f"{s}  ", style="green" if s.startswith("live") else "yellow")
    g = uniswap_gas_usd()
    gas_text = f"${g:,.2f}/swap" if g else "waiting"
    fees = " ".join(f"{CODE[v]} {TAKER_FEE_PCT[v]:g}%" for v in venues if v != "Uniswap")
    cost_line = Text(f"Trade size ${settings['size_usd']:,.0f} · Uniswap gas {gas_text} · Taker fees: {fees}",
                     style="cyan")
    footer = Text(f"Net = gross − taker fees − gas. Green when net ≥ {alert_pct}%. Struck-out = stale. "
                  "CB=Coinbase KRK=Kraken BST=Bitstamp BIN=Binance.US UNI=Uniswap. Ctrl+C to quit.", style="dim")
    return Group(table, status_line, cost_line, footer)


class AlertLog:
    def __init__(self, path):
        self.path = path
        self.last = {}
        if path and not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(["time", "coin", "buy_on", "buy_price", "sell_on", "sell_price",
                                        "gross_pct", "net_pct", "size_usd"])

    def check(self, alert_pct):
        if not self.path:
            return
        now = time.time()
        for coin in COINS:
            b = best_spread(quotes[coin], now)
            if b and b[5] >= alert_pct and now - self.last.get(coin, 0) >= LOG_EVERY_SECONDS:
                self.last[coin] = now
                with open(self.path, "a", newline="") as f:
                    csv.writer(f).writerow([datetime.now().isoformat(timespec="seconds"), coin,
                                            b[0], b[2], b[1], b[3], round(b[4], 4), round(b[5], 4),
                                            settings["size_usd"]])


# --------------------------------------------------------------------------
# Demo mode (fake prices, no network) — handy for checking the display
# --------------------------------------------------------------------------

async def run_demo(venues):
    base = {"BTC": 90000, "ETH": 3200, "SOL": 160, "LINK": 18, "UNI": 9}
    for v in venues:
        status[v] = "live"
    gas.update(base_fee_wei=int(2e9), eth_usd=3200.0)
    while True:
        for coin, px in base.items():
            base[coin] = px * (1 + random.gauss(0, 0.0005))
            for v in venues:
                if v == "Uniswap" and coin == "SOL":
                    continue
                mid = base[coin] * (1 + random.gauss(0, 0.002))
                half = mid * 0.0001
                put(coin, v, mid - half, mid + half)
        await asyncio.sleep(0.5)


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

async def main(args):
    venues = [v for v in VENUES if not (args.no_uniswap and v == "Uniswap")]
    settings["size_usd"] = args.size
    if args.demo:
        tasks = [run_demo(venues)]
    else:
        tasks = [
            run_exchange("Coinbase", coinbase_feed()),
            run_exchange("Kraken", kraken_feed()),
            run_exchange("Bitstamp", bitstamp_feed()),
            run_exchange("Binance.US", binance_us_feed()),
        ]
        if not args.no_uniswap:
            tasks.append(run_uniswap(os.environ.get("ETH_WS_URL") or DEFAULT_ETH_WS_URL))

    log = AlertLog(None if args.no_log else args.log)
    running = [asyncio.create_task(t) for t in tasks]
    try:
        with Live(build_view(args.alert, venues), console=Console(), refresh_per_second=REFRESH_PER_SECOND,
                  screen=False) as live:
            while True:
                await asyncio.sleep(1 / REFRESH_PER_SECOND)
                live.update(build_view(args.alert, venues))
                log.check(args.alert)
    finally:
        for t in running:
            t.cancel()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description="Live multi-exchange crypto arbitrage screener")
    ap.add_argument("--alert", type=float, default=0.0,
                    help="highlight/log when the NET spread is at or above this %% (default 0.0 = any profit)")
    ap.add_argument("--size", type=float, default=DEFAULT_SIZE_USD,
                    help=f"trade size in USD used for the gas cost (default {DEFAULT_SIZE_USD})")
    ap.add_argument("--no-uniswap", action="store_true", help="skip the on-chain Uniswap feed")
    ap.add_argument("--log", default="arb_alerts.csv", help="CSV file for net spreads over the alert level")
    ap.add_argument("--no-log", action="store_true", help="don't write the CSV log")
    ap.add_argument("--demo", action="store_true", help="fake prices, no network (test the display)")
    return ap.parse_args(argv)


if __name__ == "__main__":
    try:
        asyncio.run(main(parse_args()))
    except KeyboardInterrupt:
        pass
