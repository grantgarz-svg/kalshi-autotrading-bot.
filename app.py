def manage_position(client, mode, position, avg_entry, take_profit_pct, stop_loss_pct, enable_panic, panic_mins):
    if position["contracts"] <= 0:
        return False

    ticker = position["ticker"]
    outcome = position["outcome"]
    contracts = int(position["contracts"])
    if outcome not in ("YES", "NO"):
        return False

    try:
        market_data = client.get_market(ticker)
        market = market_data.get("market", market_data)
        mins_left = market_minutes_remaining(market)

        prices = get_both_prices(client, ticker)
        if not prices: return False
        current_bid = prices[outcome]["bid"]
        if current_bid <= 0:
            return False
    except Exception:
        return False

    current = D(current_bid) / D(100)

    if avg_entry is None or avg_entry <= 0:
        avg_entry = current

    # Calculate exact ROI percentage just like the Kalshi app (+X.XX%)
    roi_pct = ((current - avg_entry) / avg_entry) * D(100)

    reason = None
    if take_profit_pct > 0 and roi_pct >= D(take_profit_pct):
        reason = f"TAKE PROFIT ({roi_pct:.2f}%)"
    elif stop_loss_pct > 0 and roi_pct <= -D(stop_loss_pct):
        reason = f"STOP LOSS ({roi_pct:.2f}%)"
    elif enable_panic and mins_left is not None and mins_left <= panic_mins and current < avg_entry:
        reason = f"TIME PANIC EXIT ({roi_pct:.2f}%)"

    if reason is None:
        return False

    log(f"🚨 {mode} {reason}: {ticker} {outcome} entry=${avg_entry:.4f}, bid=${current:.4f}")
    submit_trade(
        client=client, mode=mode, ticker=ticker, outcome=outcome,
        action="SELL", contracts=contracts, outcome_cents=current_bid, reduce_only=True,
    )

    if reason.startswith(("STOP LOSS", "TIME PANIC EXIT", "TAKE PROFIT")):
        st.session_state.blacklisted_tickers.add(f"{ticker}_{outcome}")
        st.session_state["last_status_log"] = None 

    return True
