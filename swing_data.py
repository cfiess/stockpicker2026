"""
Data sources for swing trading screener.

Key data fetched here:
  - SEC EDGAR submissions API  → 8-K item numbers, recurring filer check
  - SEC EDGAR Form 4 feed      → insider purchases
  - Yahoo Finance (yfinance)   → price, ATR, 52w high, earnings date, vol
  - Yahoo Finance screener     → day-gainers fallback
  - Yahoo Finance news         → headlines for merger detection + sentiment
  - Anthropic Claude API       → narrative analysis (optional)
  - yfinance SPY + ^VIX        → market regime
"""
import json
import logging
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import requests
import yfinance as yf

from swing_config import (
    ANTHROPIC_API_KEY, USE_CLAUDE_API,
    SEC_LOOKBACK_DAYS, INSIDER_LOOKBACK_DAYS, NEWS_LOOKBACK_DAYS,
    TECH_LOOKBACK_DAYS, ITEM_SCORES, DEFAULT_ITEM_SCORE,
    RECURRING_FILER_WEEKS, RECURRING_FILER_MIN_COUNT,
    ATR_STOP_MULT, HIGH_VIX,
)

log = logging.getLogger(__name__)

_SEC_UA = "swing-screener/2.0 research@example.com"
_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": _BROWSER_UA})

MERGER_KEYWORDS = [
    "merger agreement", "per share in cash", "to be acquired",
    "go private", "go-private", "tender offer", "all-cash deal",
    "definitive agreement to acquire", "acquired by", "acquisition by",
    "cash consideration", "take private", "take-private",
]

EXCLUDED_QUOTE_TYPES = {"ETF", "ETN", "MUTUALFUND", "FUTURE", "CURRENCY", "INDEX"}
EXCLUDED_NAME_RE = re.compile(
    r'\betf\b|\betn\b|preferred|warrant|\bunit\b|spac|blank.check'
    r'|acquisition.corp|\btrust\b|series [a-z]|class [a-z] preferred',
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(url: str, timeout: int = 12, ua: str = _BROWSER_UA) -> Optional[requests.Response]:
    hdrs = {"User-Agent": ua}
    for attempt in range(3):
        try:
            r = SESSION.get(url, timeout=timeout, headers=hdrs)
            if r.status_code == 200:
                return r
            log.warning("GET %s → %d", url, r.status_code)
            if r.status_code in (403, 429, 503):
                time.sleep(2 ** attempt)
        except requests.RequestException as exc:
            log.warning("GET %s (attempt %d): %s", url, attempt + 1, exc)
        time.sleep(1.0 * (attempt + 1))
    return None


# ---------------------------------------------------------------------------
# CIK helpers
# ---------------------------------------------------------------------------

def _cik_to_ticker_map() -> dict[str, str]:
    r = _get("https://www.sec.gov/files/company_tickers.json", ua=_SEC_UA)
    if not r:
        log.warning("Could not load company_tickers.json")
        return {}
    data = r.json()
    result = {str(v["cik_str"]).zfill(10): v["ticker"].upper() for v in data.values()}
    log.info("CIK map: %d entries", len(result))
    return result


def _ticker_to_cik_map(cik_map: dict[str, str]) -> dict[str, str]:
    return {v: k for k, v in cik_map.items()}


# ---------------------------------------------------------------------------
# SEC dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SecFiling:
    ticker: str
    company: str
    catalyst_type: str
    filed_date: str
    cik: str
    items: list[str]          # e.g. ["2.02", "9.01"]
    best_item_score: float
    is_routine_filer: bool
    routine_reason: str = ""


@dataclass
class InsiderBuy:
    ticker: str
    company: str
    insider_name: str
    shares: int
    price: float
    filed_date: str


# ---------------------------------------------------------------------------
# EDGAR submissions API — item numbers + recurring-filer check
# ---------------------------------------------------------------------------

def _fetch_submissions(cik_padded: str) -> Optional[dict]:
    url = f"https://data.sec.gov/submissions/CIK{cik_padded}.json"
    r = _get(url, ua=_SEC_UA)
    if not r:
        return None
    try:
        return r.json()
    except Exception:
        return None


def _get_recent_8k_items(cik_padded: str) -> tuple[list[str], bool, str]:
    """
    Returns (items_for_latest_8K, is_routine_filer, routine_reason).
    Uses the EDGAR submissions JSON which includes itemized 8-K data.
    """
    sub = _fetch_submissions(cik_padded)
    if not sub:
        return [], False, ""

    recent = sub.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    items_list = recent.get("items", [])
    dates = recent.get("filingDate", [])

    if not forms:
        return [], False, ""

    cutoff_6w = (datetime.now(timezone.utc) - timedelta(weeks=RECURRING_FILER_WEEKS)).date()
    cutoff_recent = (datetime.now(timezone.utc) - timedelta(days=SEC_LOOKBACK_DAYS)).date()

    latest_items: list[str] = []
    item_counter: Counter = Counter()

    for i, form in enumerate(forms):
        if form not in ("8-K", "8-K/A"):
            continue
        try:
            filing_date = datetime.strptime(dates[i], "%Y-%m-%d").date()
        except (ValueError, IndexError):
            continue

        raw_items = items_list[i] if i < len(items_list) else ""
        parsed = [x.strip() for x in raw_items.split(",") if x.strip()]

        # Capture the most recent filing's items
        if filing_date >= cutoff_recent and not latest_items:
            latest_items = parsed

        # Count items filed in the last 6 weeks for recurring check
        if filing_date >= cutoff_6w:
            for item in parsed:
                item_counter[item] += 1

    # Recurring filer: any item filed >= threshold times in last 6 weeks
    for item, count in item_counter.items():
        if count >= RECURRING_FILER_MIN_COUNT:
            return latest_items, True, f"filed item {item} {count}× in {RECURRING_FILER_WEEKS} weeks"

    return latest_items, False, ""


def _best_item_score(items: list[str]) -> float:
    if not items:
        return DEFAULT_ITEM_SCORE
    return max(ITEM_SCORES.get(item, DEFAULT_ITEM_SCORE) for item in items)


def _classify_catalyst(items: list[str], company: str = "") -> str:
    item_names = {
        "2.02": "Earnings Results",
        "1.01": "Material Agreement",
        "2.01": "Acquisition/Disposal",
        "1.02": "Agreement Termination",
        "2.05": "Restructuring",
        "8.01": "Other Event",
        "7.01": "Reg FD Disclosure",
        "5.02": "Officer Change",
        "5.03": "Charter Amendment",
        "9.01": "Exhibit Filing",
    }
    if not items:
        return "Corporate Event"
    top = max(items, key=lambda x: ITEM_SCORES.get(x, DEFAULT_ITEM_SCORE))
    return item_names.get(top, f"Item {top}")


# ---------------------------------------------------------------------------
# SEC 8-K Atom feed + enrichment
# ---------------------------------------------------------------------------

def get_sec_catalysts(days_back: int = SEC_LOOKBACK_DAYS) -> dict[str, SecFiling]:
    cik_map = _cik_to_ticker_map()
    if not cik_map:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    r = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=8-K&dateb=&owner=include&count=80&output=atom",
        ua=_SEC_UA,
    )
    if not r:
        log.warning("8-K Atom feed unavailable")
        return {}

    entries = re.findall(r'<entry>(.*?)</entry>', r.text, re.DOTALL)
    log.info("8-K feed: %d entries", len(entries))

    seen_tickers: set[str] = set()
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

        cik_m = re.search(r'CIK[=:]0*(\d+)', entry) or re.search(r'/data/0*(\d+)/', entry)
        if not cik_m:
            continue
        cik = cik_m.group(1).zfill(10)
        ticker = cik_map.get(cik)
        if not ticker or ticker in seen_tickers:
            continue
        seen_tickers.add(ticker)

    # Enrich each ticker with item numbers from submissions API
    results: dict[str, SecFiling] = {}
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

        cik_m = re.search(r'CIK[=:]0*(\d+)', entry) or re.search(r'/data/0*(\d+)/', entry)
        if not cik_m:
            continue
        cik = cik_m.group(1).zfill(10)
        ticker = cik_map.get(cik)
        if not ticker or ticker in results:
            continue

        title_m = re.search(r'<title[^>]*>(.*?)</title>', entry, re.DOTALL)
        title_text = title_m.group(1).strip() if title_m else ""
        company_m = re.match(r'^(.*?)\s*\(', title_text)
        company = company_m.group(1).strip() if company_m else title_text

        # Fetch item numbers + routine check from EDGAR submissions
        items, is_routine, routine_reason = _get_recent_8k_items(cik)
        best_score = _best_item_score(items)
        catalyst = _classify_catalyst(items, company)

        results[ticker] = SecFiling(
            ticker=ticker,
            company=company,
            catalyst_type=catalyst,
            filed_date=updated.strftime("%Y-%m-%d"),
            cik=cik,
            items=items,
            best_item_score=best_score,
            is_routine_filer=is_routine,
            routine_reason=routine_reason,
        )
        time.sleep(0.15)   # polite rate limiting for EDGAR

    log.info("8-K catalysts enriched: %d", len(results))
    return results


# ---------------------------------------------------------------------------
# Form 4 insider transactions
# ---------------------------------------------------------------------------

def get_insider_buys(days_back: int = INSIDER_LOOKBACK_DAYS) -> dict[str, list[InsiderBuy]]:
    cik_map = _cik_to_ticker_map()
    if not cik_map:
        return {}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
    r = _get(
        "https://www.sec.gov/cgi-bin/browse-edgar"
        "?action=getcurrent&type=4&dateb=&owner=include&count=80&output=atom",
        ua=_SEC_UA,
    )
    if not r:
        return {}

    entries = re.findall(r'<entry>(.*?)</entry>', r.text, re.DOTALL)
    results: dict[str, list[InsiderBuy]] = {}
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

        filing = _parse_form4(link_m.group(1), cik_map)
        if not filing or filing.get("tx_type") != "P":
            continue

        ticker = filing.get("issuer_ticker", "")
        if not ticker:
            continue

        results.setdefault(ticker, []).append(InsiderBuy(
            ticker=ticker,
            company=filing.get("issuer_name", ticker),
            insider_name=filing.get("insider_name", "Unknown"),
            shares=filing.get("shares", 0),
            price=filing.get("price", 0.0),
            filed_date=updated.strftime("%Y-%m-%d"),
        ))
        processed += 1
        if processed >= 20:
            break

    log.info("Form 4: insider buys for %d tickers", len(results))
    return results


def _parse_form4(url: str, cik_map: dict) -> Optional[dict]:
    r = _get(url, ua=_SEC_UA)
    if not r:
        return None
    xml_m = re.search(r'href="(/Archives/[^"]+\.xml)"', r.text)
    if not xml_m:
        return None
    xr = _get("https://www.sec.gov" + xml_m.group(1), ua=_SEC_UA)
    if not xr:
        return None
    xml = xr.text
    issuer_cik_m = re.search(r'<issuerCik>0*(\d+)</issuerCik>', xml)
    if not issuer_cik_m:
        return None
    cik = issuer_cik_m.group(1).zfill(10)
    return {
        "tx_type": (re.search(r'<transactionCode>(.*?)</transactionCode>', xml) or [None, ""])[1].strip(),
        "issuer_ticker": cik_map.get(cik, ""),
        "issuer_name": (re.search(r'<issuerName>(.*?)</issuerName>', xml) or [None, ""])[1].strip(),
        "shares": int(float((re.search(r'<transactionShares>\s*<value>([\d.]+)</value>', xml) or [None, "0"])[1])),
        "price": float((re.search(r'<transactionPricePerShare>\s*<value>([\d.]+)</value>', xml) or [None, "0"])[1]),
        "insider_name": (re.search(r'<rptOwnerName>(.*?)</rptOwnerName>', xml) or [None, "Unknown"])[1].strip(),
    }


# ---------------------------------------------------------------------------
# Technical signal (ATR, 52w high, earnings date, instrument type)
# ---------------------------------------------------------------------------

@dataclass
class TechnicalSignal:
    ticker: str
    price: float
    avg_dollar_volume: float
    rel_volume: float           # vs 50-day avg
    ma20: float
    above_ma20: bool
    pct_above_ma20: float
    atr14: float
    atr_pct: float              # ATR14 / price %
    momentum_5d: float
    momentum_20d: float
    high_52w: float
    pct_from_52w_high: float    # negative means below high
    sector: str
    quote_type: str             # EQUITY, ETF, etc.
    long_name: str
    earnings_date: Optional[str]
    # Computed after construction
    stop_price: float = 0.0
    target_price: float = 0.0
    rr_ratio: float = 0.0


def _compute_atr14(hist: pd.DataFrame) -> float:
    n = min(15, len(hist))
    h = hist["High"].tail(n)
    l = hist["Low"].tail(n)
    c = hist["Close"].shift(1).tail(n)
    tr = pd.concat([h - l, (h - c).abs(), (l - c).abs()], axis=1).max(axis=1)
    return float(tr.tail(14).mean())


def get_technical_signal(ticker: str) -> Optional[TechnicalSignal]:
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period=f"{TECH_LOOKBACK_DAYS}d", auto_adjust=True)
        if hist.empty or len(hist) < 10:
            return None

        price = float(hist["Close"].iloc[-1])
        if price <= 0:
            return None

        info = stock.info or {}
        quote_type = info.get("quoteType", "EQUITY")
        long_name = info.get("longName") or info.get("shortName") or ticker

        ma20 = float(hist["Close"].tail(20).mean()) if len(hist) >= 20 else price
        vol50 = float(hist["Volume"].tail(50).mean()) if len(hist) >= 50 else float(hist["Volume"].mean())
        today_vol = float(hist["Volume"].iloc[-1])
        avg_dv = price * vol50

        atr14 = _compute_atr14(hist)
        atr_pct = (atr14 / price * 100) if price > 0 else 0.0

        high_52w = float(hist["High"].max())
        pct_from_high = ((price / high_52w) - 1) * 100 if high_52w > 0 else 0.0

        idx5 = max(0, len(hist) - 6)
        mom5 = ((price / float(hist["Close"].iloc[idx5])) - 1) * 100 if len(hist) > 5 else 0.0
        mom20 = ((price / float(hist["Close"].iloc[0])) - 1) * 100

        # Earnings date
        earnings_date: Optional[str] = None
        try:
            cal = stock.calendar
            if cal is not None and not cal.empty:
                ed = cal.get("Earnings Date")
                if ed is not None and len(ed) > 0:
                    earnings_date = str(ed.iloc[0])[:10]
        except Exception:
            pass

        # Stop and target
        stop = price - ATR_STOP_MULT * atr14   # 1.5×ATR below entry
        risk = price - stop                     # = 1.5×ATR

        # Target: 60-day swing high is the natural resistance level.
        # Fall back to a theoretical 2.5:1 reward if no overhead resistance.
        recent_high_60 = float(hist["High"].tail(60).max())
        if recent_high_60 > price * 1.02:   # meaningful upside exists
            target = recent_high_60
        else:
            target = price + 2.5 * risk     # theoretical: 2.5×risk = 2.5:1 R:R

        rr = (target - price) / risk if risk > 0 else 0.0

        sig = TechnicalSignal(
            ticker=ticker,
            price=price,
            avg_dollar_volume=avg_dv,
            rel_volume=round(today_vol / vol50, 2) if vol50 > 0 else 0.0,
            ma20=round(ma20, 2),
            above_ma20=price > ma20,
            pct_above_ma20=round(((price / ma20) - 1) * 100, 2) if ma20 > 0 else 0.0,
            atr14=round(atr14, 3),
            atr_pct=round(atr_pct, 2),
            momentum_5d=round(mom5, 2),
            momentum_20d=round(mom20, 2),
            high_52w=round(high_52w, 2),
            pct_from_52w_high=round(pct_from_high, 2),
            sector=info.get("sector", "Unknown"),
            quote_type=quote_type,
            long_name=long_name,
            earnings_date=earnings_date,
            stop_price=round(stop, 2),
            target_price=round(target, 2),
            rr_ratio=round(rr, 2),
        )
        return sig
    except Exception as exc:
        log.debug("Technical %s: %s", ticker, exc)
        return None


# ---------------------------------------------------------------------------
# Market regime
# ---------------------------------------------------------------------------

@dataclass
class MarketRegime:
    spy_price: float
    spy_ma50: float
    spy_above_ma50: bool
    vix: float
    is_bad: bool
    label: str


def get_market_regime() -> Optional[MarketRegime]:
    try:
        spy_hist = yf.Ticker("SPY").history(period="60d", auto_adjust=True)
        vix_hist = yf.Ticker("^VIX").history(period="5d", auto_adjust=True)
        if spy_hist.empty or vix_hist.empty:
            return None

        spy_price = float(spy_hist["Close"].iloc[-1])
        spy_ma50 = float(spy_hist["Close"].tail(50).mean())
        vix = float(vix_hist["Close"].iloc[-1])
        above = spy_price > spy_ma50
        bad = (not above) and (vix > HIGH_VIX)

        label = (
            f"SPY ${spy_price:.2f} {'above' if above else 'BELOW'} 50-MA (${spy_ma50:.2f}) | "
            f"VIX {vix:.1f} ({'elevated' if vix > HIGH_VIX else 'normal'})"
        )
        return MarketRegime(
            spy_price=spy_price, spy_ma50=spy_ma50,
            spy_above_ma50=above, vix=vix, is_bad=bad, label=label,
        )
    except Exception as exc:
        log.debug("Market regime: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Merger detection
# ---------------------------------------------------------------------------

def is_merger_target(ticker: str, headlines: list[str], filing_text: str = "") -> bool:
    combined = " ".join(headlines + [filing_text]).lower()
    return any(kw in combined for kw in MERGER_KEYWORDS)


def is_near_deal_price(price: float, headlines: list[str]) -> bool:
    """Check if price is within 5% of a stated per-share cash price."""
    for h in headlines:
        m = re.search(r'\$\s*(\d+(?:\.\d+)?)\s*(?:per share|a share)', h, re.IGNORECASE)
        if m:
            deal_price = float(m.group(1))
            if deal_price > 0 and abs(price / deal_price - 1) < 0.05:
                return True
    return False


# ---------------------------------------------------------------------------
# Yahoo Finance movers (fallback)
# ---------------------------------------------------------------------------

def get_yahoo_movers(count: int = 30) -> list[str]:
    url = (
        "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved"
        f"?formatted=false&scrIds=day_gainers&count={count}&start=0"
    )
    r = _get(url)
    if not r:
        return []
    try:
        quotes = r.json()["finance"]["result"][0]["quotes"]
        return [q["symbol"] for q in quotes if q.get("symbol")]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Yahoo Finance news
# ---------------------------------------------------------------------------

@dataclass
class NewsResult:
    ticker: str
    headlines: list[str]
    sentiment_score: float
    thesis: str


def get_yahoo_news(ticker: str, days_back: int = NEWS_LOOKBACK_DAYS) -> Optional[NewsResult]:
    r = _get(
        f"https://query1.finance.yahoo.com/v1/finance/search"
        f"?q={ticker}&newsCount=10&quotesCount=0",
    )
    if not r:
        return None
    try:
        items = r.json().get("news", [])
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
    bullish = {
        "surge", "soar", "beat", "jump", "rally", "gains", "record", "boost",
        "deal", "win", "upgrade", "buy", "outperform", "strong", "growth",
        "profit", "rise", "higher", "top", "breakout", "positive", "raises",
    }
    bearish = {
        "fall", "drop", "miss", "plunge", "cut", "downgrade", "sell", "loss",
        "decline", "weak", "warning", "lawsuit", "fraud", "short", "crash",
        "lower", "concern", "risk", "negative", "disappoint", "probe",
    }
    score = 5.0
    for h in headlines:
        words = set(h.lower().split())
        score += 0.4 * len(words & bullish)
        score -= 0.4 * len(words & bearish)
    return max(0.0, min(10.0, score))


# ---------------------------------------------------------------------------
# Claude API narrative analysis
# ---------------------------------------------------------------------------

def analyze_with_claude(
    ticker: str, company: str, headlines: list[str], catalyst: str
) -> tuple[float, str]:
    if not USE_CLAUDE_API:
        return 5.0, ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = (
            f"Evaluate {ticker} ({company}) as a 2–14 day swing trade.\n"
            f"Catalyst: {catalyst}\n"
            f"Headlines:\n" + "\n".join(f"- {h}" for h in headlines[:5]) + "\n\n"
            f'Reply JSON only: {{"sentiment_score":<1-10>,"thesis":"<one sentence>","risk":"<one sentence>"}}'
        )
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text.strip()
        m = re.search(r'\{.*\}', text, re.DOTALL)
        if m:
            d = json.loads(m.group())
            score = float(d.get("sentiment_score", 5))
            thesis = d.get("thesis", "")
            risk = d.get("risk", "")
            return max(0.0, min(10.0, score)), f"{thesis} Risk: {risk}" if risk else thesis
    except Exception as exc:
        log.debug("Claude API %s: %s", ticker, exc)
    return 5.0, ""
