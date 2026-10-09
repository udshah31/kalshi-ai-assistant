"""Local web dashboard for the BTC 15-minute predictor."""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from forecast_archive import (
    read_context_comparison,
    read_historical_analytics,
    read_recent_forecasts,
    read_validation_summary,
)

from btc_predictor import (
    DEFAULT_LOOKBACK_MINUTES,
    aggregate_candles,
    archive_and_guard,
    bootstrap_from_bars,
    demo_account_summary,
    fetch_candles,
    fetch_kalshi_market,
    fetch_previous_kalshi_market,
    fetch_spot_price,
    finalize_prediction,
    learn_from_pending,
    load_state,
    paper_trade_summary,
    predict_and_queue,
    save_state,
    state_transaction,
)


HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>BTC 15m Predictor</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #08111f;
      --panel: rgba(18, 31, 53, .86);
      --panel-strong: #142542;
      --line: #274064;
      --text: #ecf4ff;
      --muted: #91a4c0;
      --green: #45e2a0;
      --red: #ff6c86;
      --blue: #6ea8ff;
      --yellow: #ffd166;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      min-height: 100vh;
      color: var(--text);
      background:
        radial-gradient(circle at 10% 0%, #14325e 0, transparent 38%),
        radial-gradient(circle at 95% 10%, #163d3a 0, transparent 32%),
        var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .shell { width: min(1100px, calc(100% - 32px)); margin: 0 auto; padding: 42px 0 54px; }
    header { display: flex; justify-content: space-between; align-items: flex-end; gap: 24px; margin-bottom: 28px; }
    .eyebrow { color: var(--blue); font-size: 12px; font-weight: 800; letter-spacing: .18em; text-transform: uppercase; }
    h1 { margin: 8px 0 8px; font-size: clamp(32px, 6vw, 56px); letter-spacing: -.055em; line-height: 1; }
    .subtitle { color: var(--muted); margin: 0; max-width: 650px; line-height: 1.55; }
    button { border: 1px solid #4778bc; color: var(--text); background: #183760; border-radius: 10px; padding: 11px 16px; cursor: pointer; font-weight: 700; }
    button:hover { background: #214778; }
    .status { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 13px; margin-top: 13px; }
    .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--yellow); box-shadow: 0 0 12px currentColor; }
    .dot.ok { background: var(--green); }
    .dot.error { background: var(--red); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 16px; }
    .card { background: var(--panel); border: 1px solid var(--line); border-radius: 18px; padding: 22px; box-shadow: 0 18px 50px rgba(0,0,0,.16); backdrop-filter: blur(12px); }
    .hero { grid-column: span 7; min-height: 310px; display: flex; flex-direction: column; justify-content: space-between; }
    .market { grid-column: span 5; min-height: 310px; }
    .metric { grid-column: span 3; }
    .wide { grid-column: span 6; }
    .label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .12em; font-weight: 800; }
    .direction { font-size: clamp(60px, 9vw, 104px); letter-spacing: -.08em; font-weight: 900; line-height: .95; margin: 20px 0 8px; }
    .direction.up { color: var(--green); }
    .direction.down { color: var(--red); }
    .confidence { color: var(--muted); font-size: 17px; }
    .confidence strong { color: var(--text); font-size: 26px; }
    .signal { display: inline-block; margin-top: 22px; padding: 10px 14px; border-radius: 10px; font-size: 14px; font-weight: 900; letter-spacing: .08em; }
    .signal.trade { color: var(--green); background: rgba(69,226,160,.14); border: 1px solid rgba(69,226,160,.5); }
    .signal.wait { color: var(--yellow); background: rgba(255,209,102,.12); border: 1px solid rgba(255,209,102,.45); }
    .signal-reason { margin: 8px 0 0; max-width: 600px; }
    .bar { height: 12px; background: #1a2b47; border-radius: 999px; overflow: hidden; margin: 18px 0 10px; }
    .bar > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--blue), var(--green)); transition: width .35s ease; }
    .bar.down > span { background: linear-gradient(90deg, var(--red), #ffb36a); }
    .split { display: flex; justify-content: space-between; color: var(--muted); font-size: 13px; }
    .split strong { color: var(--text); }
    .market-title { font-size: 20px; font-weight: 800; margin: 10px 0 4px; }
    .target { color: var(--yellow); font-size: 15px; margin: 0 0 24px; }
    .settled { grid-column: span 5; }
    .settled-result { font-size: 34px; font-weight: 900; margin: 12px 0 10px; }
    .settled-result.yes { color: var(--green); }
    .settled-result.no { color: var(--red); }
    .odds { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 20px; }
    .odd { background: var(--panel-strong); padding: 15px; border-radius: 12px; }
    .odd b { display: block; font-size: 27px; margin-top: 5px; }
    .odd.yes b { color: var(--green); }
    .odd.no b { color: var(--red); }
    .value { font-size: 29px; font-weight: 800; margin-top: 10px; letter-spacing: -.04em; }
    .small { color: var(--muted); font-size: 13px; line-height: 1.5; }
    .pill { display: inline-block; padding: 5px 9px; border-radius: 999px; font-size: 12px; font-weight: 800; background: #203453; color: var(--muted); }
    .pill.positive { color: var(--green); background: rgba(69,226,160,.11); }
    .pill.negative { color: var(--red); background: rgba(255,108,134,.11); }
    .value.positive { color: var(--green); }
    .value.negative { color: var(--red); }
    .notice { border-left: 3px solid var(--yellow); padding: 2px 0 2px 14px; color: var(--muted); font-size: 13px; line-height: 1.6; }
    .error { color: var(--red); white-space: pre-wrap; }
    .evidence { grid-column: span 12; }
    .check-row { display: grid; grid-template-columns: minmax(170px, 1fr) 90px; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--line); }
    .check-pass { color: var(--green); } .check-block { color: var(--yellow); }
    #blockers { padding-left: 20px; } #blockers li { margin: 6px 0; }
    .context-panel { grid-column: span 12; border-top: 3px solid var(--blue); }
    .context-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
    .context-heading h2 { margin: 0 0 6px; font-size: 24px; letter-spacing: -.035em; }
    .context-heading p { margin: 0; max-width: 70ch; }
    .context-inputs { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 18px 0; }
    .context-input { padding: 12px 16px; border: 1px solid var(--line); border-radius: 10px; }
    .context-input strong { display: block; font-size: 21px; margin-top: 4px; font-variant-numeric: tabular-nums; }
    .context-input.challenger { border-color: var(--blue); background: rgba(110,168,255,.07); }
    .context-input.challenger strong { color: var(--blue); }
    .context-controls { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
    .context-tabs { display: inline-flex; gap: 4px; }
    .context-tabs button { padding: 9px 12px; background: transparent; border-color: var(--line); color: var(--muted); }
    .context-tabs button[aria-pressed="true"] { background: var(--panel-strong); border-color: var(--blue); color: var(--text); }
    button:focus-visible { outline: 2px solid var(--yellow); outline-offset: 3px; }
    .context-table-wrap { overflow-x: auto; margin-top: 12px; }
    .context-scroll-hint { display: none; }
    .context-table { border-collapse: collapse; width: 100%; min-width: 540px; font-size: 14px; }
    .context-table th, .context-table td { padding: 13px 10px; border-bottom: 1px solid var(--line); text-align: right; font-variant-numeric: tabular-nums; }
    .context-table thead th { color: var(--muted); font-weight: 600; font-size: 12px; }
    .context-table th:first-child { text-align: left; padding-left: 0; }
    .context-table tbody th { font-weight: 600; }
    .context-table .challenger-row { background: rgba(110,168,255,.06); }
    .context-table .challenger-row th { color: var(--blue); }
    .context-progress { display: block; width: 100%; height: 8px; margin: 10px 0 7px; accent-color: var(--blue); }
    .context-footer { display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; margin-top: 14px; }
    #context-conclusion { margin: 16px 0 6px; }
    .analytics-panel { grid-column: span 12; min-width: 0; }
    .analytics-heading { display: flex; justify-content: space-between; align-items: flex-start; gap: 16px; }
    .analytics-panel h2 { margin: 0 0 6px; font-size: 24px; letter-spacing: -.035em; }
    .analytics-panel h3 { margin: 0 0 8px; font-size: 17px; }
    .analytics-panel p { max-width: 78ch; }
    .analytics-heading p { margin: 0; }
    .analytics-warning { padding: 12px 16px; border: 1px solid var(--yellow); border-left-width: 4px; border-radius: 8px; color: var(--yellow); background: rgba(255,209,102,.08); line-height: 1.5; }
    .analytics-status-stale { color: var(--yellow); }
    .analytics-summary { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; margin: 18px 0; }
    .analytics-stat { background: var(--panel-strong); border-radius: 12px; padding: 16px; }
    .analytics-stat dt { color: var(--muted); font-size: 13px; }
    .analytics-stat dd { margin: 8px 0 0; font-size: 27px; font-weight: 750; letter-spacing: -.035em; font-variant-numeric: tabular-nums; }
    .analytics-stat .small { display: block; margin-top: 8px; font-weight: 400; letter-spacing: normal; }
    .analytics-columns { display: grid; grid-template-columns: minmax(0, 2fr) minmax(0, 1fr); gap: 22px; margin: 24px 0; }
    .analytics-columns > * { min-width: 0; }
    .analytics-coverage { border-left: 1px solid var(--line); padding-left: 22px; }
    .analytics-coverage dl { margin: 14px 0; }
    .analytics-coverage dl > div { display: flex; justify-content: space-between; gap: 16px; border-bottom: 1px solid var(--line); padding: 9px 0; }
    .analytics-coverage dt { color: var(--muted); font-size: 13px; }
    .analytics-coverage dd { margin: 0; font-size: 14px; font-variant-numeric: tabular-nums; }
    .analytics-plot { margin: 16px 0 8px; }
    .analytics-plot h4 { margin: 0 0 4px; font-size: 13px; font-weight: 600; }
    .analytics-plot svg { display: block; width: 100%; height: auto; }
    .analytics-plot text { fill: var(--muted); font-size: 12px; }
    .analytics-plot .plot-grid { stroke: var(--line); stroke-width: 1; }
    .analytics-plot .plot-series { stroke: var(--blue); fill: none; stroke-width: 2; }
    .analytics-plot circle { fill: var(--blue); }
    .analytics-plot.brier .plot-series { stroke: var(--yellow); }
    .analytics-plot.brier circle { fill: var(--yellow); }
    .analytics-panel details > summary { padding: 12px 0; color: var(--blue); cursor: pointer; font-size: 13px; }
    .analytics-scroll { max-width: 100%; overflow: auto; max-height: 420px; border: 1px solid var(--line); border-radius: 10px; }
    .analytics-scroll:focus-visible, .analytics-panel summary:focus-visible { outline: 2px solid var(--yellow); outline-offset: 3px; }
    .analytics-table { border-collapse: collapse; width: 100%; min-width: 980px; font-size: 13px; }
    .analytics-table.trend-values { min-width: 500px; }
    .analytics-table caption { text-align: left; padding: 12px; color: var(--muted); }
    .analytics-table th, .analytics-table td { padding: 12px; text-align: left; border-bottom: 1px solid var(--line); font-variant-numeric: tabular-nums; }
    .analytics-table thead th { position: sticky; top: 0; background: var(--panel-strong); color: var(--muted); font-weight: 600; }
    .analytics-table tbody th { font-weight: 500; overflow-wrap: anywhere; min-width: 140px; max-width: 220px; }
    .analytics-table td { min-width: 85px; }
    .analytics-table .analytics-time { min-width: 150px; }
    #analytics-message { overflow-wrap: anywhere; }
    #analytics-updated, #analytics-trend-time, #analytics-forecasts-updated { font-variant-numeric: tabular-nums; }
    @media (prefers-reduced-motion: reduce) { .bar > span { transition: none; } }
    [hidden] { display: none !important; }
    @media (max-width: 760px) {
      .shell { width: min(100% - 22px, 600px); padding-top: 26px; }
      header { display: block; }
      header button { margin-top: 18px; }
      .hero, .market, .settled, .metric, .wide { grid-column: span 12; }
      .hero, .market { min-height: 0; }
      .context-heading { flex-direction: column; gap: 10px; }
      .context-inputs { grid-template-columns: 1fr; }
      .context-scroll-hint { display: block; margin: 8px 0 0; }
      .analytics-heading { flex-direction: column; }
      .analytics-summary, .analytics-columns { grid-template-columns: minmax(0, 1fr); }
      .analytics-coverage { border-left: 0; border-top: 1px solid var(--line); padding: 18px 0 0; }
      .analytics-plot text { font-size: 22px; }
    }
  </style>
</head>
<body>
  <main class="shell">
    <header>
      <div>
        <div class="eyebrow">Local prediction terminal</div>
        <h1>BTC / 15m</h1>
        <p class="subtitle">Experimental, paper-only recommendations for the exact active Kalshi contract. Raw model estimates are not calibrated winning probabilities.</p>
        <div class="status"><span id="status-dot" class="dot"></span><span id="status-text">Loading live data…</span><span>•</span><span id="clock">—</span></div>
      </div>
      <button id="refresh">Refresh prediction</button>
    </header>

    <section class="grid">
      <article class="card hero">
        <div>
          <div class="label">Model direction</div>
          <div id="direction" class="direction">—</div>
          <div class="confidence">Raw model estimate <strong id="probability">—</strong></div>
          <div class="small">Uncalibrated • a direction forecast is not a trade recommendation</div>
          <div class="small">Current context inputs: 15m / 45m / 3h</div>
          <div id="forecast-status" class="small">Waiting for forecast evidence…</div>
          <div id="probability-bar" class="bar"><span style="width: 0%"></span></div>
          <div id="trade-signal" class="signal wait">WAIT</div>
          <p id="trade-reason" class="small signal-reason">Evaluating model confidence and market edge…</p>
          <div class="split"><span>Up <strong id="up-probability">—</strong></span><span>Down <strong id="down-probability">—</strong></span></div>
        </div>
        <div class="small">Latest complete window: <span id="window-time">—</span></div>
      </article>

      <article class="card market">
        <div class="label">Active Kalshi market</div>
        <div id="market-title" class="market-title">—</div>
        <p id="target" class="target">—</p>
        <div class="small">Closes <span id="close-time">—</span></div>
        <div class="odds">
          <div class="odd yes"><span class="label">Up midpoint</span><b id="kalshi-yes">—</b></div>
          <div class="odd no"><span class="label">Down midpoint</span><b id="kalshi-no">—</b></div>
        </div>
      </article>

      <article class="card context-panel" aria-labelledby="context-title">
        <div class="context-heading">
          <div>
            <h2 id="context-title">Context horizon comparison</h2>
            <p class="small">Predict the next 15 minutes using two different sets of historical returns. These are held-out model scores.</p>
          </div>
          <span id="context-status" class="pill" role="status">Loading saved comparison…</span>
        </div>
        <div class="context-inputs">
          <div class="context-input"><span class="small">Current feature setup</span><strong>15m / 45m / 3h</strong></div>
          <div class="context-input challenger"><span class="small">Challenger feature setup</span><strong>15m / 30m / 1h</strong></div>
        </div>
        <p id="context-empty" class="small">Loading the hourly experiment report…</p>
        <div id="context-results" hidden>
          <div class="context-controls">
            <div class="context-tabs" role="group" aria-label="Comparison sample">
              <button id="context-all" type="button" aria-pressed="true">All test outcomes</button>
              <button id="context-quotes" type="button" aria-pressed="false">With market quotes</button>
            </div>
            <span id="context-sample" class="small"></span>
          </div>
          <p id="context-table-caption" class="small">Lower Brier score and log loss are better.</p>
          <p class="context-scroll-hint small">Swipe the table to see all scores.</p>
          <div class="context-table-wrap" tabindex="0" role="region" aria-label="Model comparison scores" aria-describedby="context-table-caption">
            <table class="context-table">
              <thead><tr><th scope="col">Predictor</th><th scope="col">Outcomes</th><th scope="col">Brier ↓</th><th scope="col">Log loss ↓</th><th scope="col">Accuracy</th></tr></thead>
              <tbody id="context-models"></tbody>
            </table>
          </div>
          <p id="context-conclusion" class="small"></p>
          <progress id="context-progress" class="context-progress" value="0" max="125" aria-label="Common feature records toward the next test block"></progress>
          <p id="context-progress-detail" class="small"></p>
          <div class="context-footer small"><span id="context-folds"></span><span id="context-updated"></span></div>
          <p class="small">Only the two longer returns change. The 15m return, EMA, volatility, volume and range stay fixed. Report refreshes hourly at minute 05 UTC.</p>
        </div>
      </article>

      <section id="historical-analytics" class="card analytics-panel" aria-labelledby="analytics-title">
        <div class="analytics-heading">
          <div>
            <h2 id="analytics-title">Historical analytics</h2>
            <p class="small">Frozen official forecasts and saved evaluation evidence.</p>
          </div>
          <span id="analytics-status" class="pill" role="status">Loading analytics…</span>
        </div>
        <p id="analytics-warning" class="analytics-warning" role="status" hidden></p>
        <p id="analytics-message" class="small">Analytics are unavailable until the first response arrives.</p>
        <p id="analytics-updated" class="small">Report updated —; age at refresh —</p>
        <dl id="analytics-summary" class="analytics-summary">
          <div class="analytics-stat"><dt>Eligible / sample threshold</dt><dd>— / —</dd></div>
          <div class="analytics-stat"><dt>Accuracy</dt><dd>—</dd></div>
          <div class="analytics-stat"><dt>Brier score</dt><dd>—</dd></div>
          <div class="analytics-stat"><dt>Walk-forward scored / target</dt><dd>— / —</dd></div>
        </dl>
        <div class="analytics-columns">
          <section aria-labelledby="analytics-trend-title">
            <h3 id="analytics-trend-title">Live archive trend</h3>
            <p class="small">Rolling 25-record windows, every 5 records; at least 10 scored outcomes per point. Gaps mean unknown scores. These windows can be newer than the report above.</p>
            <p id="analytics-trend-time" class="small">Live archive window-end issue times: —</p>
            <div id="analytics-trend"><p class="small">Awaiting archive trend.</p></div>
            <details>
              <summary>View numeric trend values</summary>
              <div class="analytics-scroll" tabindex="0" role="region" aria-label="Numeric trend values">
                <table class="analytics-table trend-values">
                  <caption>Live archive windows. All times UTC.</caption>
                  <thead><tr><th scope="col">Window-end issue time</th><th scope="col">Scored</th><th scope="col">Accuracy</th><th scope="col">Brier</th></tr></thead>
                  <tbody id="analytics-trend-values"><tr><td colspan="4">Awaiting archive trend.</td></tr></tbody>
                </table>
              </div>
            </details>
          </section>
          <section class="analytics-coverage" aria-labelledby="analytics-coverage-title">
            <h3 id="analytics-coverage-title">Evidence coverage</h3>
            <p class="small">Report-wide collection counts. Categories may overlap.</p>
            <dl id="analytics-coverage"><div><dt>Timely forecasts</dt><dd>—</dd></div></dl>
          </section>
        </div>
        <section aria-labelledby="analytics-forecasts-title">
          <h3 id="analytics-forecasts-title">Recent archived forecasts</h3>
          <p id="analytics-forecasts-updated" class="small" role="status">Awaiting archived forecasts.</p>
          <p class="small">Earliest eligible forecast per ticker, up to 25 rows ordered newest first. Unknown evidence is —. Scroll the table to see all fields.</p>
          <div class="analytics-scroll" tabindex="0" role="region" aria-label="Recent archived forecasts">
            <table class="analytics-table">
              <caption>Live archive records, newest first. All times UTC; probabilities are raw estimates.</caption>
              <thead><tr><th scope="col">Ticker</th><th scope="col">Issued</th><th scope="col">Up estimate</th><th scope="col">Official result</th><th scope="col">Timing</th><th scope="col">Up midpoint</th><th scope="col">Settlement observed</th><th scope="col">Observation delay</th></tr></thead>
              <tbody id="analytics-forecasts"><tr><td colspan="8">Awaiting archived forecasts.</td></tr></tbody>
            </table>
          </div>
        </section>
        <p id="analytics-note" class="notice">Descriptive archived evidence; not profitability proof.</p>
      </section>

      <article class="card settled">
        <div class="label">Previous market settlement</div>
        <div id="previous-result" class="settled-result">—</div>
        <p id="previous-target" class="target">—</p>
        <div class="small">Closed <span id="previous-close">—</span></div>
        <div class="small">Settled <span id="previous-settlement">—</span></div>
      </article>

      <article class="card metric"><div class="label">Live BTC price</div><div id="spot-price" class="value">—</div><div class="small">Coinbase spot</div></article>
      <article class="card metric"><div class="label">Last complete close</div><div id="close" class="value">—</div><div class="small">15-minute window</div></article>
      <article class="card metric"><div class="label">Cost-adjusted edge</div><div id="edge" class="value">—</div><div class="small">Raw probability − ask − assumed 3c costs</div></article>
      <article class="card metric"><div class="label">Model updates</div><div id="updates" class="value">—</div><div class="small">Online learning steps</div></article>
      <article class="card metric"><div class="label">Training windows</div><div id="bars" class="value">—</div><div class="small">Fetched complete bars</div></article>
      <article class="card metric"><div class="label">Historical match accuracy</div><div id="demo-accuracy" class="value">—</div><div class="small"><span id="demo-record">0–0</span> labels; includes legacy proxy results</div></article>
      <article class="card metric"><div class="label">Hypothetical score P&amp;L</div><div id="demo-pnl" class="value">—</div><div class="small">One-contract paper result</div></article>
      <article class="card metric"><div class="label">Demo balance</div><div id="demo-balance" class="value">—</div><div class="small">Starting balance $100</div></article>
      <article class="card metric"><div class="label">Total traded</div><div id="demo-total-traded" class="value">—</div><div class="small">Settled demo cost</div></article>
      <article class="card metric"><div class="label">Demo win rate</div><div id="demo-win-rate" class="value">—</div><div class="small">TRADE signals only</div></article>

      <article class="card metric"><div class="label">Actual demo P&amp;L</div><div id="account-pnl" class="value">—</div><div class="small">Qualifying paper entries; assumed costs included</div></article>
      <article class="card evidence">
        <div class="label">Recommendation evidence</div>
        <div id="recommendation-stance" class="value" style="font-size:22px">RESEARCH / NOT VALIDATED</div>
        <p id="contract-evidence" class="small">Exact market, entry price, depth, and timing unavailable.</p>
        <p id="validation-evidence" class="small">Collecting official Kalshi outcomes.</p>
        <div id="checks" class="small"></div>
        <ul id="blockers" class="small"></ul>
        <p id="next-step" class="notice">No real orders are placed.</p>
      </article>
      <article class="card wide">
        <div class="label">Learning activity</div>
        <div id="learning" class="value" style="font-size:22px">Waiting for first result</div>
        <p id="learning-detail" class="small">The next refresh after a completed 15-minute window will teach the model whether its previous call was correct.</p>
      </article>
      <article class="card wide">
        <div class="label">Self-learning analysis</div>
        <div id="analysis-status" class="value" style="font-size:22px">Warming up</div>
        <p id="analysis-reason" class="small">Waiting for settled samples.</p>
        <div id="analysis-metrics" class="small">—</div>
        <div id="learning-freshness" class="small" style="margin-top:8px">Learning freshness: —</div>
        <div id="typesafe-review" class="small" style="margin-top:10px">TypeSafe review: optional / not configured</div>
      </article>
      <article class="card wide">
        <div class="label">Durable data collection</div>
        <div id="archive-status" class="value" style="font-size:22px">Waiting for archive</div>
        <p id="archive-detail" class="small">Forward evidence is retained separately from the rolling model state.</p>
      </article>
      <article class="card wide">
        <div class="label">Independent offline evaluation</div>
        <div id="audit-status" class="value" style="font-size:22px">Awaiting audit</div>
        <p id="audit-forward" class="small">Only frozen, timely forecasts with exact official settlements are scored.</p>
        <p id="audit-walk-forward" class="small">Historical training, calibration, and test blocks stay separate.</p>
        <p id="audit-account" class="small">Recorded paper entries only; no synthetic trading returns.</p>
        <p id="audit-note" class="notice">Experimental calibration is offline only and cannot authorize a trade.</p>
      </article>
      <article class="card wide">
        <div class="label">Risk note</div>
        <p class="notice">This is an experimental probability estimate, not a guarantee or financial advice. Kalshi settles BTC contracts using CF Benchmarks BRTI, while model features use Coinbase candles. No orders are placed by this app.</p>
      </article>
    </section>
  </main>

  <script>
    const $ = (id) => document.getElementById(id);
    const pct = (value) => value == null ? 'unknown' : `${(Number(value) * 100).toFixed(1)}%`;
    let recommendationDeadline = 0;
    const money = (value) => value == null ? '—' : `$${Number(value).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2})}`;
    const date = (value) => value ? new Date(value).toLocaleString() : '—';
    function set(id, value) { $(id).textContent = value; }
    let contextReport = null;
    let contextQuoteSubset = false;
    function updateContextFreshness() {
      if (!contextReport?.models) return;
      const age = Date.now() - Date.parse(contextReport.generated_at);
      const stale = !Number.isFinite(age) || age < 0 || age > 125 * 60 * 1000;
      set('context-status', stale ? 'Report stale' : 'Hourly report');
      $('context-status').className = `pill${stale ? ' negative' : ''}`;
    }
    function renderContextComparison(report) {
      contextReport = report;
      const available = report && ['descriptive_evaluation', 'insufficient_data'].includes(report.status) && report.models;
      $('context-empty').hidden = !!available;
      $('context-results').hidden = !available;
      if (!available) {
        set('context-status', report?.status === 'pending' ? 'Awaiting report' : 'Report unavailable');
        $('context-status').className = 'pill';
        set('context-empty', report?.reason || 'The saved comparison could not be loaded. Retry on the next refresh.');
        $('context-models').replaceChildren();
        return;
      }
      updateContextFreshness();
      const coverage = report.coverage;
      const models = contextQuoteSubset ? report.market_midpoint_same_subset : report.models;
      const rows = [
        ['current_15m_45m_3h', 'Current · 15m / 45m / 3h'],
        ['proposed_15m_30m_1h', 'Challenger · 15m / 30m / 1h'],
        ['constant_50_percent', 'Constant 50%'],
      ];
      if (contextQuoteSubset) rows.push(['market_midpoint', 'Kalshi market midpoint']);
      $('context-models').replaceChildren();
      for (const [key, name] of rows) {
        const metrics = key === 'market_midpoint' ? report.market_midpoint_baseline : models[key];
        const row = document.createElement('tr');
        if (key === 'proposed_15m_30m_1h') row.className = 'challenger-row';
        const label = document.createElement('th'); label.scope = 'row'; label.textContent = name; row.append(label);
        for (const value of [metrics.count, metrics.brier_score == null ? '—' : metrics.brier_score.toFixed(4),
                            metrics.log_loss == null ? '—' : metrics.log_loss.toFixed(4),
                            metrics.accuracy == null ? '—' : pct(metrics.accuracy)]) {
          const cell = document.createElement('td'); cell.textContent = value; row.append(cell);
        }
        $('context-models').append(row);
      }
      $('context-all').setAttribute('aria-pressed', String(!contextQuoteSubset));
      $('context-quotes').setAttribute('aria-pressed', String(contextQuoteSubset));
      set('context-sample', `${coverage.market_midpoint_walk_forward} / ${coverage.walk_forward_scored} scored outcomes have issue-time quotes`);
      set('context-table-caption', `${contextQuoteSubset ? 'All four predictors use the same quote-matched outcomes.' : 'Both models and the 50% baseline use the same held-out outcomes.'} Lower Brier score and log loss are better.`);
      const count = models.current_15m_45m_3h.count;
      const bothBehind = models.current_15m_45m_3h.brier_score != null
        && models.current_15m_45m_3h.brier_score >= models.constant_50_percent.brier_score
        && models.proposed_15m_30m_1h.brier_score >= models.constant_50_percent.brier_score;
      set('context-conclusion', count === 0 ? (contextQuoteSubset ? 'No scored test outcomes have valid issue-time market quotes yet.' : 'Collecting enough prior-known labels for the first held-out test block.')
        : `${count < 100 ? 'Small sample; no model selection yet.' : 'Descriptive comparison; model selection requires consistent results across blocks.'}${bothBehind ? ' Both models currently trail or tie the 50% Brier baseline.' : ''}`);
      const target = report.next_block_feature_target;
      const common = coverage.both_feature_sets;
      $('context-progress').max = target; $('context-progress').value = Math.min(common, target);
      set('context-progress-detail', `${common} / ${target} common feature records for the next ${report.configuration.test_size}-record block. ${Math.max(0, target - common)} more needed; training labels must be available before the block starts.`);
      set('context-folds', `${report.fold_count} evaluated block${report.fold_count === 1 ? '' : 's'} · ${coverage.walk_forward_unscored} tested outcomes awaiting valid labels`);
      set('context-updated', `Report updated ${date(report.generated_at)}`);
    }
    async function refreshContextComparison() {
      try {
        const response = await fetch(`/api/context-comparison?ts=${Date.now()}`, {cache: 'no-store'});
        if (!response.ok) throw new Error('Comparison endpoint unavailable');
        renderContextComparison(await response.json());
      } catch (error) {
        renderContextComparison({status: 'unavailable', reason: 'The saved comparison could not be loaded. Retry on the next refresh.'});
      }
    }
    // Analytics never supplies recommendation state or infers missing evidence.
    const analyticsNumber = value => typeof value === 'number' && Number.isFinite(value);
    const analyticsCount = value => analyticsNumber(value) && Number.isInteger(value) && value >= 0 ? String(value) : '—';
    const analyticsUnit = value => analyticsNumber(value) && value >= 0 && value <= 1;
    const analyticsPercent = value => analyticsUnit(value) ? `${(value * 100).toFixed(1)}%` : '—';
    const analyticsScore = value => analyticsUnit(value) ? value.toFixed(3) : '—';
    const analyticsSeconds = value => analyticsNumber(value) && value >= 0 ? `${value.toFixed(1)} s` : '—';
    function analyticsTimestamp(value) {
      const time = typeof value === 'string' ? Date.parse(value) : analyticsNumber(value) ? value * 1000 : NaN;
      return Number.isFinite(time) && Number.isFinite(new Date(time).getTime()) ? time : null;
    }
    function analyticsDate(value) {
      const time = analyticsTimestamp(value);
      return time === null ? '—' : `${new Date(time).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
    }
    function analyticsNode(tag, text, className) {
      const node = document.createElement(tag);
      if (text != null) node.textContent = text;
      if (className) node.className = className;
      return node;
    }
    function analyticsEmptyRow(target, columns, message) {
      const row = analyticsNode('tr');
      const cell = analyticsNode('td', message); cell.setAttribute('colspan', columns);
      row.append(cell); target.replaceChildren(row);
    }
    function analyticsPlot(points, key, label, format) {
      const holder = analyticsNode('div', null, `analytics-plot${key === 'brier_score' ? ' brier' : ''}`);
      holder.append(analyticsNode('h4', label));
      function svgNode(tag, attributes = {}, text) {
        const node = document.createElementNS('http://www.w3.org/2000/svg', tag);
        for (const [name, value] of Object.entries(attributes)) node.setAttribute(name, value);
        if (text != null) node.textContent = text;
        return node;
      }
      const titleId = `analytics-${key}-title`, descId = `analytics-${key}-description`;
      const svg = svgNode('svg', {viewBox:'0 0 560 140', role:'img', 'aria-labelledby':titleId, 'aria-describedby':descId});
      svg.append(svgNode('title', {id:titleId}, label), svgNode('desc', {id:descId},
        'Live archive rolling windows, ordered by window-end issue time. Missing values break the line. Exact times, scores and sample counts are in View numeric trend values.'));
      for (const value of [0, .5, 1]) {
        const y = 110 - value * 96;
        svg.append(svgNode('line', {x1:80, y1:y, x2:548, y2:y, class:'plot-grid'}),
          svgNode('text', {x:72, y:y + 4, 'text-anchor':'end'}, key === 'accuracy' ? `${value * 100}%` : value.toFixed(1)));
      }
      const times = points.map(point => analyticsTimestamp(point?.issued_at)).filter(time => time !== null);
      const first = Math.min(...times), last = Math.max(...times);
      let segment = [];
      function flushSegment() {
        if (segment.length > 1) svg.append(svgNode('polyline', {points:segment.join(' '), class:'plot-series'}));
        segment = [];
      }
      let plotted = 0;
      for (const point of points) {
        const time = analyticsTimestamp(point?.issued_at), value = point?.[key];
        if (time === null || !analyticsUnit(value)) { flushSegment(); continue; }
        const x = first === last ? 314 : 80 + (time - first) / (last - first) * 468;
        const y = 110 - value * 96;
        segment.push(`${x.toFixed(2)},${y.toFixed(2)}`);
        const dot = svgNode('circle', {cx:x, cy:y, r:3});
        dot.append(svgNode('title', {}, `${analyticsDate(point.issued_at)}: ${format(value)}; ${analyticsCount(point.count)} scored`));
        svg.append(dot); plotted++;
      }
      flushSegment();
      svg.append(svgNode('text', {x:80, y:134}, 'Earlier'), svgNode('text', {x:548, y:134, 'text-anchor':'end'}, 'Later'));
      holder.append(svg);
      if (!plotted) holder.append(analyticsNode('p', 'No scored trend values available yet.', 'small'));
      return holder;
    }
    function renderAnalyticsTrend(trend) {
      const points = Array.isArray(trend) ? trend.slice(-25) : [];
      const target = $('analytics-trend'), values = $('analytics-trend-values');
      target.replaceChildren(); values.replaceChildren();
      const times = points.map(point => analyticsTimestamp(point?.issued_at)).filter(time => time !== null);
      set('analytics-trend-time', times.length
        ? `Live archive window-end issue times: ${analyticsDate(Math.min(...times) / 1000)} to ${analyticsDate(Math.max(...times) / 1000)}`
        : 'Live archive window-end issue times: —');
      if (!points.length) {
        target.append(analyticsNode('p', 'No rolling windows available. Collecting archived evidence.', 'small'));
        analyticsEmptyRow(values, 4, 'No rolling windows available.');
        return;
      }
      target.append(analyticsPlot(points, 'accuracy', 'Accuracy (%) · higher is better', analyticsPercent),
        analyticsPlot(points, 'brier_score', 'Brier score (0–1) · lower is better', analyticsScore));
      for (const point of points) {
        const row = analyticsNode('tr'), time = analyticsNode('th', analyticsDate(point?.issued_at));
        time.setAttribute('scope', 'row'); row.append(time);
        for (const value of [analyticsCount(point?.count), analyticsPercent(point?.accuracy), analyticsScore(point?.brier_score)]) {
          row.append(analyticsNode('td', value));
        }
        values.append(row);
      }
    }
    function renderAnalytics(report) {
      const available = report?.status === 'available';
      const pending = report?.status === 'pending';
      const stale = available && (report.report_freshness === 'stale' || report.freshness?.status === 'stale');
      const summary = available ? report.summary || {} : {};
      const walk = available ? report.walk_forward || {} : {};
      const coverage = available ? report.coverage || {} : {};
      set('analytics-status', stale ? 'Report stale' : available
        ? summary.eligible_count === 0 ? 'Report empty · no eligible forecasts' : 'Report available'
        : pending ? 'Report pending' : 'Report unavailable');
      $('analytics-status').className = `pill${stale ? ' analytics-status-stale' : ''}`;
      $('analytics-warning').hidden = !stale;
      set('analytics-warning', stale ? 'Stale report — diagnostic-only. These saved statistics cannot establish validation readiness or authorize an entry.' : '');
      set('analytics-message', available
        ? 'Summary and coverage are report-wide statistics. Analytics do not establish validation readiness.'
        : typeof report?.reason === 'string' ? report.reason : pending
          ? 'Awaiting the first saved evaluation report.' : 'Historical analytics are unavailable. Retry on the next refresh.');
      const age = available && analyticsNumber(report.report_age_seconds) && report.report_age_seconds >= 0
        ? report.report_age_seconds < 3600 ? `${Math.floor(report.report_age_seconds / 60)} min`
          : `${(report.report_age_seconds / 3600).toFixed(1)} h` : '—';
      set('analytics-updated', `Report updated ${available ? analyticsDate(report.generated_at) : '—'}; age at refresh ${age}`);
      const interval = summary.accuracy_wilson_95;
      const intervalText = analyticsUnit(interval?.lower) && analyticsUnit(interval?.upper) && interval.lower <= interval.upper
        ? `${analyticsPercent(interval.lower)}–${analyticsPercent(interval.upper)}` : '—';
      // The current API does not project forward scored_count. Do not substitute
      // eligible_count, walk-forward counts, or the newest rolling sample count.
      const stats = [
        ['Eligible / sample threshold', `${analyticsCount(summary.eligible_count)} / ${analyticsCount(summary.minimum_count)}`, 'Eligible forecasts; threshold applies to scored evidence.'],
        ['Accuracy', analyticsPercent(summary.accuracy), `95% Wilson interval ${intervalText}. Scored count: ${analyticsCount(summary.scored_count)} (report).`],
        ['Brier score', analyticsScore(summary.brier_score), `Constant 50% baseline: ${analyticsScore(summary.constant_50_brier)}. Lower is better.`],
        ['Walk-forward scored / target', `${analyticsCount(walk.scored_count)} / ${analyticsCount(walk.test_target)}`, `${analyticsCount(walk.additional_scored_samples_needed)} more scored outcomes needed.`],
      ];
      $('analytics-summary').replaceChildren();
      for (const [label, value, detail] of stats) {
        const item = analyticsNode('div', null, 'analytics-stat'), data = analyticsNode('dd', value);
        data.append(analyticsNode('span', detail, 'small'));
        item.append(analyticsNode('dt', label), data); $('analytics-summary').append(item);
      }
      $('analytics-coverage').replaceChildren();
      const coverageRows = [
        ['Timely forecasts', analyticsCount(coverage.timely)], ['Late forecasts excluded', analyticsCount(coverage.late)],
        ['Missing ticker', analyticsCount(coverage.missing_ticker)], ['Missing features', analyticsCount(coverage.missing_features)],
        ['Missing fresh quotes', analyticsCount(coverage.missing_fresh_quotes)], ['Interval gaps', analyticsCount(coverage.interval_gaps)],
        ['Settlement delay · median', analyticsSeconds(coverage.settlement_delay_seconds?.median)],
        ['Settlement delay · 95th percentile', analyticsSeconds(coverage.settlement_delay_seconds?.p95)],
        ['Settlement delay · maximum', analyticsSeconds(coverage.settlement_delay_seconds?.max)],
      ];
      for (const [label, value] of coverageRows) {
        const row = analyticsNode('div'); row.append(analyticsNode('dt', label), analyticsNode('dd', value));
        $('analytics-coverage').append(row);
      }
      // A pending/unavailable saved report can still carry a newly read archive trend.
      renderAnalyticsTrend(report?.trend);
    }
    function renderAnalyticsForecasts(payload) {
      const target = $('analytics-forecasts'); target.replaceChildren();
      const available = payload?.status === 'available' && Array.isArray(payload.rows);
      const rows = available ? payload.rows.slice(0, 25) : [];
      const message = !available ? payload?.status === 'pending' ? 'Awaiting the first archived forecasts.'
        : 'Archived forecasts unavailable. Retry on the next refresh.' : !rows.length ? 'No eligible archived forecasts yet.'
          : `${rows.length} archived forecasts shown.`;
      set('analytics-forecasts-updated', `${message}${available ? ` Archive read ${analyticsDate(Date.now() / 1000)}.` : ''}`);
      if (!rows.length) { analyticsEmptyRow(target, 8, message); return; }
      for (const forecast of rows) {
        const row = analyticsNode('tr');
        const ticker = analyticsNode('th', typeof forecast?.market_ticker === 'string' && forecast.market_ticker ? forecast.market_ticker : '—');
        ticker.setAttribute('scope', 'row'); row.append(ticker);
        const values = [analyticsDate(forecast?.issued_at), analyticsPercent(forecast?.probability_up),
          forecast?.result === 'yes' ? 'Yes / up' : forecast?.result === 'no' ? 'No / down' : '—',
          forecast?.timing_status === 'timely' ? 'Timely' : forecast?.timing_status === 'late' ? 'Late' : '—',
          analyticsPercent(forecast?.yes_mid), analyticsDate(forecast?.settlement_available_at), analyticsSeconds(forecast?.settlement_delay_seconds)];
        values.forEach((value, index) => row.append(analyticsNode('td', value, index === 0 || index === 5 ? 'analytics-time' : '')));
        target.append(row);
      }
    }
    let analyticsInFlight = false;
    async function refreshAnalytics() {
      if (analyticsInFlight) return;
      analyticsInFlight = true;
      async function load(url, renderer) {
        const controller = new AbortController();
        const timeout = setTimeout(() => controller.abort(), 10000);
        try {
          const response = await fetch(url, {cache:'no-store', signal:controller.signal});
          if (!response.ok) throw new Error('Analytics endpoint unavailable');
          renderer(await response.json());
        } catch (error) {
          renderer({status:'unavailable'});
        } finally {
          clearTimeout(timeout);
        }
      }
      try {
        const stamp = Date.now();
        await Promise.all([load(`/api/analytics?ts=${stamp}`, renderAnalytics),
          load(`/api/analytics/forecasts?limit=25&ts=${stamp}`, renderAnalyticsForecasts)]);
      } finally {
        analyticsInFlight = false;
      }
    }
    function render(payload) {
      const prediction = payload.prediction || {};
      const market = prediction.kalshi;
      const previous = payload.previous_settled;
      const spot = payload.spot;
      const up = Number(prediction.probability_up || 0);
      const down = Number(prediction.probability_down || 0);
      const direction = prediction.direction || '—';
      const directionEl = $('direction');
      directionEl.textContent = direction;
      directionEl.className = `direction ${direction.toLowerCase()}`;
      set('probability', pct(direction === 'UP' ? up : down));
      set('up-probability', pct(up));
      set('down-probability', pct(down));
      $('probability-bar').className = `bar ${direction.toLowerCase()}`;
      $('probability-bar').firstElementChild.style.width = `${(direction === 'UP' ? up : down) * 100}%`;
      set('window-time', date(prediction.bar_time));
      const forecastLabels = {issued:'Saved forecast', awaiting_market_evidence:'Awaiting market evidence — estimate only', missed_window:'Entry window missed — no forecast saved', awaiting_settlement:'Awaiting previous official settlement'};
      set('forecast-status', `${forecastLabels[prediction.forecast_status] || 'Forecast status unavailable'}${prediction.forecast_issued_at ? ` • issued ${date(Number(prediction.forecast_issued_at) * 1000)}` : ''}`);
      set('close', money(prediction.close));
      set('spot-price', spot ? money(spot.price) : '—');
      if (previous) {
        const result = (previous.result || '').toLowerCase();
        set('previous-result', result === 'yes' ? 'YES / UP' : result === 'no' ? 'NO / DOWN' : '—');
        $('previous-result').className = `settled-result ${result}`;
        set('previous-target', previous.target || 'Target unavailable');
        set('previous-close', date(previous.close_time));
        set('previous-settlement', date(previous.settlement_time));
      } else {
        set('previous-result', 'No settled market');
        set('previous-target', '—'); set('previous-close', '—'); set('previous-settlement', '—');
      }
      set('updates', prediction.model_updates ?? '—');
      set('bars', payload.bars ?? '—');
      const paper = payload.paper || {};
      set('demo-accuracy', paper.accuracy == null ? '—' : pct(paper.accuracy));
      set('demo-record', `${paper.wins || 0}–${paper.losses || 0}`);
      const pnl = Number(paper.pnl || 0);
      set('demo-pnl', paper.settled_trades ? `${pnl >= 0 ? '+' : ''}$${pnl.toFixed(3)}` : '—');
      $('demo-pnl').className = `value ${pnl >= 0 ? 'positive' : 'negative'}`;
      const analysis = prediction.analysis || {};
      const typeSafe = prediction.typesafe_review || {};
      const typeSafeText = typeSafe.status === 'ok'
        ? `TypeSafe review: ${typeSafe.choice || 'unknown'} (${pct(typeSafe.confidence)}) • ${typeSafe.applies_to_current_evidence ? 'current evidence' : 'expired/different evidence; cannot authorize'} • not a BTC forecast`
        : `TypeSafe review: ${typeSafe.status || 'not configured'} • ${typeSafe.reason || 'optional second-opinion veto'}`;
      set('typesafe-review', typeSafeText);
      set('analysis-status', `${analysis.status || 'WARMING UP'} • ${analysis.suggestion || 'WAIT'}`);
      set('analysis-reason', analysis.reason || 'Waiting for settled samples.');
      const accuracy = analysis.rolling_accuracy == null ? '—' : pct(analysis.rolling_accuracy);
      const brier = analysis.rolling_brier == null ? '—' : Number(analysis.rolling_brier).toFixed(3);
      const analysisPnl = Number(analysis.rolling_pnl || 0);
      set('analysis-metrics', `${analysis.settled_samples || 0} settled samples • rolling accuracy ${accuracy} • Brier ${brier} • recent P&L ${analysisPnl >= 0 ? '+' : ''}$${analysisPnl.toFixed(3)}`);
      const freshness = analysis.learning_freshness || {};
      const freshnessAge = freshness.last_learned_age_bars == null ? 'no learned result yet' : `${freshness.last_learned_age_bars} windows since last learned result`;
      set('learning-freshness', `Learning freshness: ${freshness.status || 'UNKNOWN'} • ${freshnessAge} • skipped ${freshness.skipped_predictions || 0}`);
      const signal = prediction.trade_signal || 'WAIT';
      const signalEl = $('trade-signal');
      signalEl.textContent = signal === 'TRADE' ? 'TRADE NOW' : 'WAIT';
      signalEl.className = `signal ${signal === 'TRADE' ? 'trade' : 'wait'}`;
      const archive = prediction.archive || {};
      const archiveCounts = archive.counts || {};
      set('archive-status', archive.status === 'ok' ? 'SQLite archive active' : 'Archive unavailable / entries withheld');
      set('archive-detail', archive.status === 'ok' ? `${archiveCounts.candles || 0} minute candles • ${archiveCounts.bars || 0} complete bars • ${archiveCounts.forecasts || 0} forecasts • ${archiveCounts.settlements || 0} official observations • ${archiveCounts.decisions || 0} decision snapshots. Imports are labelled; missing past quotes/features are not invented.` : archive.reason || 'Collecting point-in-time evidence.');
      const audit = payload.validation || {};
      const auditForward = audit.forward || {};
      const auditWalk = audit.walk_forward || {};
      const auditAccount = audit.paper_account || {};
      const auditMetrics = auditForward.metrics || {};
      const auditAge = audit.generated_at ? Date.now() - Date.parse(audit.generated_at) : Infinity;
      const auditStale = !Number.isFinite(auditAge) || auditAge > 26 * 3600 * 1000 || auditAge < 0;
      set('audit-status', `${(audit.status || 'pending').replaceAll('_', ' ').toUpperCase()}${audit.generated_at && auditStale ? ' • STALE SNAPSHOT' : ''}`);
      const collection = (audit.data_quality || {}).collection_quality || {};
      const timelyRate = collection.timely_rate_of_official_rows == null ? 'unknown' : pct(collection.timely_rate_of_official_rows);
      set('audit-forward', auditForward.status ? `Frozen forward forecasts: ${auditForward.scored_count || 0}/${auditForward.minimum_evidence_samples || 100} scored • accuracy ${pct(auditMetrics.accuracy)} • Brier ${auditMetrics.brier_score == null ? 'unknown' : Number(auditMetrics.brier_score).toFixed(3)} • not evidence of trading profitability. Collection: ${collection.timely_forecast_rows || 0}/${collection.official_forecast_rows || 0} timely official forecasts (${timelyRate}); late excluded ${collection.excluded_late_forecasts || 0}.` : audit.reason || 'Awaiting the first scheduled read-only audit.');
      set('audit-walk-forward', auditWalk.status ? `Walk-forward: ${auditWalk.fold_count || 0} folds • ${auditWalk.scored_count || 0} held-out outcomes • ${auditWalk.train_size || 100} training + ${auditWalk.calibration_size || 50} calibration + ${auditWalk.test_size || 25} test records per block • ${(auditWalk.status || '').replaceAll('_', ' ')}.` : 'No evaluated walk-forward folds yet.');
      set('audit-account', auditAccount.status ? `Recorded account audit: ${(auditAccount.status || '').replaceAll('_', ' ')} • ${auditAccount.settled_count || 0} settled • ${auditAccount.unresolved_count || 0} unresolved • net realized P&L ${money(auditAccount.net_realized_pnl)}${auditAccount.run_count > 1 ? ' • independent account resets, no combined return' : ''}.` : 'No audited account results yet.');
      const settlementDelay = collection.settlement_delay_seconds || {};
      set('audit-note', `${audit.generated_at ? `Audit snapshot ${date(audit.generated_at)}. ` : ''}Settlement observation delay median ${settlementDelay.median == null ? 'unknown' : `${Number(settlementDelay.median).toFixed(0)}s`}; report refreshes on runner startup and daily UTC. It is not a live eligibility gate. Platt calibration remains experimental and offline only. Full report stays on disk.`);
      const recommendation = prediction.recommendation || {};
      const blockers = recommendation.blockers || [];
      recommendationDeadline = Number(recommendation.valid_until || 0) * 1000;
      set('trade-reason', blockers.length ? `${blockers.slice(0, 3).join('; ')}${blockers.length > 3 ? `; +${blockers.length - 3} more checks below` : ''}` : prediction.trade_reason || 'No signal reason available.');
      set('recommendation-stance', `${recommendation.stance || 'RESEARCH / NOT VALIDATED'} • ${recommendation.action || 'WAIT'}`);
      const ask = prediction.market_entry_price == null ? 'unknown' : money(prediction.market_entry_price);
      const depth = prediction.market_liquidity == null ? 'unknown (not zero)' : `${Number(prediction.market_liquidity).toFixed(2)} contracts`;
      set('contract-evidence', `${recommendation.market_ticker || 'No aligned ticker'} • ${direction} ask ${ask} • executable ask depth ${depth} • spread ${pct(prediction.market_spread)} • ${market?.quote_source || 'no orderbook'} • deadline ${recommendationDeadline ? new Date(recommendationDeadline).toLocaleTimeString() : 'unknown'}${prediction.open_paper_trade ? ' • an earlier paper entry is already committed' : ''}`);
      const validation = analysis.official_validation || {};
      set('validation-evidence', `Official validation: ${validation.samples || 0}/${validation.minimum_samples || 100} eligible exact-market settlements • accuracy ${pct(validation.accuracy)} • Brier ${validation.brier == null ? 'unknown' : Number(validation.brier).toFixed(3)} (50% baseline: 0.250). Historical proxy matches do not count. Passing this gate is not proof of profitability.`);
      const checkNames = {probability:'Valid raw probability', candle_freshness:'Latest complete, gap-free candle', market_alignment:'Exact forecast / contract interval', market_open:'Contract open and active', quote_freshness:'Quote age ≤60 seconds', entry_timing:'Entry within first 120 seconds', confidence:'Raw confidence ≥65%', indicators:'At least 3/4 trend signals agree', features:'Valid complete feature history', market:'Executable, uncrossed prices', orderbook:'Public orderbook confirmed', forecast_evidence:'Timely exact-contract quote evidence', edge:'Net edge ≥8 percentage points after assumed costs', spread:'Spread ≤8 percentage points', liquidity:'Executable ask depth ≥25 contracts', volatility:'Existing volatility/range limits', risk_validation:'Official validation and risk gates', pending_slot:'No older unsettled forecast', pending_contract:'Quotes match the frozen forecast ticker', budget:'Paper budget and depth sufficient', typesafe:'Current TypeSafe approval', archive:'Durable evidence archive available'};
      $('checks').replaceChildren();
      for (const [key, passed] of Object.entries(recommendation.checks || {})) {
        const row = document.createElement('div'); row.className = 'check-row';
        const name = document.createElement('span'); name.textContent = checkNames[key] || key;
        const result = document.createElement('strong'); result.textContent = passed ? 'PASS' : 'BLOCK'; result.className = passed ? 'check-pass' : 'check-block';
        row.append(name, result); $('checks').append(row);
      }
      $('blockers').replaceChildren();
      for (const blocker of blockers) { const li = document.createElement('li'); li.textContent = blocker; $('blockers').append(li); }
      set('next-step', `${recommendation.next_step || 'Collect and validate evidence before paper entries.'} ${recommendation.cost_note || ''}`);
      const account = payload.demo_account || {};
      set('demo-balance', money(account.balance));
      set('demo-total-traded', money(account.total_traded));
      set('demo-win-rate', account.accuracy == null ? '—' : pct(account.accuracy));
      set('account-pnl', money(account.realized_pnl));
      if (market) {
        set('market-title', market.title || 'BTC market');
        set('target', market.target || 'Target unavailable');
        set('close-time', date(market.close_time));
        set('kalshi-yes', pct(market.yes_mid));
        set('kalshi-no', pct(market.no_mid));
        const edge = prediction.model_edge;
        set('edge', edge == null ? 'unknown' : `${edge >= 0 ? '+' : ''}${(Number(edge) * 100).toFixed(1)} pp`);
        $('edge').className = `value ${edge != null && edge >= 0 ? 'positive' : 'negative'}`;
      } else {
        set('market-title', 'Kalshi unavailable');
        set('target', ''); set('close-time', '—'); set('kalshi-yes', '—'); set('kalshi-no', '—'); set('edge', '—');
      }
      const learned = payload.learned_previous;
      if (learned?.status === 'skipped_stale') {
        set('learning', 'Skipped unresolved forecast — no invented outcome');
        set('learning-detail', learned.reason || 'Exact settlement unavailable.');
      } else if (learned?.outcome) {
        const paperTrade = learned.paper_trade;
        const match = paperTrade ? (paperTrade.correct ? 'MATCH' : 'MISS') : learned.outcome;
        const pnlText = paperTrade ? ` ${paperTrade.pnl >= 0 ? '+' : ''}$${Number(paperTrade.pnl).toFixed(3)} hypothetical score P&L (not account money).` : '';
        set('learning', `Prediction match: ${match}`);
        set('learning-detail', `Label source: ${learned.outcome_source || 'legacy Coinbase proxy'} • ${learned.market_ticker || 'no exact ticker'}. Probability before update: ${pct(learned.probability_before_update)}.${pnlText}`);
      } else {
        set('learning', 'No new official result this refresh');
        set('learning-detail', 'Waiting for the queued exact-market settlement; Coinbase direction is not a substitute.');
      }
      $('learning-detail').className = 'small';
      set('status-text', `Live • updated ${new Date(payload.generated_at).toLocaleTimeString()}`);
      $('status-dot').className = 'dot ok';
    }
    async function refresh() {
      set('status-text', 'Refreshing live data…');
      try {
        const response = await fetch(`/api/status?ts=${Date.now()}`, {cache: 'no-store'});
        const payload = await response.json();
        if (!response.ok || !payload.ok) throw new Error(payload.error || 'The server returned an error.');
        render(payload);
      } catch (error) {
        $('status-dot').className = 'dot error';
        set('status-text', 'Could not load live data — recommendations withheld');
        recommendationDeadline = 0;
        set('trade-signal', 'WAIT / DATA UNAVAILABLE'); $('trade-signal').className = 'signal wait';
        set('trade-reason', 'Previous evidence is not a current recommendation. Retry when fresh data is available.');
        set('recommendation-stance', 'WAIT / DATA UNAVAILABLE');
        set('learning-detail', error.message);
        $('learning-detail').className = 'small error';
      }
    }
    function refreshDashboard() { refresh(); refreshContextComparison(); refreshAnalytics(); }
    $('refresh').addEventListener('click', refreshDashboard);
    $('context-all').addEventListener('click', () => { contextQuoteSubset = false; renderContextComparison(contextReport); });
    $('context-quotes').addEventListener('click', () => { contextQuoteSubset = true; renderContextComparison(contextReport); });
    function updateClock() {
      set('clock', new Date().toLocaleString());
      updateContextFreshness();
      if ($('trade-signal').classList.contains('trade') && Date.now() > recommendationDeadline) {
        set('trade-signal', 'WAIT / EVIDENCE EXPIRED'); $('trade-signal').className = 'signal wait';
        set('trade-reason', 'The quote/entry deadline passed. Refresh for a new recommendation.');
        set('recommendation-stance', 'WAIT / EVIDENCE EXPIRED');
      }
    }
    updateClock();
    setInterval(updateClock, 1000);
    refreshDashboard();
    setInterval(refreshDashboard, 30000);
  </script>
</body>
</html>
"""


class DashboardApp:
    def __init__(self, state_file: str, lookback_minutes: int, cache_seconds: int = 20):
        self.state_file = Path(state_file)
        self.lookback_minutes = lookback_minutes
        self.cache_seconds = cache_seconds
        self._cache: tuple[float, dict[str, Any]] | None = None
        self._lock = threading.Lock()

    def context_comparison(self) -> dict[str, Any]:
        return read_context_comparison(self.state_file)

    def analytics(self) -> dict[str, Any]:
        payload = read_historical_analytics(self.state_file)
        # Saved-report reasons may contain private diagnostics; publish fixed text.
        if payload.get("status") == "pending":
            payload["reason"] = "Awaiting historical analytics evidence."
        elif payload.get("status") == "unavailable":
            payload["reason"] = "Historical analytics are unavailable."
        return payload

    def recent_forecasts(self, limit: int) -> dict[str, Any]:
        return read_recent_forecasts(self.state_file, limit)

    def status(self) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if self._cache and now - self._cache[0] < self.cache_seconds:
                return self._cache[1]
            try:
                raw = fetch_candles(lookback_minutes=self.lookback_minutes)
                prices_observed_at = time.time()
                bars = aggregate_candles(raw)
                if len(bars) < 22:
                    raise RuntimeError(f"Only {len(bars)} complete 15-minute bars are available.")

                kalshi = None
                previous_settled = None
                spot = None
                context_errors = []
                try:
                    kalshi = fetch_kalshi_market()
                except RuntimeError as exc:
                    context_errors.append(str(exc))
                try:
                    previous_settled = fetch_previous_kalshi_market()
                except RuntimeError as exc:
                    context_errors.append(str(exc))
                try:
                    spot = fetch_spot_price()
                except RuntimeError as exc:
                    context_errors.append(str(exc))

                with state_transaction(self.state_file):
                    if self.state_file.exists():
                        model, state = load_state(self.state_file)
                        learned = learn_from_pending(model, state, bars)
                        bootstrapped = False
                    else:
                        model, state, metrics = bootstrap_from_bars(bars)
                        state["bootstrap_metrics"] = metrics
                        learned = None
                        bootstrapped = True

                    prediction = predict_and_queue(model, state, bars, kalshi)
                    prediction = finalize_prediction(prediction, state)
                    prediction = archive_and_guard(self.state_file, state, bars, prediction, raw_candles=raw,
                                                   prices_observed_at=prices_observed_at)
                    save_state(self.state_file, model, state)
                    payload: dict[str, Any] = {
                        "ok": True,
                        "generated_at": datetime.now(timezone.utc).isoformat(),
                        "bars": len(bars),
                        "learned_previous": learned,
                        "bootstrapped": bootstrapped,
                        "paper": paper_trade_summary(state),
                        "demo_account": demo_account_summary(state),
                        "spot": spot,
                        "previous_settled": previous_settled,
                        "prediction": prediction,
                        "validation": read_validation_summary(self.state_file),
                    }
                    if context_errors:
                        payload["context_errors"] = context_errors
            except Exception as exc:
                payload = {
                    "ok": False,
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "error": str(exc),
                }
            self._cache = (now, payload)
            return payload


class DashboardHandler(BaseHTTPRequestHandler):
    app: DashboardApp

    def _send(self, body: bytes, content_type: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, allow_nan=False, separators=(",", ":")).encode("utf-8")
        self._send(body, "application/json; charset=utf-8", status)

    @staticmethod
    def _unavailable_analytics() -> dict[str, str]:
        return {"status": "unavailable", "reason": "Historical analytics are unavailable."}

    @staticmethod
    def _unavailable_forecasts() -> dict[str, Any]:
        return {
            "status": "unavailable",
            "reason": "Archived forecasts are unavailable.",
            "rows": [],
        }

    @staticmethod
    def _parse_limit(value: str) -> int:
        if not value or not value.isdecimal():
            raise ValueError("invalid analytics limit")
        limit = int(value)
        if not 1 <= limit <= 25:
            raise ValueError("invalid analytics limit")
        return limit

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._send(HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/health":
            self._send(b'{"ok":true}', "application/json; charset=utf-8")
            return
        if path == "/api/context-comparison":
            self._send(json.dumps(self.app.context_comparison(), allow_nan=False).encode("utf-8"),
                       "application/json; charset=utf-8")
            return
        if path == "/api/analytics":
            try:
                self._json(self.app.analytics())
            except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError):
                self._json(self._unavailable_analytics())
            return
        if path == "/api/analytics/forecasts":
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            try:
                limit = self._parse_limit(query.get("limit", ["25"])[0])
            except (TypeError, ValueError):
                self._json({"error": "invalid analytics limit"}, HTTPStatus.BAD_REQUEST)
                return
            try:
                self._json(self.app.recent_forecasts(limit))
            except (OSError, sqlite3.Error, ValueError, KeyError, json.JSONDecodeError):
                self._json(self._unavailable_forecasts())
            return
        if path == "/api/status":
            payload = self.app.status()
            status = HTTPStatus.OK if payload.get("ok") else HTTPStatus.SERVICE_UNAVAILABLE
            self._send(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8", status)
            return
        self._send(b"Not found", "text/plain; charset=utf-8", HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: Any) -> None:
        # Keep the terminal useful without logging every browser refresh.
        if urlparse(self.path).path not in {"/api/status", "/api/context-comparison"}:
            super().log_message(format, *args)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-file", default="data/btc_15m_state.json")
    parser.add_argument("--lookback-minutes", type=int, default=DEFAULT_LOOKBACK_MINUTES)
    args = parser.parse_args()

    app = DashboardApp(args.state_file, args.lookback_minutes)
    handler = type("BoundDashboardHandler", (DashboardHandler,), {"app": app})
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"BTC 15m dashboard running at http://{args.host}:{args.port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
