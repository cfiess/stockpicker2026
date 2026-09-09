#!/usr/bin/env python3
"""
Swing Trading Screener
======================
Screens for stocks with:
  - SEC 8-K corporate catalyst (last 14 days)
  - Insider buying (Form 4 purchases, last 14 days)
  - Technical momentum (price above 20-MA, relative volume)
  - News sentiment (Claude API narrative analysis when API key available)

Recommended hold: 2–14 days.

Usage:
  python swing_main.py                # run once, send email
  python swing_main.py --dry-run      # print to terminal only
  python swing_main.py --picks 5      # return top 5 picks
  python swing_main.py --schedule     # run daily at 7:30 AM ET
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
    from swing_screener import run_swing_screen
    from swing_scorer import rank_candidates
    from swing_email import send_swing_email, build_plain

    log.info("Starting swing screen…")
    candidates = run_swing_screen()

    if not candidates:
        log.warning("No candidates found.")
        if not dry_run:
            _send_no_picks_email()
        return

    picks = rank_candidates(candidates, n=n_picks)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M ET")

    # Always print to console
    print(build_plain(picks, generated_at))

    send_swing_email(picks, generated_at, dry_run=dry_run)
    log.info("Done. %d picks delivered.", len(picks))


def _send_no_picks_email() -> None:
    from swing_config import EMAIL_FROM, EMAIL_TO, GMAIL_APP_PASSWORD
    import smtplib
    from email.mime.text import MIMEText
    if not EMAIL_FROM or not GMAIL_APP_PASSWORD:
        return
    msg = MIMEText("No swing trade candidates found in today's scan.")
    msg["Subject"] = "Swing Trade Picks — No candidates today"
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo(); smtp.starttls()
            smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
    except Exception as exc:
        log.error("No-picks email failed: %s", exc)


def _schedule(run_hour: int, run_minute: int, n_picks: int, dry_run: bool) -> None:
    import schedule
    import time

    def job():
        try:
            run_job(n_picks=n_picks, dry_run=dry_run)
        except Exception as exc:
            log.exception("Scheduled run failed: %s", exc)

    schedule.every().monday.at(f"{run_hour:02d}:{run_minute:02d}").do(job)
    schedule.every().tuesday.at(f"{run_hour:02d}:{run_minute:02d}").do(job)
    schedule.every().wednesday.at(f"{run_hour:02d}:{run_minute:02d}").do(job)
    schedule.every().thursday.at(f"{run_hour:02d}:{run_minute:02d}").do(job)
    schedule.every().friday.at(f"{run_hour:02d}:{run_minute:02d}").do(job)

    log.info("Scheduler armed: %02d:%02d ET weekdays", run_hour, run_minute)
    while True:
        schedule.run_pending()
        time.sleep(30)


def main() -> None:
    parser = argparse.ArgumentParser(description="Swing trading screener")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument("--picks", type=int, default=3, help="Number of picks to return")
    parser.add_argument("--hour", type=int, default=7, help="Run hour (ET, 24h)")
    parser.add_argument("--minute", type=int, default=30, help="Run minute")
    parser.add_argument("--dry-run", action="store_true", help="Print only, no email")
    parser.add_argument("--verbose", action="store_true", help="Debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.schedule:
        _schedule(args.hour, args.minute, args.picks, args.dry_run)
    else:
        run_job(n_picks=args.picks, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
