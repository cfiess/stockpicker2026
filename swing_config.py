import os

EMAIL_FROM = os.getenv("GMAIL_USER", "")
EMAIL_TO = "cfiess@gmail.com"
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

NUM_PICKS = 3
SEC_LOOKBACK_DAYS = 14
INSIDER_LOOKBACK_DAYS = 14
NEWS_LOOKBACK_DAYS = 7
TECH_LOOKBACK_DAYS = 30

MIN_PRICE = 5.0
MAX_PRICE = 500.0
MIN_AVG_VOLUME = 300_000

WEIGHTS = {
    "sec_catalyst": 5.0,
    "insider_buying": 4.0,
    "technical_momentum": 3.0,
    "news_sentiment": 3.0,
    "cross_source": 2.0,
}

USE_CLAUDE_API = bool(ANTHROPIC_API_KEY)
