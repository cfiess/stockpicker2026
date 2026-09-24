import os

# Email
EMAIL_FROM = os.getenv("GMAIL_USER", "")
EMAIL_TO = "cfiess@gmail.com"
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
USE_CLAUDE_API = bool(ANTHROPIC_API_KEY)

# Pick count / score gate
NUM_PICKS = 3
MIN_SCORE = 5.0          # nothing below this makes it into picks
NEAR_MISS_THRESHOLD = 3.5  # include in exclusion log if >= this

# Lookbacks
SEC_LOOKBACK_DAYS = 14
INSIDER_LOOKBACK_DAYS = 14
NEWS_LOOKBACK_DAYS = 7
TECH_LOOKBACK_DAYS = 60   # enough for 50-day vol + 52w-high

# Price / liquidity filters
MIN_PRICE = 5.0
MAX_PRICE = 500.0
MIN_AVG_DOLLAR_VOLUME = 5_000_000   # $5M/day
MIN_ATR_PCT = 1.5                    # ATR(14)/price must be >= 1.5%

# Already-moved hard exclusions
MAX_5D_GAIN_PCT = 15.0
MAX_20D_GAIN_PCT = 25.0
PCT_FROM_52W_HIGH_MAX = -2.0         # exclude if within 2% of 52w high (too extended)

# Risk/reward
MIN_RR = 1.5             # stop = price - 1.5×ATR; require (target-price)/(price-stop) >= 1.5
ATR_STOP_MULT = 1.5

# Market regime
HIGH_VIX = 25.0
BAD_REGIME_SCORE_BUMP = 2.0   # raise effective MIN_SCORE by this when SPY < 50MA & VIX high

# Freshness
FRESHNESS_TRADING_DAYS = 10

# Recurring filer detection
RECURRING_FILER_WEEKS = 6
RECURRING_FILER_MIN_COUNT = 3   # same item in >= 3 of last 6 weeks → routine filer

# 8-K item scoring (higher = more signal)
ITEM_SCORES: dict[str, float] = {
    "2.02": 9.0,   # Earnings results
    "1.01": 8.0,   # Material definitive agreement
    "2.01": 7.0,   # Completion of acquisition/disposition
    "1.02": 5.0,   # Termination of material agreement (sometimes bullish)
    "2.05": 5.0,   # Costs associated with exit/disposal (restructuring — watch)
    "2.06": 5.0,   # Material impairments
    "8.01": 2.5,   # Other events (context-dependent)
    "7.01": 2.0,   # Reg FD disclosure (usually routine)
    "5.02": 1.5,   # Officer departure/appointment (low unless abrupt CEO/CFO exit)
    "5.03": 0.0,   # Amendment to articles (routine)
    "9.01": 0.0,   # Exhibits only (no independent news value)
}
DEFAULT_ITEM_SCORE = 3.5   # for items not in the table

# 8-K items that are too routine to use as primary catalyst
ROUTINE_ITEMS = {"9.01", "5.03", "7.01"}
