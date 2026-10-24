"""Complete finalization evidence for isolated model/gate test fixtures."""


def finalized_snapshot(ticker, close_time, result="yes"):
    return {"ticker": ticker, "open_time": close_time - 900, "close_time": close_time,
            "result": result, "status": "finalized", "settlement_time": close_time + 5}


def finalized_history(count=100, *, probability=.8, wins=None, bar=1799999100, prefix="history-"):
    rows = []
    for index in range(count):
        timestamp = bar - (count-index)*900
        ticker = f"{prefix}{index}"
        label = int(index < wins) if wins is not None else int(probability >= .5)
        correct = (probability >= .5) == bool(label)
        rows.append({"bar_timestamp": timestamp, "market_ticker": ticker,
                     "outcome_source": "kalshi_official", "validation_eligible": True,
                     "probability_up": probability, "outcome": "UP" if label else "DOWN",
                     "correct": correct, "side": "UP" if probability >= .5 else "DOWN",
                     "pnl": .5 if correct else -.5, "entry_close": 100., "entry_price": .5,
                     "forecast_issued_at": timestamp + 930, "market_close_time": timestamp + 1800,
                     "settled_at": timestamp + 1810,
                     "market_snapshot": {"ticker": ticker, "observed_at": timestamp + 925,
                                         "yes_bid": .49, "yes_ask": .51},
                     "official_result_snapshot": finalized_snapshot(ticker, timestamp + 1800, "yes" if label else "no")})
    return rows
