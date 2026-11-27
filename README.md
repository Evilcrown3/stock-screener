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
python crypto_screener.py --size 5000  # net spread for a $5,000 trade (default $1,000)
python crypto_screener.py --alert 0.1  # only highlight net spreads >= 0.1%
python crypto_screener.py --no-uniswap # exchanges only
python crypto_screener.py --demo       # fake prices, no network
```

The table shows two spreads for the best route per coin:

- **Gross**: sell venue's bid minus buy venue's ask. Uniswap's pool fee is already in its price.
- **Net**: gross minus both exchanges' taker fees, minus live Ethereum gas for any Uniswap leg
  (spread over `--size`). **Green = the trade would make money after costs.**

Taker fees are entry-level tiers as of Nov 2026: Coinbase 0.90%, Kraken 0.80%, Bitstamp 0.40%,
Binance.US 0.02%. **Edit `TAKER_FEE_PCT` at the top of `crypto_screener.py` to match your
account.** Withdrawal/transfer fees are not included (it assumes you already hold funds on both
venues).

Net spreads at or above `--alert` (default 0 = any profit) are appended to `arb_alerts.csv`
(turn off with `--no-log`).

### Phone / web version

`crypto_app.py` is the same screener as a web page with one card per coin, which fits a
phone screen. Run it locally with `streamlit run crypto_app.py`, or host it free on
[Streamlit Community Cloud](https://share.streamlit.io) so it runs without your computer:

1. share.streamlit.io → **Create app** → repo `Evilcrown3/stock-screener`, branch `main`,
   main file `crypto_app.py` → **Deploy**.
2. Optional: app **Settings → Secrets**, add `ETH_WS_URL = "wss://..."` (your Alchemy URL).
3. On your phone, open the app's URL in Chrome → **⋮** → **Add to Home screen**.

Free Streamlit apps go to sleep after a long stretch with no visitors; opening the page
wakes them up (takes about a minute).

Tests: `python -m pytest tests`
