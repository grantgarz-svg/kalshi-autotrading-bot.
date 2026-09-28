import threading
import asyncio
import websockets

PROD_WS_URL = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
DEMO_WS_URL = "wss://external-api-ws.demo.kalshi.co/trade-api/ws/v2"


def create_ws_headers(key_id, private_key, demo=False):
    timestamp = str(int(time.time() * 1000))
    path = "/trade-api/ws/v2"
    signature = create_signature(private_key, timestamp, "GET", path)
    return {
        "KALSHI-ACCESS-KEY": key_id.strip(),
        "KALSHI-ACCESS-SIGNATURE": signature,
        "KALSHI-ACCESS-TIMESTAMP": timestamp,
    }


async def ws_listener(key_id, private_key_obj, demo):
    ws_url = DEMO_WS_URL if demo else PROD_WS_URL
    
    while st.session_state.get("running", False):
        try:
            timestamp = str(int(time.time() * 1000))
            signature = create_signature(private_key_obj, timestamp, "GET", "/trade-api/ws/v2")
            headers = {
                "KALSHI-ACCESS-KEY": key_id.strip(),
                "KALSHI-ACCESS-SIGNATURE": signature,
                "KALSHI-ACCESS-TIMESTAMP": timestamp,
            }

            async with websockets.connect(ws_url, extra_headers=headers) as websocket:
                # Subscribe to live ticker and orderbook delta feeds
                sub_msg = {
                    "id": 1,
                    "cmd": "subscribe",
                    "params": {
                        "channels": ["ticker", "orderbook_delta"]
                    }
                }
                await websocket.send(json.dumps(sub_msg))
                log("⚡ WebSocket connected: Streaming live ticks!")

                async for message in websocket:
                    if not st.session_state.get("running", False):
                        break
                    
                    data = json.loads(message)
                    if data.get("type") == "ticker":
                        msg_body = data.get("msg", {})
                        t = msg_body.get("market_ticker")
                        yes_ask = int(float(msg_body.get("yes_ask_dollars", 0)) * 100) if msg_body.get("yes_ask_dollars") else 0
                        no_ask = int(float(msg_body.get("no_ask_dollars", 0)) * 100) if msg_body.get("no_ask_dollars") else 0
                        
                        if t and yes_ask > 0:
                            st.session_state.active_ticker = t
                            st.session_state.up_ask_display = f"{yes_ask}¢"
                            st.session_state.down_ask_display = f"{no_ask}¢" if no_ask > 0 else "--"
                            st.session_state.price_history.append(yes_ask)
                            st.session_state.price_history = st.session_state.price_history[-100:]

        except Exception as e:
            log(f"⚠️ WebSocket dropped: {e}. Reconnecting in 3s...")
            await asyncio.sleep(3)


def start_websocket_thread(key_id, private_key_obj, demo):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(ws_listener(key_id, private_key_obj, demo))

    t = threading.Thread(target=run, daemon=True)
    t.start()
