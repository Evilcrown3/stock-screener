import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import crypto_screener as cs  # noqa: E402


def sqrt_x96_for(price_token0_in_token1, dec0, dec1):
    raw = price_token0_in_token1 * 10 ** (dec1 - dec0)
    return int(math.sqrt(raw) * 2**96)


def test_parse_coinbase():
    msg = {"type": "ticker", "product_id": "ETH-USD", "price": "3200.5", "best_bid": "3200.4", "best_ask": "3200.6"}
    assert cs.parse_coinbase(msg) == [("ETH", 3200.4, 3200.6)]
    assert cs.parse_coinbase({"type": "subscriptions"}) == []


def test_parse_kraken():
    msg = {"channel": "ticker", "type": "update",
           "data": [{"symbol": "BTC/USD", "bid": 90000.1, "ask": 90000.2, "last": 90000.1}]}
    assert cs.parse_kraken(msg) == [("BTC", 90000.1, 90000.2)]
    assert cs.parse_kraken({"channel": "heartbeat"}) == []


def test_parse_bitstamp():
    msg = {"event": "data", "channel": "order_book_linkusd",
           "data": {"bids": [["18.01", "100"], ["18.00", "5"]], "asks": [["18.03", "50"]]}}
    assert cs.parse_bitstamp(msg) == [("LINK", 18.01, 18.03)]
    assert cs.parse_bitstamp({"event": "bts:subscription_succeeded"}) == []


def test_parse_binance_us():
    msg = {"stream": "solusd@bookTicker", "data": {"u": 1, "s": "SOLUSD", "b": "160.10", "B": "3", "a": "160.20", "A": "2"}}
    assert cs.parse_binance_us(msg) == [("SOL", 160.10, 160.20)]


def test_decode_swap_sqrt_price():
    words = [2**255, 12345, 79228162514264337593543950336, 999, 0]  # amount0 (negative-ish), amount1, sqrtP, liq, tick
    data = "0x" + "".join(f"{w:064x}" for w in words)
    assert cs.decode_swap_sqrt_price(data) == 79228162514264337593543950336


def test_weth_usdc_pool_price():
    # Real WETH/USDC pool: token0 = USDC (6 dec), token1 = WETH (18 dec)
    pool = cs.Pool("ETH", "0x", cs.WETH, cs.USDC, coin_is_token0=False, dec0=6, dec1=18)
    sqrtp = sqrt_x96_for(1 / 3200, 6, 18)
    assert abs(cs.pool_price_in_quote(pool, sqrtp) - 3200) < 0.01


def test_wbtc_usdc_pool_price():
    # token0 = WBTC (8 dec), token1 = USDC (6 dec)
    pool = cs.Pool("BTC", "0x", cs.WBTC, cs.USDC, coin_is_token0=True, dec0=8, dec1=6)
    sqrtp = sqrt_x96_for(90000, 8, 6)
    assert abs(cs.pool_price_in_quote(pool, sqrtp) - 90000) < 1


def test_publish_uniswap_weth_quoted_and_fee():
    cs.quotes["LINK"].clear()
    eth = cs.Pool("ETH", "0x1", cs.WETH, cs.USDC, price_in_quote=3000.0, fee=0.0005)
    link = cs.Pool("LINK", "0x2", cs.LINK, cs.WETH, price_in_quote=0.006, fee=0.003)
    cs.publish_uniswap([eth, link])
    q = cs.quotes["LINK"]["Uniswap"]
    assert abs(q.mid - 18.0) < 1e-9
    assert abs(q.bid - 18.0 * 0.997) < 1e-9 and abs(q.ask - 18.0 * 1.003) < 1e-9


def test_best_spread_picks_cheapest_ask_and_richest_bid_across_venues():
    now = time.time()
    qs = {
        "Coinbase": cs.Quote(100.0, 100.1, now),
        "Kraken": cs.Quote(101.0, 101.1, now),
        "Bitstamp": cs.Quote(99.0, 99.2, now - 999),  # stale, ignored
    }
    buy_v, sell_v, buy_p, sell_p, pct, net = cs.best_spread(qs, now)
    assert (buy_v, sell_v, buy_p, sell_p) == ("Coinbase", "Kraken", 100.1, 101.0)
    assert abs(pct - (101.0 - 100.1) / 100.1 * 100) < 1e-12
    assert abs(net - (pct - cs.TAKER_FEE_PCT["Coinbase"] - cs.TAKER_FEE_PCT["Kraken"])) < 1e-12


def test_best_spread_needs_two_fresh_venues():
    now = time.time()
    assert cs.best_spread({"Coinbase": cs.Quote(1, 1.1, now)}, now) is None


def test_coinbase_gets_longer_stale_window():
    now = time.time()
    qs = {"Coinbase": cs.Quote(100.0, 100.1, now - 60), "Kraken": cs.Quote(101.0, 101.1, now - 60)}
    assert cs.is_stale("Kraken", qs["Kraken"], now)
    assert not cs.is_stale("Coinbase", qs["Coinbase"], now)
    assert cs.best_spread(qs, now) is None  # only Coinbase is fresh


def test_uniswap_gas_usd():
    cs.gas.update(base_fee_wei=int(1.9e9), eth_usd=2000.0)  # 1.9 gwei base + 0.1 tip = 2 gwei
    # 2 gwei * 150,000 gas = 0.0003 ETH = $0.60
    assert abs(cs.uniswap_gas_usd() - 0.60) < 1e-9


def test_net_pct_subtracts_fees_and_uniswap_gas():
    cs.gas.update(base_fee_wei=int(1.9e9), eth_usd=2000.0)  # $0.60 per swap
    # Buy on Uniswap (fee already in its price, but pays gas), sell on Binance.US (0.02%)
    net = cs.net_pct("Uniswap", "Binance.US", 0.50, size_usd=1000)
    assert abs(net - (0.50 - 0.02 - 0.06)) < 1e-9


def test_best_spread_ranks_by_net_not_gross():
    now = time.time()
    cs.gas.update(base_fee_wei=0, eth_usd=0.0)
    qs = {
        # Coinbase->Kraken has the bigger gross gap but pays 0.90% + 0.80% in fees
        "Coinbase": cs.Quote(99.9, 100.0, now),
        "Kraken": cs.Quote(101.0, 101.1, now),
        # Binance.US->Bitstamp: smaller gross gap, but only 0.02% + 0.40% in fees
        "Binance.US": cs.Quote(99.9, 100.0, now),
        "Bitstamp": cs.Quote(100.8, 100.9, now),
    }
    b = cs.best_spread(qs, now)
    # Best gross is CB->KRK / BIN->KRK (+1.0%), but BIN->BST nets more: 0.8 - 0.02 - 0.40
    assert (b[0], b[1]) == ("Binance.US", "Bitstamp")
    assert abs(b[5] - 0.38) < 1e-9


def test_uniswap_feed_end_to_end_with_fake_node(monkeypatch):
    """Drive run_uniswap against a fake RPC node: setup calls, a swap, then a new block."""
    import asyncio
    import json as _json

    pools = {p[1]: p for p in cs.UNISWAP_POOLS}
    decimals = {cs.WETH: 18, cs.USDC: 6, cs.WBTC: 8, cs.LINK: 18, cs.UNI: 18}
    word = lambda n: f"{n:064x}"
    addr_word = lambda a: "0x" + "0" * 24 + a[2:]
    # token0/token1 per pool (real ordering: lower address first)
    order = {a: sorted([ct, qt]) for a, (_, _, ct, qt) in pools.items()}
    prices = {  # token0 priced in token1, human units
        "0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640": 1 / 2000,   # USDC in WETH -> ETH $2000
        "0x99ac8ca7087fa4a2a1fb6357269965a2014abc35": 60000,      # WBTC in USDC
        "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8": 0.005,      # LINK in WETH -> $10
        "0x1d42064fc4beb5f8aaf85f4617ae8b3b5b8bd801": 0.0025,     # UNI in WETH -> $5
    }

    def sqrtp(pool):
        t0, t1 = order[pool]
        return sqrt_x96_for(prices[pool], decimals[t0], decimals[t1])

    class FakeWS:
        def __init__(self):
            self.out = asyncio.Queue()
            self.sent_notifications = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, raw):
            m = _json.loads(raw)
            method, params = m["method"], m["params"]
            if method == "eth_call":
                to, data = params[0]["to"], params[0]["data"]
                if data == cs.SEL_TOKEN0:
                    res = addr_word(order[to][0])
                elif data == cs.SEL_TOKEN1:
                    res = addr_word(order[to][1])
                elif data == cs.SEL_DECIMALS:
                    res = "0x" + word(decimals[to])
                elif data == cs.SEL_FEE:
                    res = "0x" + word(500 if to.startswith("0x88e6") else 3000)
                elif data == cs.SEL_SLOT0:
                    res = "0x" + word(sqrtp(to)) + word(0) * 6
                await self.out.put({"jsonrpc": "2.0", "id": m["id"], "result": res})
            elif method == "eth_getBlockByNumber":
                await self.out.put({"id": m["id"], "result": {"baseFeePerGas": hex(int(1.9e9))}})
            elif method == "eth_subscribe":
                sub = "0xlogs" if params[0] == "logs" else "0xheads"
                await self.out.put({"id": m["id"], "result": sub})
                if sub == "0xheads":
                    # a LINK swap moving it to 0.006 WETH, then a new block
                    prices["0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8"] = 0.006
                    link = "0xa6cc3c2531fdaa6ae1a3ca84c2855806728693e8"
                    data = "0x" + word(1) + word(2) + word(sqrtp(link)) + word(3) + word(4)
                    await self.out.put({"method": "eth_subscription", "params": {
                        "subscription": "0xlogs", "result": {"address": link, "data": data, "removed": False}}})
                    # ETH moves to $2500 with NO swap event delivered (like a provider
                    # that drops log events); polling on the next block must catch it.
                    prices["0x88e6a0c2ddd26feeb64f039a2c41296fcb3f5640"] = 1 / 2500
                    await self.out.put({"method": "eth_subscription", "params": {
                        "subscription": "0xheads", "result": {"baseFeePerGas": hex(int(2.9e9))}}})

        async def recv(self):
            return _json.dumps(await self.out.get())

        def __aiter__(self):
            return self

        async def __anext__(self):
            return await self.recv()

    monkeypatch.setattr(cs.websockets, "connect", lambda *a, **k: FakeWS())
    for c in cs.COINS:
        cs.quotes[c].pop("Uniswap", None)

    async def run():
        task = asyncio.create_task(cs.run_uniswap("wss://fake"))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if cs.status["Uniswap"].startswith("live ("):
                break
        task.cancel()

    asyncio.run(run())
    assert cs.status["Uniswap"] == "live (1 swaps)", cs.status["Uniswap"]
    q = cs.quotes
    assert abs(q["ETH"]["Uniswap"].mid - 2500) < 0.01     # picked up by per-block polling
    assert abs(q["BTC"]["Uniswap"].mid - 60000) < 1
    assert abs(q["LINK"]["Uniswap"].mid - 15.0) < 1e-6    # 0.006 WETH (from the swap) * $2500
    assert abs(q["UNI"]["Uniswap"].mid - 6.25) < 1e-6
    assert q["ETH"]["Uniswap"].bid < 2500 < q["ETH"]["Uniswap"].ask  # pool fee applied
    assert cs.gas["base_fee_wei"] == int(2.9e9)                      # updated by newHeads


def test_narrow_terminal_hides_gross_and_nothing_is_cut_off():
    from rich.console import Console
    now = time.time()
    cs.gas.update(base_fee_wei=0, eth_usd=0.0)
    mids = {"BTC": 60422.0, "ETH": 1685.9, "SOL": 70.89, "LINK": 9.252, "UNI": 4.64}
    for coin, m in mids.items():
        cs.quotes[coin] = {v: cs.Quote(m * 0.9999, m * 1.0001, now) for v in cs.VENUES
                           if not (coin == "SOL" and v == "Uniswap")}
    for width in (72, 76):
        console = Console(width=width, record=True)
        console.print(cs.build_view(0.0, cs.VENUES, width))
        out = console.export_text()
        table = out.split("Coinbase: ")[0]
        assert "…" not in table, out
        assert "Gross" not in table and "Net" in table
    console = Console(width=100, record=True)
    console.print(cs.build_view(0.0, cs.VENUES, 100))
    assert "Gross" in console.export_text()
