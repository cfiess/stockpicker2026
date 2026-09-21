#!/usr/bin/env python3
"""
Swing Trading Screener  (2–14 day holds)
========================================
Usage:
  python swing_main.py              # run once, send email
  python swing_main.py --dry-run    # print to terminal, no email
  python swing_main.py --picks 5    # up to 5 picks
  python swing_main.py --schedule   # run daily at 7:30 AM ET
  python swing_main.py --verbose    # debug logging
"""
import argparse
import logging
import sys
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def run_job(n_picks: int = 3, dry_run: bool = False) -> None:
    from swing_data import get_market_regime
    from swing_screener import run_swing_screen, append_to_log
    from swing_scorer import rank_candidates
    from swing_email import send_swing_email

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M ET")
    run_date = datetime.now().strftime("%Y-%m-%d")

    log.info("=== Swing screener starting — %s ===", generated_at)

    # Market regime first (fast)
    log.info("Fetching market regime...")
    regime = get_market_regime()
    if regime:
        log.info("Regime: %s", regime.label)

    # Run screener (returns all candidates including excluded)
    all_candidates = run_swing_screen(regime=regime)

    # Rank and separate
    picks, near_misses = rank_candidates(all_candidates, n=n_picks)
    excluded = [c for c in all_candidates if c.excluded]

    log.info(
        "Results: %d picks | %d near-misses | %d excluded",
        len(picks), len(near_misses), len(excluded),
    )

    # Log to CSV (all candidates for future backtesting)
    append_to_log(all_candidates, run_date)

    # Send email
    send_swing_email(
        picks=picks,
        near_misses=near_misses,
        excluded=excluded,
        regime=regime,
        generated_at=generated_at,
        dry_run=dry_run,
    )

    log.info("Done.")


def _schedule(run_hour: int, run_minute: int, n_picks: int, dry_run: bool) -> None:
    import schedule
    import time

    def job():
        try:
            run_job(n_picks=n_picks, dry_run=dry_run)
        except Exception as exc:
            log.exception("Scheduled run failed: %s", exc)

    for day in ("monday", "tuesday", "wednesday", "thursday", "friday"):
        getattr(schedule.every(), day).at(f"{run_hour:02d}:{run_minute:02d}").do(job)

    log.info("Scheduler armed: %02d:%02d ET weekdays", run_hour, run_minute)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing trading screener")
    parser.add_argument("--schedule", action="store_true")
    parser.add_argument("--picks", type=int, default=3)
    parser.add_argument("--hour", type=int, default=7)
    parser.add_argument("--minute", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.schedule:
        _schedule(args.hour, args.minute, args.picks, args.dry_run)
    else:
        run_job(n_picks=args.picks, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
