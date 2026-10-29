import requests
from datetime import datetime
import streamlit as st
from streamlit_autorefresh import st_autorefresh
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockSnapshotRequest

# ---- PASTE YOUR KEYS HERE (keep them private) ----
API_KEY = st.secrets["API_KEY"]
API_SECRET = st.secrets["API_SECRET"]
FEED = "iex"  # free real-time feed

st.set_page_config(page_title="Dollar Movers Screener", layout="wide")
st.title("JK3 Movers Screener")

# ---- SETTINGS YOU CAN SWITCH ON THE PAGE ----
col1, col2, col3, col4 = st.columns(4)
move = col1.selectbox("Minimum dollar move", [1, 2, 3, 5, 10, 20], index=0)
min_price = col2.number_input("Minimum price ($)", value=5.0, step=1.0)
max_price = col3.number_input("Maximum price ($)", value=500.0, step=10.0)
min_volume = col4.number_input("Minimum volume", value=1_000_000, step=100_000)

col5, col6, col7, col8 = st.columns(4)
min_rvol = col5.number_input("Min relative volume (0 = off)", value=0.0, step=0.5)
min_pct = col6.number_input("Min percent move (0 = off)", value=0.0, step=0.5)
auto = col7.checkbox("Auto-refresh")
interval = col8.selectbox("Refresh every (seconds)", [30, 60, 120, 300], index=1)

# Rerun the page on a timer while auto-refresh is ticked
if auto:
    st_autorefresh(interval=interval * 1000, key="refresher")


@st.cache_data(ttl=3600)
def get_symbols():
    resp = requests.get(
        "https://paper-api.alpaca.markets/v2/assets",
        headers={"APCA-API-KEY-ID": API_KEY, "APCA-API-SECRET-KEY": API_SECRET},
        params={"status": "active", "asset_class": "us_equity"},
    )
    resp.raise_for_status()
    return [
        a["symbol"] for a in resp.json()
        if a.get("tradable") and a.get("exchange") != "OTC"
    ]


def run_scan():
    data = StockHistoricalDataClient(API_KEY, API_SECRET)
    symbols = get_symbols()
    progress = st.progress(0, text=f"Scanning {len(symbols)} stocks...")
    gainers, losers = [], []

    BATCH = 500
    for i in range(0, len(symbols), BATCH):
        chunk = symbols[i:i + BATCH]
        try:
            snaps = data.get_stock_snapshot(
                StockSnapshotRequest(symbol_or_symbols=chunk, feed=FEED)
            )
        except Exception:
            continue

        for sym, s in snaps.items():
            if not s.daily_bar or not s.previous_daily_bar:
                continue
            price = s.daily_bar.close
            volume = s.daily_bar.volume
            high = s.daily_bar.high
            prev_close = s.previous_daily_bar.close
            prev_volume = s.previous_daily_bar.volume
            if not prev_close:
                continue

            change = price - prev_close
            pct = change / prev_close * 100
            rvol = volume / prev_volume if prev_volume else 0
            off_high = (high - price) / high * 100 if high else 0

            if price < min_price or price > max_price or volume < min_volume:
                continue
            if min_rvol and rvol < min_rvol:
                continue
            if min_pct and abs(pct) < min_pct:
                continue

            row = {
                "Symbol": sym,
                "Price": round(price, 2),
                "Dollar change": round(change, 2),
                "Percent change": round(pct, 2),
                "Volume": int(volume),
                "Relative volume": round(rvol, 2),
                "Below high (%)": round(off_high, 2),
            }
            if change >= move:
                gainers.append(row)
            elif change <= -move:
                losers.append(row)

        progress.progress(min((i + BATCH) / len(symbols), 1.0))

    progress.empty()
    gainers.sort(key=lambda r: r["Dollar change"], reverse=True)
    losers.sort(key=lambda r: r["Dollar change"])

    st.caption(f"Last scan: {datetime.now().strftime('%I:%M:%S %p')}")
    left, right = st.columns(2)
    left.subheader(f"Dollar gainers ({len(gainers)})")
    left.dataframe(gainers, use_container_width=True)
    right.subheader(f"Dollar losers ({len(losers)})")
    right.dataframe(losers, use_container_width=True)


if st.button("Scan Now") or auto:
    run_scan()