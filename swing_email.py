"""
Email delivery for swing trading picks.
"""
import smtplib
import logging
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from swing_config import EMAIL_FROM, EMAIL_TO, GMAIL_APP_PASSWORD
from swing_scorer import SwingCandidate

log = logging.getLogger(__name__)

CATALYST_COLORS = {
    "M&A": "#8B5CF6",
    "Earnings": "#10B981",
    "Partnership": "#3B82F6",
    "FDA/Regulatory": "#F59E0B",
    "Capital Return": "#06B6D4",
    "Guidance": "#6366F1",
    "Corporate Event": "#64748B",
}


def _card_color(catalyst: str) -> str:
    return CATALYST_COLORS.get(catalyst, "#64748B")


def build_html(picks: list[SwingCandidate], generated_at: str) -> str:
    cards = ""
    for c in picks:
        color = _card_color(c.sec_filing.catalyst_type if c.sec_filing else "Corporate Event")
        tech_html = ""
        if c.technical:
            t = c.technical
            trend = "↑ Above 20-MA" if t.above_ma20 else "↓ Below 20-MA"
            tech_html = (
                f"<p style='margin:4px 0;font-size:13px;color:#94a3b8;'>"
                f"${t.price:.2f} | {trend} | {t.rel_volume:.1f}× vol | "
                f"{t.momentum_5d:+.1f}% 5d | {t.sector}</p>"
            )

        insider_html = ""
        if c.insider_buys:
            b = c.insider_buys[0]
            insider_html = (
                f"<p style='margin:4px 0;font-size:13px;color:#34d399;'>"
                f"Insider buy: {b.insider_name} — {b.shares:,} sh @ ${b.price:.2f}</p>"
            )

        thesis_html = ""
        if c.thesis:
            thesis_html = (
                f"<p style='margin:6px 0;font-size:13px;color:#e2e8f0;"
                f"border-left:3px solid {color};padding-left:8px;'>{c.thesis}</p>"
            )

        cards += f"""
<div style="background:#1e293b;border-radius:12px;padding:20px;margin:16px 0;
            border-left:5px solid {color};">
  <div style="display:flex;justify-content:space-between;align-items:center;">
    <span style="font-size:22px;font-weight:700;color:#f1f5f9;">${c.ticker}</span>
    <span style="background:{color};color:white;padding:3px 10px;border-radius:20px;
                 font-size:11px;font-weight:600;">#{c.rank} | Score {c.score:.1f}</span>
  </div>
  <p style="margin:4px 0;font-size:15px;color:#94a3b8;">{c.company}</p>
  {tech_html}
  {insider_html}
  <p style="margin:4px 0;font-size:13px;color:#94a3b8;">{c.reason}</p>
  {thesis_html}
</div>"""

    disclaimer = (
        "<p style='font-size:11px;color:#64748b;margin-top:24px;"
        "border-top:1px solid #334155;padding-top:12px;'>"
        "Educational only. Not financial advice. Do your own research before trading.</p>"
    )

    return f"""<!DOCTYPE html>
<html><body style="background:#0f172a;font-family:-apple-system,sans-serif;padding:24px;max-width:640px;margin:auto;">
<h2 style="color:#f1f5f9;margin:0 0 4px;">🔭 Swing Trade Picks</h2>
<p style="color:#64748b;margin:0 0 20px;font-size:13px;">2–14 day hold horizon · Generated {generated_at}</p>
{cards}
{disclaimer}
</body></html>"""


def build_plain(picks: list[SwingCandidate], generated_at: str) -> str:
    lines = [f"SWING TRADE PICKS — {generated_at}", "=" * 50, ""]
    for c in picks:
        lines.append(f"#{c.rank}  ${c.ticker}  ({c.company})  Score: {c.score:.1f}")
        if c.technical:
            t = c.technical
            trend = "Above" if t.above_ma20 else "Below"
            lines.append(f"   ${t.price:.2f} | {trend} 20-MA | {t.rel_volume:.1f}× vol | {t.momentum_5d:+.1f}% 5d")
        if c.insider_buys:
            b = c.insider_buys[0]
            lines.append(f"   Insider: {b.insider_name} bought {b.shares:,} @ ${b.price:.2f}")
        lines.append(f"   {c.reason}")
        if c.thesis:
            lines.append(f"   Thesis: {c.thesis}")
        lines.append("")
    lines.append("Educational only. Not financial advice.")
    return "\n".join(lines)


def send_swing_email(picks: list[SwingCandidate], generated_at: str, dry_run: bool = False) -> None:
    if dry_run:
        print(build_plain(picks, generated_at))
        log.info("[dry-run] email not sent")
        return

    if not EMAIL_FROM or not GMAIL_APP_PASSWORD:
        log.warning("GMAIL_USER / GMAIL_APP_PASSWORD not set; skipping email")
        return

    subject = f"Swing Trade Picks — {generated_at}"
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg.attach(MIMEText(build_plain(picks, generated_at), "plain"))
    msg.attach(MIMEText(build_html(picks, generated_at), "html"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.login(EMAIL_FROM, GMAIL_APP_PASSWORD)
            smtp.sendmail(EMAIL_FROM, EMAIL_TO, msg.as_string())
        log.info("Swing email sent to %s", EMAIL_TO)
    except Exception as exc:
        log.error("Failed to send email: %s", exc)
