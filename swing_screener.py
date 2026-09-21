"""
Pipeline: collect candidates → apply hard filters → return for scoring.

Hard filters (any one → excluded with reason):
  1. Routine 8-K filer (same item 3+ times in 6 weeks)
  2. Only routine items (9.01, 5.03, 7.01 — no real catalyst)
  3. Merger target (keyword detection + price near deal)
  4. Non-equity instrument (ETF, preferred, warrant, SPAC, trust)
  5. Price out of range ($5–$500)
  6. Dollar volume < $5M/day
  7. ATR(14) < 2% (not enough volatility for a swing)
  8. Already moved: 5d gain > 15% or 20d gain > 25%
  9. Risk/reward < 2:1
 10. Earnings inside hold window (unless earnings IS the catalyst)
 11. Re-flagged within 10 trading days with no new catalyst

Fallback: if SEC yields < 5 candidates, supplement with Yahoo movers.
"""
import csv
import logging
import os
import time
from datetime import datetime, timedelta

from swing_config import (
    MIN_PRICE, MAX_PRICE, MIN_AVG_DOLLAR_VOLUME, MIN_ATR_PCT,
    MAX_5D_GAIN_PCT, MAX_20D_GAIN_PCT,
    MIN_RR, FRESHNESS_TRADING_DAYS, ROUTINE_ITEMS,
    BAD_REGIME_SCORE_BUMP, MIN_SCORE,
)
from swing_data import (
    get_sec_catalysts, get_insider_buys, get_yahoo_movers,
    get_technical_signal, get_yahoo_news, analyze_with_claude,
    is_merger_target, is_near_deal_price,
    MarketRegime, TechnicalSignal,
    EXCLUDED_NAME_RE, EXCLUDED_QUOTE_TYPES,
)
from swing_scorer import SwingCandidate

log = logging.getLogger(__name__)

PICKS_LOG = "picks_log.csv"

_SKIP_TICKERS = {
    "BTC-USD", "ETH-USD", "^GSPC", "^DJI", "^IXIC", "^VIX", "^RUT",
}


# ---------------------------------------------------------------------------
# Recent picks log — freshness check
# ---------------------------------------------------------------------------

def _load_recent_picks(trading_days: int = FRESHNESS_TRADING_DAYS) -> set[str]:
    """Return tickers picked in the last `trading_days` calendar days (~2 weeks)."""
    if not os.path.exists(PICKS_LOG):
        return set()
    cutoff = datetime.now() - timedelta(days=trading_days * 1.5)
    seen: set[str] = set()
    try:
        with open(PICKS_LOG, newline="") as f:
            for row in csv.DictReader(f):
                try:
                    d = datetime.strptime(row["date"], "%Y-%m-%d")
                    if d >= cutoff and row.get("excluded") != "1":
                        seen.add(row["ticker"])
                except (ValueError, KeyError):
                    continue
    except Exception as exc:
        log.debug("picks_log read: %s", exc)
    return seen


def append_to_log(candidates: list[SwingCandidate], run_date: str) -> None:
    """Append all candidates (picks + near-misses + excluded) to picks_log.csv."""
    write_header = not os.path.exists(PICKS_LOG)
    try:
        with open(PICKS_LOG, "a", newline="") as f:
            fieldnames = [
                "date", "ticker", "company", "score", "rank",
                "catalyst", "items", "price", "stop", "target", "rr",
                "atr_pct", "mom5d", "mom20d", "above_ma20",
                "earnings_date", "excluded", "exclude_reason",
                "ret_2d", "ret_5d", "ret_10d",   # filled in later
            ]
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            if write_header:
                w.writeheader()
            for c in candidates:
                t = c.technical
                sf = c.sec_filing
                w.writerow({
                    "date": run_date,
                    "ticker": c.ticker,
                    "company": c.company,
                    "score": c.score,
                    "rank": c.rank,
                    "catalyst": sf.catalyst_type if sf else "",
                    "items": ",".join(sf.items) if sf else "",
                    "price": t.price if t else "",
                    "stop": t.stop_price if t else "",
                    "target": t.target_price if t else "",
                    "rr": t.rr_ratio if t else "",
                    "atr_pct": t.atr_pct if t else "",
                    "mom5d": t.momentum_5d if t else "",
                    "mom20d": t.momentum_20d if t else "",
                    "above_ma20": (1 if t and t.above_ma20 else 0) if t else "",
                    "earnings_date": t.earnings_date if t else "",
                    "excluded": 1 if c.excluded else 0,
                    "exclude_reason": c.exclude_reason,
                    "ret_2d": "", "ret_5d": "", "ret_10d": "",
                })
    except Exception as exc:
        log.warning("picks_log write: %s", exc)


# ---------------------------------------------------------------------------
# Hard filter helpers
# ---------------------------------------------------------------------------

def _is_only_routine_items(items: list[str]) -> bool:
    """True if ALL items are routine (no real catalyst)."""
    if not items:
        return False
    return all(item in ROUTINE_ITEMS for item in items)


def _is_excluded_instrument(t: TechnicalSignal) -> tuple[bool, str]:
    if t.quote_type in EXCLUDED_QUOTE_TYPES:
        return True, f"non-equity instrument ({t.quote_type})"
    if EXCLUDED_NAME_RE.search(t.long_name):
        return True, f"excluded by name pattern ({t.long_name[:40]})"
    return False, ""


def _earnings_in_window(earnings_date: str, hold_days: int = 14) -> bool:
    try:
        ed = datetime.strptime(earnings_date[:10], "%Y-%m-%d")
        days_away = (ed - datetime.now()).days
        return 0 <= days_away <= hold_days
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Main screener
# ---------------------------------------------------------------------------

def run_swing_screen(regime: MarketRegime = None) -> list[SwingCandidate]:
    recent_picks = _load_recent_picks()
    effective_min_score = MIN_SCORE + (BAD_REGIME_SCORE_BUMP if regime and regime.is_bad else 0)
    if regime and regime.is_bad:
        log.info("Bad market regime — min score raised to %.1f", effective_min_score)

    # --- Primary: SEC 8-K + insider ---
    log.info("Fetching SEC 8-K catalysts...")
    sec_filings = get_sec_catalysts()
    log.info("Fetching insider buys...")
    insider_buys = get_insider_buys()

    primary = set(sec_filings.keys()) | set(insider_buys.keys())
    log.info("Primary pool: %d tickers", len(primary))

    # --- Fallback: Yahoo movers ---
    fallback: set[str] = set()
    if len(primary) < 5:
        log.info("Small primary pool — adding Yahoo movers fallback")
        movers = get_yahoo_movers(30)
        fallback = {t for t in movers if t not in _SKIP_TICKERS and "-" not in t and "^" not in t}
        log.info("Fallback: %d movers", len(fallback))

    all_tickers = primary | fallback
    log.info("Total candidates to evaluate: %d", len(all_tickers))

    candidates: list[SwingCandidate] = []

    for ticker in sorted(all_tickers):
        if "-" in ticker or "^" in ticker:
            continue

        sec_f = sec_filings.get(ticker)
        ibuys = insider_buys.get(ticker, [])
        company = sec_f.company if sec_f else ticker

        c = SwingCandidate(ticker=ticker, company=company, sec_filing=sec_f, insider_buys=ibuys)

        # --- Hard filter 1: routine filer ---
        if sec_f and sec_f.is_routine_filer:
            c.excluded = True
            c.exclude_reason = f"routine filer — {sec_f.routine_reason}"
            candidates.append(c)
            continue

        # --- Hard filter 2: only routine items ---
        if sec_f and _is_only_routine_items(sec_f.items):
            c.excluded = True
            c.exclude_reason = f"routine items only ({', '.join(sec_f.items)})"
            candidates.append(c)
            continue

        # --- Fetch news (needed for merger check) ---
        news = get_yahoo_news(ticker)
        c.news = news
        headlines = news.headlines if news else []

        # --- Hard filter 3: merger target ---
        if is_merger_target(ticker, headlines):
            c.excluded = True
            c.exclude_reason = "merger/acquisition target (keyword match)"
            candidates.append(c)
            continue

        # --- Technical data ---
        tech = get_technical_signal(ticker)
        c.technical = tech
        c.company = company if company != ticker else (tech.long_name if tech else ticker)

        if not tech:
            log.debug("No technical data for %s", ticker)
            # Keep candidate but mark as low quality (no tech = can't filter properly)
            candidates.append(c)
            continue

        # --- Hard filter 3b: near deal price ---
        if is_near_deal_price(tech.price, headlines):
            c.excluded = True
            c.exclude_reason = f"price ${tech.price:.2f} near cash deal price"
            candidates.append(c)
            continue

        # --- Hard filter 4: non-equity instrument ---
        is_excl, excl_reason = _is_excluded_instrument(tech)
        if is_excl:
            c.excluded = True
            c.exclude_reason = excl_reason
            candidates.append(c)
            continue

        # --- Hard filter 5: price range ---
        if not (MIN_PRICE <= tech.price <= MAX_PRICE):
            c.excluded = True
            c.exclude_reason = f"price ${tech.price:.2f} outside ${MIN_PRICE}–${MAX_PRICE}"
            candidates.append(c)
            continue

        # --- Hard filter 6: dollar volume ---
        if tech.avg_dollar_volume < MIN_AVG_DOLLAR_VOLUME:
            c.excluded = True
            c.exclude_reason = f"avg dollar vol ${tech.avg_dollar_volume/1e6:.1f}M < $5M"
            candidates.append(c)
            continue

        # --- Hard filter 7: ATR floor ---
        if tech.atr_pct < MIN_ATR_PCT:
            c.excluded = True
            c.exclude_reason = f"ATR {tech.atr_pct:.1f}% < {MIN_ATR_PCT}% (too flat)"
            candidates.append(c)
            continue

        # --- Hard filter 8: already moved ---
        if tech.momentum_5d > MAX_5D_GAIN_PCT:
            c.excluded = True
            c.exclude_reason = f"already up {tech.momentum_5d:.1f}% in 5 days"
            candidates.append(c)
            continue
        if tech.momentum_20d > MAX_20D_GAIN_PCT:
            c.excluded = True
            c.exclude_reason = f"already up {tech.momentum_20d:.1f}% in 20 days"
            candidates.append(c)
            continue

        # --- Hard filter 9: R:R ---
        if tech.rr_ratio < MIN_RR:
            c.excluded = True
            c.exclude_reason = f"R:R {tech.rr_ratio:.1f}:1 < {MIN_RR}:1 required"
            candidates.append(c)
            continue

        # --- Hard filter 10: earnings in hold window ---
        earnings_is_catalyst = sec_f and "Earnings" in sec_f.catalyst_type
        if (tech.earnings_date and
                _earnings_in_window(tech.earnings_date) and
                not earnings_is_catalyst):
            c.excluded = True
            c.exclude_reason = f"earnings {tech.earnings_date} inside 14-day hold window"
            candidates.append(c)
            continue

        # --- Hard filter 11: freshness ---
        if ticker in recent_picks and not sec_f:
            c.excluded = True
            c.exclude_reason = f"picked recently (within {FRESHNESS_TRADING_DAYS} trading days)"
            candidates.append(c)
            continue

        # --- Claude narrative (optional) ---
        if news and sec_f and USE_CLAUDE_API_flag():
            score, thesis = analyze_with_claude(
                ticker, c.company, headlines, sec_f.catalyst_type
            )
            news.sentiment_score = score
            news.thesis = thesis

        candidates.append(c)
        time.sleep(0.2)

    log.info(
        "Screener done: %d total | %d passed filters | %d excluded",
        len(candidates),
        sum(1 for c in candidates if not c.excluded),
        sum(1 for c in candidates if c.excluded),
    )
    return candidates


def USE_CLAUDE_API_flag() -> bool:
    from swing_config import USE_CLAUDE_API
    return USE_CLAUDE_API
