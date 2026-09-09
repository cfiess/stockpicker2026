"""
Data sources for swing trading screener.

Primary sources (SEC EDGAR — may be slow/blocked in CI):
  - 8-K Atom feed → corporate catalysts
  - Form 4 feed   → insider purchase transactions

Fallback sources (always work):
  - Yahoo Finance screener → day gainers / high-volume movers
  - Yahoo Finance news     → recent headlines per ticker
  - Claude API             → narrative analysis (optional)
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

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/html, */*",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 12, extra_headers: dict = None) -> Optional[requests.Response]:
    headers = {}
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(3):
        try:
            resp = SESSION.get(url, timeout=timeout, headers=headers)
            if resp.status_code == 200:
                return resp
            log.warning("GET %s → HTTP %d", url, resp.status_code)
            if resp.status_code in (403, 429):
                time.sleep(2 ** attempt)
        except requests.RequestException as exc:
            log.warning("GET %s failed (attempt %d): %s", url, attempt + 1, exc)
        time.sleep(1.0 * (attempt + 1))
    return None


def _cik_to_ticker_map() -> dict[str, str]:
    resp = _get(
        "https://www.sec.gov/files/company_tickers.json",
        extra_headers={"User-Agent": "swing-screener research@example.com"},
    )
    if not resp:
        log.warning("Could not fetch company_tickers.json")
        return {}
    try:
        data = resp.json()
        result = {str(v["cik_str"]).zfill(10): v["ticker"].upper() for v in data.values()}
        log.info("CIK map loaded: %d entries", len(result))
        return result
    except Exception as exc:
        log.warning("CIK map parse error: %s", exc)
        return {}


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
    if not cik_map:
        log.warning("SEC 8-K: empty CIK map, skipping")
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    resp = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K&dateb=&owner=include&count=80&output=atom",
        extra_headers={"User-Agent": "swing-screener research@example.com"},
    )
    if not resp:
        log.warning("SEC 8-K: feed unavailable")
        return {}

    results: dict[str, SecFiling] = {}
    entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)
    log.info("SEC 8-K: parsing %d entries", len(entries))

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

        # CIK can appear as CIK= or /data/CIK/
        cik_m = re.search(r'CIK[=:](\d+)', entry) or re.search(r'/data/(\d+)/', entry)
        if not cik_m:
            continue

        cik = cik_m.group(1).zfill(10)
        ticker = cik_map.get(cik)
        if not ticker:
            continue

        title = re.search(r'<title[^>]*>(.*?)</title>', entry, re.DOTALL)
        title_text = title.group(1).strip() if title else ""
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
    if any(w in t for w in ["earnings", "financial results", "quarterly", "revenue"]):
        return "Earnings"
    if any(w in t for w in ["partnership", "agreement", "contract", "deal"]):
        return "Partnership"
    if any(w in t for w in ["fda", "approval", "trial", "clinical"]):
        return "FDA/Regulatory"
    if any(w in t for w in ["buyback", "dividend", "distribution"]):
        return "Capital Return"
    if any(w in t for w in ["guidance", "outlook", "forecast"]):
        return "Guidance"
    return "Corporate Event"


# ---------------------------------------------------------------------------
# SEC Form 4 – insider transactions (fixed: use issuerCik from XML)
# ---------------------------------------------------------------------------

@dataclass
class InsiderBuy:
    ticker: str
    company: str
    insider_name: str
    transaction_type: str
    shares: int
    price: float
    filed_date: str


def get_insider_buys(days_back: int = INSIDER_LOOKBACK_DAYS) -> dict[str, list[InsiderBuy]]:
    """Return {ticker: [InsiderBuy, ...]} for Form 4 open-market PURCHASES."""
    cik_map = _cik_to_ticker_map()
    if not cik_map:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)

    resp = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=4&dateb=&owner=include&count=80&output=atom",
        extra_headers={"User-Agent": "swing-screener research@example.com"},
    )
    if not resp:
        log.warning("Form 4: feed unavailable")
        return {}

    results: dict[str, list[InsiderBuy]] = {}
    entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)
    log.info("Form 4: parsing %d entries", len(entries))

    processed = 0
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

        link_m = re.search(r'<link[^>]+href="([^"]+)"', entry)
        if not link_m:
            continue

        filing = _get_form4_detail(link_m.group(1), cik_map)
        if not filing:
            continue
        if filing.get("transaction_type") != "P":
            continue

        ticker = filing.get("issuer_ticker", "")
        if not ticker:
            continue

        buy = InsiderBuy(
            ticker=ticker,
            company=filing.get("issuer_name", ticker),
            insider_name=filing.get("insider_name", "Unknown"),
            transaction_type="P",
            shares=filing.get("shares", 0),
            price=filing.get("price", 0.0),
            filed_date=updated.strftime("%Y-%m-%d"),
        )
        results.setdefault(ticker, []).append(buy)
        processed += 1
        if processed >= 20:  # cap to avoid timeout
            break

    log.info("Form 4: found insider buys for %d tickers", len(results))
    return results


def _get_form4_detail(url: str, cik_map: dict) -> Optional[dict]:
    resp = _get(url, timeout=10, extra_headers={"User-Agent": "swing-screener research@example.com"})
    if not resp:
        return None

    # Find the primary XML document link in the index page
    xml_m = re.search(r'href="(/Archives/[^"]+\.xml)"', resp.text)
    if not xml_m:
        return None

    xml_resp = _get(
        "https://www.sec.gov" + xml_m.group(1), timeout=10,
        extra_headers={"User-Agent": "swing-screener research@example.com"},
    )
    if not xml_resp:
        return None

    xml = xml_resp.text

    # KEY FIX: use issuerCik (the company), not the URL CIK (the filer/insider)
    issuer_cik_m = re.search(r'<issuerCik>0*(\d+)</issuerCik>', xml)
    issuer_name_m = re.search(r'<issuerName>(.*?)</issuerName>', xml)
    tx_code = re.search(r'<transactionCode>(.*?)</transactionCode>', xml)
    shares_m = re.search(r'<transactionShares>\s*<value>([\d.]+)</value>', xml)
    price_m = re.search(r'<transactionPricePerShare>\s*<value>([\d.]+)</value>', xml)
    name_m = re.search(r'<rptOwnerName>(.*?)</rptOwnerName>', xml)

    if not issuer_cik_m:
        return None

    cik_padded = issuer_cik_m.group(1).zfill(10)
    issuer_ticker = cik_map.get(cik_padded, "")

    return {
        "transaction_type": tx_code.group(1).strip() if tx_code else "",
        "issuer_ticker": issuer_ticker,
        "issuer_name": issuer_name_m.group(1).strip() if issuer_name_m else "",
        "shares": int(float(shares_m.group(1))) if shares_m else 0,
        "price": float(price_m.group(1)) if price_m else 0.0,
        "insider_name": name_m.group(1).strip() if name_m else "Unknown",
    }


# ---------------------------------------------------------------------------
# Yahoo Finance movers — reliable fallback, always works
# ---------------------------------------------------------------------------

def get_yahoo_movers(count: int = 30) -> list[str]:
    """
    Return tickers of today's top gainers from Yahoo Finance.
    Used as fallback when SEC sources return nothing.
    """
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&scrIds=day_gainers&count={count}&start=0"
    )
    resp = _get(url)
    if not resp:
        log.warning("Yahoo movers: request failed")
        return []
    try:
        quotes = resp.json()["finance"]["result"][0]["quotes"]
        tickers = [q["symbol"] for q in quotes if q.get("symbol")]
        log.info("Yahoo movers: %d tickers", len(tickers))
        return tickers
    except Exception as exc:
        log.warning("Yahoo movers parse error: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Yahoo Finance – technical data
# ---------------------------------------------------------------------------

@dataclass
class TechnicalSignal:
    ticker: str
    price: float
    avg_volume: int
    rel_volume: float
    ma20: float
    above_ma20: bool
    momentum_5d: float
    momentum_20d: float
    market_cap: float
    sector: str


def get_technical_signal(ticker: str) -> Optional[TechnicalSignal]:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{TECH_LOOKBACK_DAYS}d", auto_adjust=True)
        if hist.empty or len(hist) < 5:
            return None

        price = float(hist["Close"].iloc[-1])
        if price <= 0:
            return None

        n = min(20, len(hist))
        ma20 = float(hist["Close"].tail(n).mean())
        vol_avg = float(hist["Volume"].mean())
        today_vol = float(hist["Volume"].iloc[-1])

        info = stock.info or {}
        market_cap = float(info.get("marketCap") or 0)
        sector = info.get("sector", "Unknown")

        idx5 = max(0, len(hist) - 6)
        mom5 = ((price / float(hist["Close"].iloc[idx5])) - 1) * 100
        mom20 = ((price / float(hist["Close"].iloc[0])) - 1) * 100

        return TechnicalSignal(
            ticker=ticker,
            price=price,
            avg_volume=int(vol_avg),
            rel_volume=round(today_vol / vol_avg, 2) if vol_avg > 0 else 0.0,
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
    sentiment_score: float
    thesis: str


def get_yahoo_news(ticker: str, days_back: int = NEWS_LOOKBACK_DAYS) -> Optional[NewsResult]:
    resp = _get(
        f"https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={ticker}&newsCount=10&quotesCount=0",
    )
    if not resp:
        return None
    try:
        items = resp.json().get("news", [])
    except Exception:
        return None

    cutoff = time.time() - days_back * 86400
    headlines = [
        item["title"] for item in items
        if item.get("providerPublishTime", 0) >= cutoff and item.get("title")
    ]

    if not headlines:
        return None

    return NewsResult(
        ticker=ticker,
        headlines=headlines[:5],
        sentiment_score=_keyword_sentiment(headlines),
        thesis="",
    )


def _keyword_sentiment(headlines: list[str]) -> float:
    bullish = {"surge", "soar", "beat", "jump", "rally", "gains", "record", "boost",
               "deal", "win", "upgrade", "buy", "outperform", "strong", "growth", "profit",
               "rise", "higher", "top", "breakout", "positive"}
    bearish = {"fall", "drop", "miss", "plunge", "cut", "downgrade", "sell", "loss",
               "decline", "weak", "warning", "lawsuit", "fraud", "short", "crash",
               "lower", "concern", "risk", "negative", "disappoint"}
    score = 5.0
    for h in headlines:
        words = set(h.lower().split())
        score += 0.4 * len(words & bullish)
        score -= 0.4 * len(words & bearish)
    return max(0.0, min(10.0, score))


# ---------------------------------------------------------------------------
# Claude API – narrative analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(
    ticker: str, company: str, headlines: list[str], catalyst: str
) -> tuple[float, str]:
    if not USE_CLAUDE_API:
        return 5.0, ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        headlines_text = "\n".join(f"- {h}" for h in headlines[:5])
        prompt = (
            f"You are a swing trading analyst. Evaluate {ticker} ({company}) "
            f"as a 2–14 day swing trade.\n\n"
            f"Catalyst: {catalyst}\n\nRecent headlines:\n{headlines_text}\n\n"
            f'Reply with JSON only: {{"sentiment_score": <1-10>, '
            f'"thesis": "<one sentence bullish/bearish thesis>", '
            f'"risk": "<main risk>"}}'
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        json_m = re.search(r'\{.*\}', text, re.DOTALL)
        if json_m:
            data = json.loads(json_m.group())
            score = float(data.get("sentiment_score", 5))
            thesis = data.get("thesis", "")
            risk = data.get("risk", "")
            return max(0.0, min(10.0, score)), f"{thesis} Risk: {risk}" if risk else thesis
    except Exception as exc:
        log.debug("Claude API %s: %s", ticker, exc)
    return 5.0, ""
