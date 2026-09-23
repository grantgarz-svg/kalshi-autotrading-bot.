import streamlit as st
import time
import base64
import uuid
import datetime
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

st.set_page_config(page_title="Kalshi Scalper", page_icon="⚡", layout="centered")

# --- UI DASHBOARD (Replicating PolyPulse) ---
st.title("⚡ Kalshi Scalper")
st.markdown("Automated momentum trading for Kalshi short-term markets.")

with st.sidebar:
    st.header("🔑 Account Setup")
    st.info("Your keys are never saved. They reset when you close the page.")
    key_id = st.text_input("Kalshi Key ID", type="password")
    private_key_text = st.text_area("Private Key (Paste full contents here)"
    ticker = st.text_input("Market Ticker", value="KXBTC-26SEP22-T65000")

st.subheader("Configuration")

# Segmented controls create the clickable button rows
bet_size = st.segmented_control("Bet size per trade", ["$1", "$5", "$10", "$25", "$50", "$100"], default="$10")
daily_cap = st.segmented_control("Daily cap", ["$25", "$50", "$100", "$250", "$500"], default="$100")
take_profit = st.segmented_control("Take profit", ["+10%", "+20%", "+30%", "+40%", "+50%", "+75%"], default="+20%")
stop_loss = st.segmented_control("Stop loss", ["-10%", "-20%", "-25%", "-30%", "-40%", "-50%"], default="-25%")
max_entry = st.segmented_control("Max entry price (mode default: 75¢)", ["50¢", "55¢", "60¢", "65¢", "70¢", "75¢"], default="75¢")

col1, col2 = st.columns(2)
with col1:
    start_time = st.time_input("Start Trading Hours", datetime.time(0, 0))
with col2:
    end_time = st.time_input("End Trading Hours", datetime.time(23, 0))

# --- BOT LOGIC ---
if st.button("▶ START AUTOTRADING", use_container_width=True, type="primary"):
    if not key_id or not private_key_text:
        st.error("Please enter your Kalshi credentials in the sidebar first.")
        st.stop()

    # Convert UI text selections into usable numbers
    bet_dollars = int(bet_size.replace("$", ""))
    cap_dollars = int(daily_cap.replace("$", ""))
    tp_pct = int(take_profit.replace("+", "").replace("%", "")) / 100.0
    sl_pct = int(stop_loss.replace("-", "").replace("%", "")) / 100.0
    max_price = int(max_entry.replace("¢", ""))
    
    st.success(f"Bot activated! Monitoring {ticker}. Leave this tab open.")
    
    # Create a live log box on the website
    log_window = st.empty()
    logs = []
    
    def log_msg(msg):
        logs.append(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}")
        # Keep only the last 10 messages so the screen doesn't clutter
        log_window.code("\n".join(logs[-10:]))

    # --- THE TRADING LOOP ---
    total_spent = 0.0
    
    # While loop runs constantly as long as the webpage is open
    while True:
        now = datetime.datetime.now().time()
        
        if not (start_time <= now <= end_time):
            log_msg("Outside trading hours. Waiting...")
            time.sleep(10)
            continue
            
        if total_spent >= cap_dollars:
            log_msg("Daily cap reached. Bot stopped.")
            st.stop()
            
        # Example API check (Replace with real Kalshi auth/orderbook logic)
        try:
            res = requests.get(f"https://external-api.demo.kalshi.co/trade-api/v2/markets/{ticker}/orderbook")
            if res.status_code == 200:
                book = res.json().get('orderbook', {})
                asks = book.get('yes', [])
                
                if asks:
                    best_ask = asks[0][0]
                    log_msg(f"Market Check: Best Ask is {best_ask}¢")
                    
                    if best_ask <= max_price:
                        log_msg(f"🔥 Signal Valid! Buying at {best_ask}¢...")
                        total_spent += bet_dollars
                        log_msg("Trade executed. Pausing for 60s to prevent duplicate buys.")
                        time.sleep(60)
            else:
                log_msg(f"Waiting for market data...")
        except Exception as e:
            log_msg("Network error, retrying...")
            
        time.sleep(5)
