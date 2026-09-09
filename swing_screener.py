"""
Assembles candidates from SEC, insider, technical, and news signals.
"""
import logging
import time

from swing_config import MIN_PRICE, MAX_PRICE, MIN_AVG_VOLUME
from swing_data import (
    get_sec_catalysts, get_insider_buys, get_technical_signal,
    get_yahoo_news, analyze_with_claude,
)
from swing_scorer import SwingCandidate

log = logging.getLogger(__name__)


def run_swing_screen() -> list[SwingCandidate]:
    """
    Collect all candidate tickers from SEC 8-K and insider Form 4,
    enrich with technical + news signals, return list of SwingCandidates.
    """
    log.info("=== Swing Screener: fetching SEC catalysts ===")
    sec_filings = get_sec_catalysts()

    log.info("=== Swing Screener: fetching insider buys ===")
    insider_buys = get_insider_buys()

    # Union of all tickers
    all_tickers = set(sec_filings.keys()) | set(insider_buys.keys())
    log.info("Candidate pool: %d tickers", len(all_tickers))

    candidates: list[SwingCandidate] = []
    for ticker in sorted(all_tickers):
        log.debug("Enriching %s", ticker)

        # Technical filter — skip if price or volume out of range
        technical = get_technical_signal(ticker)
        if technical:
            if not (MIN_PRICE <= technical.price <= MAX_PRICE):
                log.debug("Skip %s: price $%.2f out of range", ticker, technical.price)
                continue
            if technical.avg_volume < MIN_AVG_VOLUME:
                log.debug("Skip %s: avg_vol %d too low", ticker, technical.avg_volume)
                continue

        sec_f = sec_filings.get(ticker)
        company = (sec_f.company if sec_f else ticker)

        news = get_yahoo_news(ticker)

        # Claude narrative analysis (only if API key present)
        if news and sec_f:
            claude_score, thesis = analyze_with_claude(
                ticker, company, news.headlines, sec_f.catalyst_type
            )
            news.sentiment_score = claude_score
            news.thesis = thesis

        c = SwingCandidate(
            ticker=ticker,
            company=company,
            sec_filing=sec_f,
            insider_buys=insider_buys.get(ticker, []),
            technical=technical,
            news=news,
        )
        candidates.append(c)

        time.sleep(0.3)  # polite pacing

    log.info("Enriched %d candidates", len(candidates))
    return candidates
