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
    buy_v, sell_v, buy_p, sell_p, pct = cs.best_spread(qs, now)
    assert (buy_v, sell_v, buy_p, sell_p) == ("Coinbase", "Kraken", 100.1, 101.0)
    assert abs(pct - (101.0 - 100.1) / 100.1 * 100) < 1e-12


def test_best_spread_needs_two_fresh_venues():
    now = time.time()
    assert cs.best_spread({"Coinbase": cs.Quote(1, 1.1, now)}, now) is None
