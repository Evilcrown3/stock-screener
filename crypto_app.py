"""
Crypto arbitrage screener: web / phone version (Streamlit).

Same live feeds and fee math as crypto_screener.py, shown as a web page you
can open on your phone and add to your home screen. Host it free on
Streamlit Community Cloud so your computer doesn't need to be on.

Run locally:      streamlit run crypto_app.py
Optional secrets: ETH_WS_URL   your Alchemy/Infura WebSocket URL (recommended)
                  CRYPTO_DEMO  "1" to show fake prices (no network)
"""

import asyncio
import os
import threading
import time
from datetime import datetime

import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import crypto_screener as cs

st.set_page_config(page_title="Crypto Arb", page_icon="📈", layout="wide")


def secret(name, default=None):
    """Read a Streamlit secret, then an environment variable."""
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:  # no secrets file at all
        pass
    return os.environ.get(name, default)


DEMO = str(secret("CRYPTO_DEMO", "")).lower() in ("1", "true", "yes")
ETH_WS_URL = secret("ETH_WS_URL") or cs.DEFAULT_ETH_WS_URL


@st.cache_resource
def start_feeds(eth_ws_url, demo):
    """
    Start the price feeds once per server, in a background thread.
    Every viewer of the page reads the same live prices.
    """
    async def run_all():
        if demo:
            await cs.run_demo(cs.VENUES)
            return
        await asyncio.gather(
            cs.run_exchange("Coinbase", cs.coinbase_feed()),
            cs.run_exchange("Kraken", cs.kraken_feed()),
            cs.run_exchange("Bitstamp", cs.bitstamp_feed()),
            cs.run_exchange("Binance.US", cs.binance_us_feed()),
            cs.run_uniswap(eth_ws_url),
        )

    thread = threading.Thread(target=lambda: asyncio.run(run_all()), daemon=True, name="crypto-feeds")
    thread.start()
    return thread


start_feeds(ETH_WS_URL, DEMO)

# ---- Controls -------------------------------------------------------------
st.markdown("### Crypto Arb Screener" + (" (demo)" if DEMO else ""))
with st.expander("Settings"):
    size = st.number_input("Trade size ($)", min_value=100, value=cs.DEFAULT_SIZE_USD, step=500)
    refresh = st.selectbox("Refresh every", [2, 5, 10, 30], index=1, format_func=lambda s: f"{s} seconds")
st_autorefresh(interval=refresh * 1000, key="tick")

now = time.time()
snap = {coin: dict(cs.quotes[coin]) for coin in cs.COINS}  # consistent copy for this redraw

if not any(snap.values()):
    st.info("Connecting to the exchanges… prices appear in a few seconds.")

# ---- Best trade per coin (phone-friendly cards) ---------------------------
st.caption(f"Updated {datetime.now().strftime('%I:%M:%S %p')} · best route per coin for a "
           f"\\${size:,.0f} trade, after fees and gas")
for coin in cs.COINS:
    best = cs.best_spread(snap[coin], now, size)
    with st.container(border=True):
        if not best:
            st.markdown(f"**{coin}** · waiting for prices")
            continue
        buy_v, sell_v, buy_p, sell_p, gross, net = best
        color = "green" if net >= 0 else "red"
        verdict = "PROFIT" if net >= 0 else "no profit"
        # "\$" stops Streamlit treating dollar signs as math formulas
        st.markdown(
            f"**{coin}** &nbsp; :{color}[**{net:+.2f}%** {verdict}]  \n"
            f"Buy {cs.SHORT[buy_v]} \\${cs.fmt_price(buy_p)} → Sell {cs.SHORT[sell_v]} \\${cs.fmt_price(sell_p)}"
            f" · gross {gross:+.2f}%"
        )

# ---- All prices -----------------------------------------------------------
with st.expander("All prices"):
    rows = []
    for coin in cs.COINS:
        row = {"Coin": coin}
        for v in cs.VENUES:
            q = snap[coin].get(v)
            row[cs.SHORT[v]] = None if (not q or cs.is_stale(v, q, now)) else q.mid
        rows.append(row)
    df = pd.DataFrame(rows)
    st.dataframe(df.style.format(lambda x: "—" if pd.isna(x) else cs.fmt_price(x),
                                 subset=[cs.SHORT[v] for v in cs.VENUES]),
                 hide_index=True)
    st.caption("— = no price yet, no Uniswap pool (SOL), or price is stale.")

# ---- Status ---------------------------------------------------------------
gas_usd = cs.uniswap_gas_usd()
gas_text = f"\\${gas_usd:.2f} per swap" if gas_usd else "waiting"
st.caption(" · ".join(f"{cs.SHORT[v]}: {cs.status[v]}" for v in cs.VENUES))
st.caption(
    f"Uniswap gas {gas_text} · Taker fees: "
    + ", ".join(f"{cs.SHORT[v]} {cs.TAKER_FEE_PCT[v]:g}%" for v in cs.VENUES if v != "Uniswap")
    + " · Net = gross − fees − gas. Withdrawal fees not included."
)
