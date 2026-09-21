"""
Plain-text email for swing trade picks.
Formatted for easy reading at a glance.
"""
import logging
import smtplib
from email.mime.text import MIMEText
from typing import Optional

from swing_config import EMAIL_FROM, EMAIL_TO, GMAIL_APP_PASSWORD, MIN_SCORE
from swing_data import MarketRegime
from swing_scorer import SwingCandidate

log = logging.getLogger(__name__)

_LINE = "=" * 56
_THIN = "-" * 56


def build_email(
    picks: list[SwingCandidate],
    near_misses: list[SwingCandidate],
    excluded: list[SwingCandidate],
    regime: Optional[MarketRegime],
    generated_at: str,
) -> str:
    lines: list[str] = []

    lines += [
        _LINE,
        "  SWING TRADE PICKS",
        f"  {generated_at}  |  Horizon: 2–14 days",
        _LINE,
        "",
    ]

    # Market regime
    if regime:
        flag = "  *** BAD REGIME — higher bar applied ***" if regime.is_bad else ""
        lines += [
            "MARKET REGIME",
            f"  {regime.label}",
        ]
        if flag:
            lines.append(flag)
        lines.append("")

    # Picks
    if not picks:
        lines += [
            f"NO QUALIFIED PICKS TODAY",
            f"(minimum score {MIN_SCORE:.0f} — nothing cleared the bar)",
            "",
        ]
    else:
        for c in picks:
            t = c.technical
            sf = c.sec_filing

            lines += [
                _THIN,
                f"  #{c.rank}  {c.ticker}  —  {c.company}",
                f"  Score: {c.score:.1f}  |  {sf.catalyst_type if sf else 'Signal'}",
            ]

            if sf:
                items_str = ", ".join(sf.items) if sf.items else "?"
                lines.append(f"  8-K Items: {items_str}  ({sf.filed_date})")

            if t:
                lines += [
                    "",
                    f"  Price:   ${t.price:.2f}",
                    f"  Stop:    ${t.stop_price:.2f}  (entry − 1.5×ATR)",
                    f"  Target:  ${t.target_price:.2f}",
                    f"  R:R:     {t.rr_ratio:.1f}:1",
                    f"  ATR:     {t.atr_pct:.1f}%  |  5d: {t.momentum_5d:+.1f}%  |  20d: {t.momentum_20d:+.1f}%",
                    f"  Volume:  {t.rel_volume:.1f}x 50-day avg",
                    f"  MA20:    {'above' if t.above_ma20 else 'BELOW'} ({t.pct_above_ma20:+.1f}%)",
                    f"  52w-Hi:  {t.pct_from_52w_high:+.1f}%  from ${t.high_52w:.2f}",
                ]
                if t.earnings_date:
                    lines.append(f"  Earnings:{t.earnings_date}")

            if c.thesis:
                lines += ["", f"  >> {c.thesis}"]

            if c.insider_buys:
                b = c.insider_buys[0]
                lines.append(f"  Insider: {b.insider_name} — {b.shares:,} sh @ ${b.price:.2f}")

            if c.news and c.news.headlines:
                lines += ["", "  News:"]
                for h in c.news.headlines[:2]:
                    lines.append(f"    • {h[:70]}")

            lines.append("")

    # Near-misses
    if near_misses:
        lines += [_THIN, "  NEAR-MISSES (scored but below threshold)", _THIN]
        for c in near_misses:
            t = c.technical
            lines.append(
                f"  {c.ticker:6s}  score {c.score:.1f}  "
                f"{'$'+str(round(t.price,2)) if t else ''}  "
                f"{c.sec_filing.catalyst_type if c.sec_filing else ''}"
            )
        lines.append("")

    # Exclusion log
    if excluded:
        lines += [_THIN, "  EXCLUDED TODAY", _THIN]
        for c in excluded:
            lines.append(f"  {c.ticker:6s}  {c.exclude_reason[:60]}")
        lines.append("")

    lines += [
        _LINE,
        "Educational only. Not financial advice. Do your own research.",
        _LINE,
    ]

    return "\n".join(lines)


def send_swing_email(
    picks: list[SwingCandidate],
    near_misses: list[SwingCandidate],
    excluded: list[SwingCandidate],
    regime: Optional[MarketRegime],
    generated_at: str,
    dry_run: bool = False,
) -> None:
    body = build_email(picks, near_misses, excluded, regime, generated_at)

    if dry_run:
        print(body)
        log.info("[dry-run] email not sent")
        return

    if not EMAIL_FROM or not GMAIL_APP_PASSWORD:
        log.warning("GMAIL_USER / GMAIL_APP_PASSWORD not set — skipping email")
        print(body)
        return

    n_picks = len(picks)
    subject = (
        f"Swing Picks — {n_picks} qualified — {generated_at}"
        if n_picks else
        f"Swing Picks — No candidates today — {generated_at}"
    )

    msg = MIMEText(body, "plain")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("Email sent to %s (%d picks)", EMAIL_TO, n_picks)
    except Exception as exc:
        log.error("Email failed: %s", exc)
        print(body)
