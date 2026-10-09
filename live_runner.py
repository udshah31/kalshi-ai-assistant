"""Monitor the predictor and paper account for a timed or indefinite session."""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any


from btc_predictor import (
    INTERVAL_SECONDS, MAX_ENTRY_DELAY_SECONDS, load_state, official_validation, state_transaction,
)
from forecast_archive import archive_path, validation_paths


LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3
MILESTONE_TARGET = 100


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def check_dashboard_health(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Check HTTP liveness; prediction freshness is checked separately."""
    try:
        with urllib.request.urlopen(f"{url.rstrip('/')}/health", timeout=timeout) as response:
            body = response.read().decode("utf-8")
            healthy = response.status == 200 and json.loads(body).get("ok") is True
        return healthy, body
    except (OSError, ValueError, AttributeError) as exc:
        return False, str(exc)


def run_prediction(
    state_file: str,
    lookback_minutes: int,
    kalshi_series: str,
) -> dict[str, Any]:
    script = Path(__file__).with_name("btc_predictor.py")
    command = [
        sys.executable, str(script),
        "--lookback-minutes", str(lookback_minutes),
        "--state-file", str(Path(state_file).resolve()),
        "--kalshi-series", kalshi_series, "run",
    ]
    try:
        completed = subprocess.run(
            command, cwd=script.parent, capture_output=True, text=True,
            timeout=120, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        # Keep the monitor alive and retry on the next poll, not next quarter-hour.
        return {"exit_code": 1, "result": {"error": str(exc)}, "stderr": ""}
    output = completed.stdout.strip()
    try:
        payload: Any = json.loads(output) if output else {"error": completed.stderr.strip()}
    except json.JSONDecodeError:
        payload = {"error": output or completed.stderr.strip()}
    return {
        "exit_code": completed.returncode,
        "result": payload,
        "stderr": completed.stderr.strip(),
    }


def current_prediction_cycle(now: float | None = None) -> tuple[int, float]:
    """Return the current 15-minute cycle and elapsed seconds in it."""
    now = time.time() if now is None else now
    cycle = int(now) // INTERVAL_SECONDS
    return cycle, now - cycle * INTERVAL_SECONDS


def initial_prediction_cycle(now: float | None = None) -> int | None:
    """Do not create a late first forecast when launchd starts mid-window."""
    cycle, elapsed = current_prediction_cycle(now)
    return None if elapsed <= MAX_ENTRY_DELAY_SECONDS else cycle


def recommendation_needs_retry(prediction: dict[str, Any], now: float) -> bool:
    """Retry late settlements without a browser; retry entry data only early on."""
    checks = prediction.get("signal_checks") or {}
    if checks.get("pending_slot") is False:
        return True
    if prediction.get("forecast_status") == "awaiting_market_evidence":
        return True
    if now % INTERVAL_SECONDS > MAX_ENTRY_DELAY_SECONDS:
        return False
    return (any(checks.get(key) is False for key in (
        "candle_freshness", "market_alignment", "market_open", "quote_freshness", "orderbook", "archive"))
        or checks.get("typesafe") is False)


def run_validation(state_file: str) -> dict[str, Any]:
    """One offline report, no network/API calls, no live model or gate changes."""
    state_path = Path(state_file).resolve()
    full_report, summary_report = validation_paths(state_path)
    script = Path(__file__).with_name("validation.py")
    command = [sys.executable, str(script), "--archive-file", str(archive_path(state_path)),
               "--output-json", str(full_report), "--summary-json", str(summary_report)]
    try:
        result = subprocess.run(command, cwd=script.parent, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=120, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return {"status": "error", "reason": "Offline validation could not complete; no validation claim."}
    return {"status": "ok" if result.returncode == 0 else "error", "exit_code": result.returncode,
            "report_file": str(full_report), "summary_file": str(summary_report)}


def append_log(path: Path, entry: dict[str, Any]) -> None:
    """Bound heartbeat telemetry to roughly 20 MiB, including three archives."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    try:
        handler.emit(logging.LogRecord(
            "btc15m.monitor", logging.INFO, __file__, 0,
            json.dumps(entry, sort_keys=True), (), None,
        ))
    finally:
        handler.close()


def send_discord_message(webhook_url: str, content: str, timeout: int = 10) -> str:
    """Require Discord's saved-message acknowledgement; never log the secret URL."""
    url = urllib.parse.urlsplit(webhook_url)
    if (url.scheme != "https" or url.hostname not in {
            "discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com"}
            or url.username or url.port not in (None, 443)
            or not re.fullmatch(r"/api/(?:v\d+/)?webhooks/\d+/[^/]+", url.path)):
        raise ValueError("A Discord HTTPS webhook URL is required")
    query = [(key, value) for key, value in urllib.parse.parse_qsl(url.query) if key != "wait"]
    target = url._replace(query=urllib.parse.urlencode(query + [("wait", "true")])).geturl()
    request = urllib.request.Request(
        target, method="POST",
        data=json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "kalshi-milestone/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    message_id = payload.get("id") if isinstance(payload, dict) else None
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Discord did not confirm a saved message")
    return message_id


def check_milestone_alert(state_file: str, dashboard_url: str) -> dict[str, Any]:
    """Notify once per run at 100 distinct qualifying outcomes, even if gates still block."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return {"status": "disabled"}
    state_path = Path(state_file).resolve()
    marker = state_path.with_name(f"{state_path.stem}_milestone_alerts.json")
    temporary_path = None
    try:
        with state_transaction(state_path):
            _, state = load_state(state_path)
            validation = official_validation(state)
            state_id = state.get("state_id")
        samples = validation["samples"]
        if samples < MILESTONE_TARGET:
            return {"status": "waiting", "samples": samples, "target": MILESTONE_TARGET}
        if not isinstance(state_id, str) or not state_id:
            raise ValueError("Milestone evidence needs a durable run identity")
        with state_transaction(marker):
            record = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {"sent": {}}
            if state_id in record["sent"]:
                return {"status": "already_sent", "samples": samples, "target": MILESTONE_TARGET}
            readiness = "ready" if validation["ready"] else "not ready; performance checks still block"
            message = (
                f"BTC validation milestone reached: {samples} qualifying official outcomes "
                f"(target {MILESTONE_TARGET}).\n"
                f"Rolling accuracy: {validation['accuracy']:.1%}. "
                f"Brier: {validation['brier']:.3f} (50% baseline: 0.250).\n"
                f"Validation: {readiness}. Sample completion does not establish profitability.\n"
                f"Dashboard: {dashboard_url}"
            )
            message_id = send_discord_message(webhook_url, message)
            record["sent"][state_id] = {"samples": samples, "target": MILESTONE_TARGET,
                                        "sent_at": utc_now(), "message_id": message_id}
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=marker.parent, delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(record, temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, marker)
            return {"status": "sent", "samples": samples, "target": MILESTONE_TARGET,
                    "message_id": message_id}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"status": "error", "reason": f"Discord delivery failed (HTTP {exc.code}); will retry."}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "error", "reason": "Milestone notification not confirmed; will retry."}
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def check_validation_ready_alert(state_file: str, dashboard_url: str) -> dict[str, Any]:
    """Notify once per run when the official accuracy and Brier gates are ready."""
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return {"status": "disabled"}
    state_path = Path(state_file).resolve()
    marker = state_path.with_name(f"{state_path.stem}_milestone_alerts.json")
    temporary_path = None
    try:
        with state_transaction(state_path):
            _, state = load_state(state_path)
            validation = official_validation(state)
            state_id = state.get("state_id")
        samples = validation["samples"]
        result = {"status": "waiting", "samples": samples, "target": MILESTONE_TARGET,
                  "accuracy": validation.get("accuracy"), "brier": validation.get("brier")}
        if samples < MILESTONE_TARGET or not validation["ready"]:
            return result
        if not isinstance(state_id, str) or not state_id:
            raise ValueError("Validation evidence needs a durable run identity")
        with state_transaction(marker):
            record = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {"sent": {}}
            sent = record.get("sent")
            if not isinstance(sent, dict):
                raise ValueError("Milestone alert record is malformed")
            sent_for_run = sent.get(state_id)
            if isinstance(sent_for_run, dict) and sent_for_run.get("validation_ready"):
                return {**result, "status": "already_sent"}
            message = (
                f"BTC validation gate is ready: {samples} qualifying official outcomes.\n"
                f"Rolling accuracy: {validation['accuracy']:.1%}. "
                f"Brier: {validation['brier']:.3f} (50% baseline: 0.250).\n"
                "The evidence gate passed; individual paper-trade checks still apply.\n"
                f"Dashboard: {dashboard_url}"
            )
            message_id = send_discord_message(webhook_url, message)
            if sent_for_run is None:
                sent_for_run = {}
                sent[state_id] = sent_for_run
            if not isinstance(sent_for_run, dict):
                raise ValueError("Milestone alert record is malformed")
            sent_for_run["validation_ready"] = {
                "samples": samples, "accuracy": validation["accuracy"],
                "brier": validation["brier"], "sent_at": utc_now(), "message_id": message_id,
            }
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=marker.parent, delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(record, temporary, indent=2, sort_keys=True)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, marker)
            return {**result, "status": "sent", "message_id": message_id}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {"status": "error", "reason": f"Discord delivery failed (HTTP {exc.code}); will retry."}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "error", "reason": "Validation notification not confirmed; will retry."}
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def run_session(args: argparse.Namespace, stop: threading.Event) -> int:
    log_path = Path(args.log_file)
    end_monotonic = None if args.forever else time.monotonic() + args.hours * 3600
    last_prediction_cycle: int | None = initial_prediction_cycle()
    last_validation_day: int | None = None
    prediction_count = 0
    duration_label = "indefinitely" if args.forever else f"for {args.hours:g} hours"
    print(f"Live paper session started {duration_label}. No real orders will be placed.", flush=True)
    append_log(log_path, {"type": "session_started", "started_at": utc_now(), "forever": args.forever})

    while not stop.is_set() and (end_monotonic is None or time.monotonic() < end_monotonic):
        checked_at = utc_now()
        healthy, health_detail = check_dashboard_health(args.dashboard_url)
        current_cycle, _ = current_prediction_cycle()
        entry: dict[str, Any] = {
            "type": "heartbeat",
            "checked_at": checked_at,
            "dashboard_healthy": healthy,
            "health_detail": health_detail,
            "prediction_due": current_cycle != last_prediction_cycle,
        }
        if current_cycle != last_prediction_cycle:
            prediction_count += 1
            result = run_prediction(args.state_file, args.lookback_minutes, args.kalshi_series)
            entry["type"] = "prediction"
            entry["prediction_run"] = result
            payload = result.get("result")
            payload = payload if isinstance(payload, dict) else {}
            prediction = payload.get("prediction")
            prediction = prediction if isinstance(prediction, dict) else {}
            analysis = prediction.get("analysis")
            analysis = analysis if isinstance(analysis, dict) else {}
            account = payload.get("demo_account")
            account = account if isinstance(account, dict) else {}
            completed_cycle = int(time.time()) // INTERVAL_SECONDS
            expected_bar = (completed_cycle - 1) * INTERVAL_SECONDS
            successful = result.get("exit_code") == 0 and payload.get("status") == "predicted"
            fresh = successful and prediction.get("bar_timestamp") == expected_bar
            issued = prediction.get("forecast_status") == "issued"
            entry["prediction_ok"] = successful and issued
            entry["prediction_fresh"] = fresh and issued
            entry["forecast_status"] = prediction.get("forecast_status")
            entry["forecast_issued"] = issued
            entry["expected_bar_timestamp"] = expected_bar
            # Fresh candles alone do not imply a usable entry or a settled learner.
            retry = fresh and recommendation_needs_retry(prediction, time.time())
            entry["recommendation_retry_requested"] = retry
            if fresh and not retry:
                last_prediction_cycle = completed_cycle
            validation_day = int(time.time() // 86400)
            if fresh and issued and (prediction.get("archive") or {}).get("status") == "ok" and validation_day != last_validation_day:
                audit = run_validation(args.state_file)
                entry["offline_validation"] = audit
                if audit["status"] == "ok":
                    last_validation_day = validation_day
            up = prediction.get("probability_up")
            confidence = (
                (up if prediction.get("direction") == "UP" else 1.0 - up)
                if isinstance(up, (int, float)) else None
            )
            print(
                f"[{checked_at}] dashboard={'OK' if healthy else 'DOWN'} "
                f"prediction={'OK' if fresh and issued else 'WAIT' if fresh else 'RETRY'} "
                f"forecast={prediction.get('forecast_status', 'unknown')} "
                f"signal={prediction.get('trade_signal', 'UNKNOWN')} "
                f"direction={prediction.get('direction', 'UNKNOWN')} "
                f"confidence={confidence} analysis={analysis.get('status', 'UNKNOWN')} "
                f"suggestion={analysis.get('suggestion', 'WAIT')} "
                f"balance=${account.get('balance', 'n/a')} "
                f"trades={account.get('trades', 'n/a')}", flush=True,
            )
        elif not healthy:
            print(f"[{checked_at}] dashboard=DOWN: {health_detail}", flush=True)

        entry["milestone_alert"] = check_milestone_alert(
            args.state_file, os.environ.get("MILESTONE_DASHBOARD_URL", args.dashboard_url),
        )
        if entry["milestone_alert"]["status"] in {"sent", "error"}:
            print(f"[{checked_at}] milestone={entry['milestone_alert']['status']}", flush=True)
        entry["validation_alert"] = check_validation_ready_alert(
            args.state_file, os.environ.get("MILESTONE_DASHBOARD_URL", args.dashboard_url),
        )
        if entry["validation_alert"]["status"] in {"sent", "error"}:
            print(f"[{checked_at}] validation={entry['validation_alert']['status']}", flush=True)
        append_log(log_path, entry)
        wait_seconds = args.poll_seconds
        if end_monotonic is not None:
            wait_seconds = min(wait_seconds, max(0.0, end_monotonic - time.monotonic()))
        stop.wait(wait_seconds)

    append_log(log_path, {
        "type": "session_stopped" if stop.is_set() else "session_complete",
        "completed_at": utc_now(), "prediction_runs": prediction_count,
    })
    print(f"Live paper session stopped; prediction runs: {prediction_count}; log: {log_path}", flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    duration = parser.add_mutually_exclusive_group()
    duration.add_argument("--hours", type=float, default=3.0)
    duration.add_argument("--forever", action="store_true", help="run until stopped by the service manager")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--dashboard-url", default="http://127.0.0.1:8765")
    parser.add_argument("--state-file", default="data/btc_15m_state.json")
    parser.add_argument("--log-file", default="data/live_session.jsonl")
    parser.add_argument("--lookback-minutes", type=int, default=24 * 60)
    parser.add_argument("--kalshi-series", default="KXBTC15M")
    args = parser.parse_args()
    if args.poll_seconds <= 0 or (not args.forever and (not math.isfinite(args.hours) or args.hours <= 0)):
        parser.error("poll-seconds and hours must be finite and positive")

    stop = threading.Event()
    old_handlers = {sig: signal.signal(sig, lambda *_: stop.set()) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        return run_session(args, stop)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    raise SystemExit(main())
