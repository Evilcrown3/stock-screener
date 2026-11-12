# Screeners

## Stock screener (Streamlit)

`streamlit run app_online.py` (uses `API_KEY` / `API_SECRET` from Streamlit secrets) or
`streamlit run app_shared.py` (each viewer types their own Alpaca paper keys).

## Crypto arbitrage screener (terminal)

`crypto_screener.py` streams live prices over WebSockets and shows, for each coin, the
best "buy on one venue, sell on another" spread.

| | |
|---|---|
| Coins | BTC, ETH, SOL, LINK, UNI |
| Exchanges | Coinbase, Kraken, Bitstamp, Binance.US (public feeds, no keys needed) |
| On-chain | Uniswap v3 swap events: WETH/USDC, WBTC/USDC, LINK/WETH, UNI/WETH (SOL is CEX-only) |

```bash
pip install -r requirements.txt

# Optional but recommended: your own Ethereum WebSocket URL (free Alchemy or Infura account).
# Without it, a public endpoint is used.
export ETH_WS_URL="wss://eth-mainnet.g.alchemy.com/v2/YOUR_KEY"

python crypto_screener.py              # live
python crypto_screener.py --alert 0.3  # highlight + log spreads >= 0.3%
python crypto_screener.py --no-uniswap # exchanges only
python crypto_screener.py --demo       # fake prices, no network
```

Spreads over the alert level are appended to `arb_alerts.csv` (turn off with `--no-log`).

**Spreads are gross.** Exchange trading/withdrawal fees and Ethereum gas are not
subtracted; the Uniswap pool fee is built into its bid/ask. A spread has to beat all of
those costs to be a real opportunity.

Tests: `python -m pytest tests`
