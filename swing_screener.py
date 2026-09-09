"""
Assembles candidates from SEC, insider, Yahoo movers, technical, and news signals.

Pipeline:
  1. Primary: SEC 8-K catalysts + Form 4 insider buys
  2. Fallback: Yahoo Finance day gainers (if primary < 5 candidates)
  3. Enrich each candidate: yfinance technical + Yahoo news
  4. Return list of SwingCandidates for scoring
"""
import logging
import time

from swing_config import MIN_PRICE, MAX_PRICE, MIN_AVG_VOLUME
from swing_data import (
    get_sec_catalysts, get_insider_buys, get_yahoo_movers,
    get_technical_signal, get_yahoo_news, analyze_with_claude,
    SecFiling,
)
from swing_scorer import SwingCandidate

log = logging.getLogger(__name__)

# Tickers that appear in movers but are not real single stocks
_SKIP_TICKERS = {
    "BTC-USD", "ETH-USD", "USDT-USD", "BNB-USD", "SOL-USD",
    "^GSPC", "^DJI", "^IXIC", "^VIX",
}


def run_swing_screen() -> list[SwingCandidate]:
    # --- Primary: SEC signals ---
    log.info("Fetching SEC 8-K catalysts...")
    sec_filings = get_sec_catalysts()

    log.info("Fetching insider buys (Form 4)...")
    insider_buys = get_insider_buys()

    primary_tickers = set(sec_filings.keys()) | set(insider_buys.keys())
    log.info("Primary pool (SEC + insider): %d tickers", len(primary_tickers))

    # --- Fallback: Yahoo movers ---
    fallback_tickers: set[str] = set()
    if len(primary_tickers) < 5:
        log.info("Primary pool small — adding Yahoo Finance movers as fallback")
        movers = get_yahoo_movers(count=30)
        fallback_tickers = {t for t in movers if t not in _SKIP_TICKERS}
        log.info("Fallback pool: %d tickers from Yahoo movers", len(fallback_tickers))

    all_tickers = primary_tickers | fallback_tickers
    log.info("Total candidate pool: %d tickers", len(all_tickers))

    if not all_tickers:
        log.warning("No tickers found from any source — check network access")
        return []

    # --- Enrich each candidate ---
    candidates: list[SwingCandidate] = []
    for ticker in sorted(all_tickers):
        if "-" in ticker or "^" in ticker:
            continue

        technical = get_technical_signal(ticker)
        if technical:
            if not (MIN_PRICE <= technical.price <= MAX_PRICE):
                log.debug("Skip %s: price $%.2f out of range", ticker, technical.price)
                continue
            if technical.avg_volume < MIN_AVG_VOLUME:
                log.debug("Skip %s: avg_vol %d too low", ticker, technical.avg_volume)
                continue

        sec_f = sec_filings.get(ticker)
        company = sec_f.company if sec_f else (
            technical.sector if technical else ticker
        )

        news = get_yahoo_news(ticker)

        if news and sec_f and USE_CLAUDE_API_check():
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
        log.debug("Added candidate %s (score preview: sec=%s, insider=%d, news=%s)",
                  ticker,
                  bool(sec_f),
                  len(insider_buys.get(ticker, [])),
                  bool(news))

        time.sleep(0.2)

    log.info("Enriched %d candidates", len(candidates))
    return candidates


def USE_CLAUDE_API_check() -> bool:
    from swing_config import USE_CLAUDE_API
    return USE_CLAUDE_API
