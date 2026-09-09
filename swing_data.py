"""
Data sources for swing trading screener.

Pulls from:
  - SEC EDGAR 8-K feed   → corporate catalysts
  - SEC EDGAR Form 4     → insider transactions
  - Yahoo Finance        → price / technical data + news headlines
  - Anthropic Claude API → narrative sentiment analysis (optional)
"""
import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass, field
from typing import Optional

import requests
import yfinance as yf

from swing_config import (
    ANTHROPIC_API_KEY, SEC_LOOKBACK_DAYS, INSIDER_LOOKBACK_DAYS,
    NEWS_LOOKBACK_DAYS, TECH_LOOKBACK_DAYS, USE_CLAUDE_API,
)

log = logging.getLogger(__name__)

HEADERS = {"User-Agent": "swing-screener/1.0 contact@example.com"}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

NON_TICKERS = {
    "AI", "IT", "IS", "IN", "OR", "AND", "THE", "FOR", "ANY", "ARE", "NOT",
    "ALL", "NEW", "NOW", "OUR", "HAS", "ITS", "CAN", "MAY", "WAS", "HOW",
    "SEC", "CEO", "CFO", "COO", "CTO", "IPO", "ETF", "NYSE", "NASDAQ",
    "HTTPS", "HTTP", "LLC", "INC", "LTD", "FY", "Q1", "Q2", "Q3", "Q4",
    "EPS", "P/E", "PE", "YTD", "ML", "LLM", "GPT", "ESG", "NDA", "FDA",
    "USA", "GDP", "CPI", "FED", "USD", "EUR", "GBP", "JPY",
}

_TICKER_RE = re.compile(r'\b\$([A-Z]{1,5})\b|\b([A-Z]{2,5})\b')

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 10) -> Optional[requests.Response]:
    for attempt in range(3):
        try:
            resp = SESSION.get(url, timeout=timeout)
            if resp.status_code == 200:
                return resp
            log.debug("GET %s → %d", url, resp.status_code)
        except requests.RequestException as exc:
            log.debug("GET %s failed: %s", url, exc)
        time.sleep(1.5 ** attempt)
    return None


def _extract_tickers(text: str) -> set[str]:
    text = re.sub(r'https?://\S+', '', text)
    tickers = set()
    for m in _TICKER_RE.finditer(text):
        t = m.group(1) or m.group(2)
        if t and t not in NON_TICKERS and len(t) >= 2:
            tickers.add(t)
    return tickers


def _cik_to_ticker_map() -> dict[str, str]:
    resp = _get("https://www.sec.gov/files/company_tickers.json")
    if not resp:
        return {}
    data = resp.json()
    return {str(v["cik_str"]).zfill(10): v["ticker"].upper() for v in data.values()}


# ---------------------------------------------------------------------------
# SEC EDGAR 8-K catalysts
# ---------------------------------------------------------------------------

@dataclass
class SecFiling:
    ticker: str
    company: str
    catalyst_type: str
    filed_date: str
    cik: str


def get_sec_catalysts(days_back: int = SEC_LOOKBACK_DAYS) -> dict[str, SecFiling]:
    """Return {ticker: SecFiling} for 8-K filings in the last `days_back` days."""
    cik_map = _cik_to_ticker_map()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    resp = _get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=80&output=atom")
    if not resp:
        return {}

    results: dict[str, SecFiling] = {}
    entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)
    for entry in entries:
        updated_m = re.search(r'<updated>(.*?)</updated>', entry)
        if not updated_m:
            continue
        try:
            updated = datetime.fromisoformat(updated_m.group(1).replace('Z', '+00:00'))
        except ValueError:
            continue
        if updated < cutoff:
            continue

        title = re.search(r'<title>(.*?)</title>', entry)
        cik_m = re.search(r'CIK=(\d+)', entry)
        if not (title and cik_m):
            continue

        cik = cik_m.group(1).zfill(10)
        ticker = cik_map.get(cik)
        if not ticker:
            continue

        title_text = title.group(1).strip()
        catalyst = _classify_catalyst(title_text)
        company_m = re.match(r'^(.*?)\s*\(', title_text)
        company = company_m.group(1).strip() if company_m else title_text

        results[ticker] = SecFiling(
            ticker=ticker,
            company=company,
            catalyst_type=catalyst,
            filed_date=updated.strftime("%Y-%m-%d"),
            cik=cik,
        )

    log.info("SEC 8-K: found %d catalysts", len(results))
    return results


def _classify_catalyst(title: str) -> str:
    t = title.lower()
    if any(w in t for w in ["acqui", "merger", "takeover", "buyout"]):
        return "M&A"
    if any(w in t for w in ["earnings", "financial results", "quarterly results", "revenue", "eps"]):
        return "Earnings"
    if any(w in t for w in ["partnership", "agreement", "contract", "deal"]):
        return "Partnership"
    if any(w in t for w in ["fda", "approval", "trial", "clinical"]):
        return "FDA/Regulatory"
    if any(w in t for w in ["buyback", "dividend", "special distribution"]):
        return "Capital Return"
    if any(w in t for w in ["guidance", "outlook", "forecast"]):
        return "Guidance"
    return "Corporate Event"


# ---------------------------------------------------------------------------
# SEC Form 4 – insider transactions
# ---------------------------------------------------------------------------

@dataclass
class InsiderBuy:
    ticker: str
    company: str
    insider_name: str
    transaction_type: str  # "P" = purchase, "S" = sale
    shares: int
    price: float
    filed_date: str


def get_insider_buys(days_back: int = INSIDER_LOOKBACK_DAYS) -> dict[str, list[InsiderBuy]]:
    """Return {ticker: [InsiderBuy, ...]} for Form 4 PURCHASES in the last `days_back` days."""
    cik_map = _cik_to_ticker_map()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    resp = _get("https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=4&dateb=&owner=include&count=80&output=atom")
    if not resp:
        return {}

    results: dict[str, list[InsiderBuy]] = {}
    entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)

    for entry in entries:
        updated_m = re.search(r'<updated>(.*?)</updated>', entry)
        if not updated_m:
            continue
        try:
            updated = datetime.fromisoformat(updated_m.group(1).replace('Z', '+00:00'))
        except ValueError:
            continue
        if updated < cutoff:
            continue

        cik_m = re.search(r'CIK=(\d+)', entry)
        if not cik_m:
            continue
        cik = cik_m.group(1).zfill(10)
        ticker = cik_map.get(cik)
        if not ticker:
            continue

        # Fetch actual filing detail to get transaction type
        link_m = re.search(r'<link.*?href="(.*?)"', entry)
        if not link_m:
            continue

        filing = _get_form4_detail(link_m.group(1))
        if not filing:
            continue

        # Only track purchases (P = open market buy)
        if filing["transaction_type"] != "P":
            continue

        title_m = re.search(r'<title>(.*?)</title>', entry)
        company_m = re.search(r'for\s+(.*?)$', title_m.group(1)) if title_m else None
        company = company_m.group(1).strip() if company_m else ticker

        buy = InsiderBuy(
            ticker=ticker,
            company=company,
            insider_name=filing.get("insider_name", "Unknown"),
            transaction_type="P",
            shares=filing.get("shares", 0),
            price=filing.get("price", 0.0),
            filed_date=updated.strftime("%Y-%m-%d"),
        )
        results.setdefault(ticker, []).append(buy)

    log.info("SEC Form 4: found insider buys for %d tickers", len(results))
    return results


def _get_form4_detail(url: str) -> Optional[dict]:
    """Fetch Form 4 index page and parse transaction info."""
    resp = _get(url, timeout=8)
    if not resp:
        return None

    # Look for XML link in index
    xml_m = re.search(r'href="(/Archives/.*?\.xml)"', resp.text)
    if not xml_m:
        return None

    xml_resp = _get("https://www.sec.gov" + xml_m.group(1), timeout=8)
    if not xml_resp:
        return None

    xml = xml_resp.text
    tx_code = re.search(r'<transactionCode>(.*?)</transactionCode>', xml)
    shares_m = re.search(r'<transactionShares>.*?<value>(.*?)</value>', xml, re.DOTALL)
    price_m = re.search(r'<transactionPricePerShare>.*?<value>(.*?)</value>', xml, re.DOTALL)
    name_m = re.search(r'<rptOwnerName>(.*?)</rptOwnerName>', xml)

    return {
        "transaction_type": tx_code.group(1) if tx_code else "",
        "shares": int(float(shares_m.group(1))) if shares_m else 0,
        "price": float(price_m.group(1)) if price_m else 0.0,
        "insider_name": name_m.group(1).strip() if name_m else "Unknown",
    }


# ---------------------------------------------------------------------------
# Yahoo Finance – technical data
# ---------------------------------------------------------------------------

@dataclass
class TechnicalSignal:
    ticker: str
    price: float
    avg_volume: int
    rel_volume: float       # today's vol / 30-day avg
    ma20: float
    above_ma20: bool
    momentum_5d: float      # 5-day return in %
    momentum_20d: float     # 20-day return in %
    market_cap: float
    sector: str


def get_technical_signal(ticker: str) -> Optional[TechnicalSignal]:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{TECH_LOOKBACK_DAYS}d", auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None

        price = float(hist["Close"].iloc[-1])
        if price <= 0:
            return None

        ma20 = float(hist["Close"].tail(20).mean())
        vol30 = float(hist["Volume"].mean())
        today_vol = float(hist["Volume"].iloc[-1])

        info = stock.info or {}
        market_cap = float(info.get("marketCap") or 0)
        sector = info.get("sector", "Unknown")

        mom5 = ((price / float(hist["Close"].iloc[-6])) - 1) * 100 if len(hist) >= 6 else 0.0
        mom20 = ((price / float(hist["Close"].iloc[0])) - 1) * 100

        return TechnicalSignal(
            ticker=ticker,
            price=price,
            avg_volume=int(vol30),
            rel_volume=round(today_vol / vol30, 2) if vol30 > 0 else 0.0,
            ma20=round(ma20, 2),
            above_ma20=price > ma20,
            momentum_5d=round(mom5, 2),
            momentum_20d=round(mom20, 2),
            market_cap=market_cap,
            sector=sector,
        )
    except Exception as exc:
        log.debug("Technical %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Yahoo Finance – news headlines
# ---------------------------------------------------------------------------

@dataclass
class NewsResult:
    ticker: str
    headlines: list[str]
    sentiment_score: float  # 0–10, filled by Claude or keyword fallback
    thesis: str


def get_yahoo_news(ticker: str, days_back: int = NEWS_LOOKBACK_DAYS) -> Optional[NewsResult]:
    resp = _get(
        f"https://query1.finance.yahoo.com/v1/finance/search?q={ticker}&newsCount=10&quotesCount=0",
        timeout=8,
    )
    if not resp:
        return None
    try:
        items = resp.json().get("news", [])
    except Exception:
        return None

    cutoff = time.time() - days_back * 86400
    headlines = [
        item.get("title", "")
        for item in items
        if item.get("providerPublishTime", 0) >= cutoff and item.get("title")
    ]

    if not headlines:
        return None

    sentiment = _keyword_sentiment(headlines)
    return NewsResult(
        ticker=ticker,
        headlines=headlines[:5],
        sentiment_score=sentiment,
        thesis="",
    )


def _keyword_sentiment(headlines: list[str]) -> float:
    bullish = {"surge", "soar", "beat", "jump", "rally", "gains", "record", "boost",
               "deal", "win", "upgrade", "buy", "outperform", "strong", "growth", "profit"}
    bearish = {"fall", "drop", "miss", "plunge", "cut", "downgrade", "sell", "loss",
               "decline", "weak", "warning", "lawsuit", "fraud", "short", "crash"}
    score = 5.0
    for h in headlines:
        words = set(h.lower().split())
        score += 0.5 * len(words & bullish)
        score -= 0.5 * len(words & bearish)
    return max(0.0, min(10.0, score))


# ---------------------------------------------------------------------------
# Claude API – narrative analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(ticker: str, company: str, headlines: list[str], catalyst: str) -> tuple[float, str]:
    """
    Call the Claude API to score bullish conviction and generate a trade thesis.
    Returns (sentiment_score 0-10, thesis string).
    Falls back to (5.0, '') if ANTHROPIC_API_KEY is not set or call fails.
    """
    if not USE_CLAUDE_API:
        return 5.0, ""

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        headlines_text = "\n".join(f"- {h}" for h in headlines[:5])
        prompt = f"""You are a swing trading analyst. Evaluate {ticker} ({company}) as a 2–14 day swing trade.

Catalyst: {catalyst}

Recent headlines:
{headlines_text}

Reply with JSON only (no prose):
{{"sentiment_score": <1-10>, "thesis": "<one sentence bullish or bearish thesis>", "risk": "<one sentence main risk>"}}"""

        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        # Parse JSON from response
        json_m = re.search(r'\{.*\}', text, re.DOTALL)
        if json_m:
            data = json.loads(json_m.group())
            score = float(data.get("sentiment_score", 5))
            thesis = data.get("thesis", "")
            risk = data.get("risk", "")
            full_thesis = f"{thesis} Risk: {risk}" if risk else thesis
            return max(0.0, min(10.0, score)), full_thesis
    except Exception as exc:
        log.debug("Claude API %s: %s", ticker, exc)

    return 5.0, ""
