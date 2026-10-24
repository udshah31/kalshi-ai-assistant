"""Proxy check and pre-registered backtest over a late-window store (paper-only, descriptive)."""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import statistics
from collections import defaultdict
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from ask_backtest import MIN_DEPTH, paper_trade, summarize
from late_window import PRICE_OFFSETS, proxy_mid
from validation import _normal_cdf

AVG_OFFSETS = tuple(range(-55, 1, 5))
TIMES = (-60, -30, -15)
MIN_EDGE = 0.05
MIN_WINDOWS, MIN_AGREEMENT, MAX_ERROR_BPS = 500, 0.98, 2.0
MIN_RETURNS, MIN_DAYS, MIN_BACKTEST_WINDOWS = 10, 20, 1000


def window_average(samples_by_offset: dict, close_ts=None) -> float | None:
    mids = [samples_by_offset.get(o) for o in AVG_OFFSETS]
    return None if any(m is None for m in mids) else sum(mids) / len(mids)


def _bps(proxy: float, official: float) -> float:
    return abs(proxy - official) / official * 1e4


def proxy_check(windows: list[dict]) -> dict:
    """Window dict: close_ts, mids {offset: mid|None}, target_price, expiration_value, result|None, snapshots."""
    averages = {w["close_ts"]: window_average(w["mids"], w["close_ts"]) for w in windows}
    official = [w for w in windows if w.get("result") is not None]
    done = [w for w in official if averages[w["close_ts"]] is not None]
    strike_err = [_bps(averages[w["close_ts"] - 900], w["target_price"]) for w in done
                  if averages.get(w["close_ts"] - 900) is not None]
    settle_err = [_bps(averages[w["close_ts"]], w["expiration_value"]) for w in done]
    agree = [(averages[w["close_ts"]] >= w["target_price"]) == (w["result"] == "yes") for w in done]
    out = {"windows": len(done), "excluded_incomplete": len(official) - len(done),
           "strike_error_bps": statistics.median(strike_err) if strike_err else None,
           "settlement_error_bps": statistics.median(settle_err) if settle_err else None,
           "call_agreement": sum(agree) / len(agree) if agree else None}
    out["passed"] = bool(done and len(done) >= MIN_WINDOWS and out["call_agreement"] >= MIN_AGREEMENT
                         and out["settlement_error_bps"] <= MAX_ERROR_BPS)
    return out


def late_probability(observed: list[float], spot: float, remaining_offsets: list[int], t_offset: int,
                     sigma_per_s: float, strike: float) -> float:
    m = len(remaining_offsets)
    n = len(observed) + m
    mean = (sum(observed) + m * spot) / n
    taus = [o - t_offset for o in remaining_offsets]
    var = (spot * sigma_per_s) ** 2 / n ** 2 * sum(min(a, b) for a in taus for b in taus)
    if var == 0:
        return 1.0 if mean >= strike else 0.0
    return _normal_cdf((mean - strike) / math.sqrt(var))


def _inputs(w: dict, t: int):
    mids = w["mids"]
    observed = [mids.get(o) for o in AVG_OFFSETS if o <= t]
    spot = mids.get(t)
    if spot is None or any(m is None for m in observed):
        return None
    path = [mids.get(o) for o in PRICE_OFFSETS if o <= t]
    rets = [math.log(b / a) for a, b in zip(path, path[1:]) if a and b]
    if len(rets) < MIN_RETURNS:
        return None
    sigma = statistics.pstdev(rets) / math.sqrt(5)
    return observed, spot, [o for o in AVG_OFFSETS if o > t], sigma


def _day(close_ts: int) -> str:
    return datetime.fromtimestamp(close_ts, timezone.utc).date().isoformat()


def _evaluate(windows: list[dict], t: int) -> dict:
    trades, eligible, days = [], 0, set()
    for w in windows:
        snap = (w.get("snapshots") or {}).get(t)
        if w.get("result") is None or window_average(w["mids"]) is None or not snap or snap.get("error"):
            continue
        yes, no = snap.get("yes_ask"), snap.get("no_ask")
        if yes is None or no is None or not (0 < yes < 1 and 0 < no < 1):
            continue
        inputs = _inputs(w, t)
        if inputs is None:
            continue
        eligible += 1
        days.add(_day(w["close_ts"]))
        observed, spot, remaining, sigma = inputs
        p = late_probability(observed, spot, remaining, t, sigma, w["target_price"])
        trade = paper_trade(p, yes, no, int(w["result"] == "yes"))
        if trade["edge"] >= MIN_EDGE and (snap.get(f"{trade['side']}_ask_size") or 0) >= MIN_DEPTH:
            trade["day"] = _day(w["close_ts"])
            trades.append(trade)
    return {"eligible_windows": eligible, "eligible_days": len(days), **summarize(trades)}


def backtest(windows: list[dict]) -> dict:
    check = proxy_check(windows)
    if not check["passed"]:
        return {"status": "proxy_check_failed", "proxy_check": check}
    by_time = {str(t): _evaluate(windows, t) for t in TIMES}
    p = by_time["-30"]
    interval = p["pnl_per_trade_interval_95"]
    success = bool(p["pnl_per_trade"] is not None and p["pnl_per_trade"] > 0 and interval and interval[0] > 0
                   and p["eligible_days"] >= MIN_DAYS and p["eligible_windows"] >= MIN_BACKTEST_WINDOWS)
    return {"status": "evaluated", "by_time": by_time, "primary": "-30", "success": success}


def load_windows(path: Path) -> list[dict]:
    uri = f"file:{Path(path).resolve()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        windows = {r["close_ts"]: {"close_ts": r["close_ts"], "mids": {}, "snapshots": {}, "result": None}
                   for r in db.execute("SELECT close_ts FROM windows")}
        rows = defaultdict(list)
        for r in db.execute("SELECT * FROM price_samples"):
            rows[(r["close_ts"], r["offset_s"])].append(dict(r))
        for (close, offset), group in rows.items():
            if close in windows:
                windows[close]["mids"][offset] = proxy_mid(group)
        for r in db.execute("SELECT * FROM quote_snapshots"):
            if r["close_ts"] in windows:
                windows[r["close_ts"]]["snapshots"][r["offset_s"]] = dict(r)
        for r in db.execute("SELECT * FROM official_values"):
            if r["close_ts"] in windows:
                windows[r["close_ts"]].update(target_price=r["target_price"], result=r["result"],
                                              expiration_value=r["expiration_value"])
    return sorted(windows.values(), key=lambda w: w["close_ts"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("store", type=Path)
    windows = load_windows(parser.parse_args().store)
    print(json.dumps({"proxy_check": proxy_check(windows), "backtest": backtest(windows)},
                     indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
