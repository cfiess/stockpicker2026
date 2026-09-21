"""
Scoring engine for swing trading candidates.

Score components (max ~100):
  item_score       — 8-K item quality (0–9 × 3.0 weight)
  insider_score    — open-market purchases (0–1 × 4.0)
  technical_score  — momentum + vol + MA trend (0–1 × 3.0)
  news_score       — headline sentiment (0–10 normalized × 2.0)

Hard exclusions (applied in screener, not scorer):
  routine_filer, merger_target, non-equity, already_moved, rr<2, no_liquidity
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from swing_config import (
    MIN_SCORE, NEAR_MISS_THRESHOLD, NUM_PICKS,
    MAX_5D_GAIN_PCT, MAX_20D_GAIN_PCT, MIN_RR,
    MIN_PRICE, MAX_PRICE, MIN_ATR_PCT, MIN_AVG_DOLLAR_VOLUME,
    ROUTINE_ITEMS,
)
from swing_data import SecFiling, InsiderBuy, TechnicalSignal, NewsResult


@dataclass
class SwingCandidate:
    ticker: str
    company: str
    score: float = 0.0
    rank: int = 0

    sec_filing: Optional[SecFiling] = None
    insider_buys: list[InsiderBuy] = field(default_factory=list)
    technical: Optional[TechnicalSignal] = None
    news: Optional[NewsResult] = None

    # Sub-scores
    item_score: float = 0.0
    insider_score: float = 0.0
    technical_score: float = 0.0
    news_score: float = 0.0

    # Narrative
    thesis: str = ""

    # Exclusion info (set when excluded)
    excluded: bool = False
    exclude_reason: str = ""

    # Pass / near-miss
    is_near_miss: bool = False


def _sigmoid(x: float, mid: float = 5.0, k: float = 0.4) -> float:
    return 1.0 / (1.0 + math.exp(-k * (x - mid)))


def compute_score(c: SwingCandidate) -> float:
    total = 0.0

    # 8-K item quality
    if c.sec_filing:
        raw = c.sec_filing.best_item_score  # 0–9
        c.item_score = round(raw / 9.0, 3)
    total += c.item_score * 3.0

    # Insider buying
    if c.insider_buys:
        count = len(c.insider_buys)
        total_val = sum(b.shares * b.price for b in c.insider_buys)
        c.insider_score = round(min(1.0, max(count / 3, total_val / 1_000_000)), 3)
    total += c.insider_score * 4.0

    # Technical: MA trend + volume + momentum
    if c.technical:
        t = c.technical
        trend = 1.0 if t.above_ma20 else 0.3
        vol = min(1.0, t.rel_volume / 2.0)
        mom = _sigmoid(t.momentum_5d, mid=3.0, k=0.3)
        c.technical_score = round((trend + vol + mom) / 3, 3)
    total += c.technical_score * 3.0

    # News sentiment
    if c.news:
        c.news_score = round(c.news.sentiment_score / 10.0, 3)
        if c.news.thesis:
            c.thesis = c.news.thesis
    total += c.news_score * 2.0

    c.score = round(total, 2)
    return c.score


def _build_reason(c: SwingCandidate) -> str:
    parts = []
    if c.sec_filing:
        items_str = ", ".join(c.sec_filing.items) if c.sec_filing.items else "?"
        parts.append(f"8-K items {items_str} ({c.sec_filing.catalyst_type}, {c.sec_filing.filed_date})")
    if c.insider_buys:
        b = c.insider_buys[0]
        parts.append(f"insider {b.insider_name} bought {b.shares:,} @ ${b.price:.2f}")
    if c.technical:
        t = c.technical
        parts.append(
            f"${t.price:.2f} | {'above' if t.above_ma20 else 'below'} 20-MA "
            f"| {t.rel_volume:.1f}x vol | {t.momentum_5d:+.1f}% 5d"
        )
    if c.news and c.news.headlines:
        parts.append(f'"{c.news.headlines[0][:55]}..."')
    return " | ".join(parts) or "no detail"


def rank_candidates(
    candidates: list[SwingCandidate],
    n: int = NUM_PICKS,
) -> tuple[list[SwingCandidate], list[SwingCandidate]]:
    """
    Returns (picks, near_misses).
    picks: scored >= MIN_SCORE, top N
    near_misses: scored >= NEAR_MISS_THRESHOLD but didn't make the cut
    """
    for c in candidates:
        if not c.excluded:
            compute_score(c)
            # Build a reason string for the pick
            c.thesis = c.thesis or _build_reason(c)

    qualified = sorted(
        [c for c in candidates if not c.excluded and c.score >= MIN_SCORE],
        key=lambda x: x.score, reverse=True,
    )
    for i, c in enumerate(qualified[:n], 1):
        c.rank = i

    near_misses = sorted(
        [c for c in candidates if not c.excluded and NEAR_MISS_THRESHOLD <= c.score < MIN_SCORE],
        key=lambda x: x.score, reverse=True,
    )[:5]
    for c in near_misses:
        c.is_near_miss = True

    return qualified[:n], near_misses
