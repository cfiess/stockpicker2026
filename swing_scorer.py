"""
Scoring engine for swing trading candidates.

Score breakdown (max ~100):
  sec_catalyst      × 5.0  → up to 25
  insider_buying    × 4.0  → up to 20
  technical_momentum× 3.0  → up to 15 (sub-scores: trend + vol + momentum)
  news_sentiment    × 3.0  → up to 15 (Claude or keyword, 0-10 scaled)
  cross_source      × 2.0  → up to 10 (bonus for appearing in 2+ independent sources)
"""
import math
from dataclasses import dataclass, field
from typing import Optional

from swing_config import WEIGHTS, NUM_PICKS
from swing_data import SecFiling, InsiderBuy, TechnicalSignal, NewsResult


@dataclass
class SwingCandidate:
    ticker: str
    company: str
    score: float = 0.0
    rank: int = 0

    # Source data
    sec_filing: Optional[SecFiling] = None
    insider_buys: list[InsiderBuy] = field(default_factory=list)
    technical: Optional[TechnicalSignal] = None
    news: Optional[NewsResult] = None

    # Sub-scores
    sec_score: float = 0.0
    insider_score: float = 0.0
    technical_score: float = 0.0
    news_score: float = 0.0
    cross_score: float = 0.0

    # Claude output
    thesis: str = ""

    # Readable reason
    reason: str = ""


def _sigmoid(x: float, midpoint: float = 5.0, steepness: float = 0.4) -> float:
    return 1.0 / (1.0 + math.exp(-steepness * (x - midpoint)))


def compute_score(candidate: SwingCandidate) -> float:
    w = WEIGHTS

    # SEC catalyst — binary, +1 if present
    if candidate.sec_filing:
        candidate.sec_score = 1.0
    score = candidate.sec_score * w["sec_catalyst"]

    # Insider buying — scales with number of transactions
    if candidate.insider_buys:
        buy_count = len(candidate.insider_buys)
        total_value = sum(b.shares * b.price for b in candidate.insider_buys)
        # Cap at 1.0 after 3 purchases or $1M total value
        insider_raw = min(1.0, max(buy_count / 3, total_value / 1_000_000))
        candidate.insider_score = round(insider_raw, 3)
    score += candidate.insider_score * w["insider_buying"]

    # Technical momentum — three sub-factors each 0-1
    if candidate.technical:
        t = candidate.technical
        trend = 1.0 if t.above_ma20 else 0.0
        vol_score = min(1.0, t.rel_volume / 2.0)  # 2× avg volume → 1.0
        mom_score = _sigmoid(t.momentum_5d, midpoint=3.0, steepness=0.3)  # 3% 5d gain → 0.5
        candidate.technical_score = round((trend + vol_score + mom_score) / 3, 3)
    score += candidate.technical_score * w["technical_momentum"]

    # News sentiment — Claude or keyword score, normalized 0-1
    if candidate.news:
        raw = candidate.news.sentiment_score  # 0-10
        candidate.news_score = round(raw / 10.0, 3)
        if candidate.news.thesis:
            candidate.thesis = candidate.news.thesis
    score += candidate.news_score * w["news_sentiment"]

    # Cross-source bonus — each independent signal source adds 0.25
    sources = sum([
        bool(candidate.sec_filing),
        bool(candidate.insider_buys),
        bool(candidate.technical and candidate.technical.above_ma20),
        bool(candidate.news and candidate.news.sentiment_score > 6),
    ])
    candidate.cross_score = min(1.0, sources / 4)
    score += candidate.cross_score * w["cross_source"]

    candidate.score = round(score, 2)
    return candidate.score


def _build_reason(c: SwingCandidate) -> str:
    parts = []
    if c.sec_filing:
        parts.append(f"{c.sec_filing.catalyst_type} catalyst ({c.sec_filing.filed_date})")
    if c.insider_buys:
        b = c.insider_buys[0]
        parts.append(f"insider buy by {b.insider_name} ({b.shares:,} sh @ ${b.price:.2f})")
    if c.technical:
        t = c.technical
        trend = "above" if t.above_ma20 else "below"
        parts.append(f"{trend} 20-MA, {t.rel_volume:.1f}× vol, {t.momentum_5d:+.1f}% 5d")
    if c.news and c.news.headlines:
        parts.append(f"news: \"{c.news.headlines[0][:60]}...\"")
    return " | ".join(parts) if parts else "no details"


def rank_candidates(candidates: list[SwingCandidate], n: int = NUM_PICKS) -> list[SwingCandidate]:
    for c in candidates:
        compute_score(c)
        c.reason = _build_reason(c)

    ranked = sorted(candidates, key=lambda x: x.score, reverse=True)[:n]
    for i, c in enumerate(ranked, 1):
        c.rank = i
    return ranked
