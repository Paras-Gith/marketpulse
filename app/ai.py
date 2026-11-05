"""AI layer: classify news and write Deep Briefs with Groq, with offline fallbacks."""
import json
import logging

from groq import AsyncGroq

from .config import get_settings

log = logging.getLogger("marketpulse.ai")
settings = get_settings()

CATEGORIES = [
    "Markets",
    "Earnings",
    "RBI & Policy",
    "IPO & Listings",
    "Mergers & Deals",
    "Commodities & Forex",
    "Global Cues",
    "Other",
]
SENTIMENTS = ("bullish", "bearish", "neutral")
BRIEF_SECTIONS = [
    ("what_happened", "What happened"),
    ("why_it_matters", "Why it matters"),
    ("who_is_affected", "Who is affected"),
    ("what_to_watch", "What to watch next"),
]

CLASSIFY_PROMPT = (
    "You classify Indian financial-market news for investors. Reply with a JSON object with keys: "
    f"category (exactly one of {CATEGORIES}), "
    "sentiment (bullish, bearish or neutral for Indian equity markets), "
    "insight (one plain sentence, at most 25 words, on why this matters to an investor)."
)
BRIEF_PROMPT = (
    "You are a markets analyst writing for global investors who follow Indian markets. "
    "Reply with a JSON object with keys what_happened, why_it_matters, who_is_affected, what_to_watch. "
    "Each value is 2 to 3 plain sentences. Use only facts in the source text, invent no figures, "
    "and give no buy or sell advice."
)

_KEYWORDS = {
    "Earnings": ["earnings", "profit", "revenue", "quarterly", "results", "dividend"],
    "RBI & Policy": ["rbi", "repo rate", "monetary policy", "inflation", "sebi", "budget", "gst"],
    "IPO & Listings": ["ipo", "listing", "bidding", "subscription", "grey market"],
    "Mergers & Deals": ["acquire", "acquisition", "merger", "stake", "buyout", "takeover"],
    "Commodities & Forex": ["gold", "crude", "oil", "rupee", "dollar", "forex", "silver"],
    "Global Cues": ["fed ", "wall street", "nasdaq", "asian", "us inflation", "china"],
    "Markets": ["sensex", "nifty", "stocks", "shares", "market", "index"],
}
_BULL = ["rally", "surge", "gain", "rise", "jump", "record", "upbeat", "strong", "higher", "beat"]
_BEAR = ["fall", "drop", "slump", "decline", "plunge", "loss", "weak", "slips", "lower", "miss"]

_client: AsyncGroq | None = None


def _groq() -> AsyncGroq | None:
    global _client
    if _client is None and settings.groq_api_key:
        _client = AsyncGroq(api_key=settings.groq_api_key)
    return _client


async def _ask_json(system: str, user: str, max_tokens: int) -> dict | None:
    client = _groq()
    if client is None:
        return None
    try:
        resp = await client.chat.completions.create(
            model=settings.groq_model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            response_format={"type": "json_object"},
            temperature=0.2,
            max_tokens=max_tokens,
        )
        data = json.loads(resp.choices[0].message.content)
        return data if isinstance(data, dict) else None
    except Exception as exc:  # network error, rate limit, malformed JSON
        log.warning("Groq request failed: %s", exc)
        return None


def _keyword_classify(title: str, summary: str) -> dict:
    text = f"{title} {summary}".lower()
    category = next((c for c, words in _KEYWORDS.items() if any(w in text for w in words)), "Other")
    bull = sum(w in text for w in _BULL)
    bear = sum(w in text for w in _BEAR)
    sentiment = "bullish" if bull > bear else "bearish" if bear > bull else "neutral"
    return {"category": category, "sentiment": sentiment, "insight": ""}


async def classify(title: str, summary: str) -> dict:
    data = await _ask_json(CLASSIFY_PROMPT, f"Title: {title}\nSummary: {summary[:500]}", 200)
    if data:
        category = data.get("category")
        sentiment = str(data.get("sentiment", "")).lower()
        if category in CATEGORIES and sentiment in SENTIMENTS:
            return {"category": category, "sentiment": sentiment, "insight": str(data.get("insight", ""))[:300]}
    return _keyword_classify(title, summary)


async def generate_brief(title: str, summary: str, category: str) -> dict:
    data = await _ask_json(BRIEF_PROMPT, f"Category: {category}\nTitle: {title}\nSummary: {summary}", 700)
    if data and all(isinstance(data.get(k), str) and data[k].strip() for k, _ in BRIEF_SECTIONS):
        return {k: data[k].strip() for k, _ in BRIEF_SECTIONS}
    return {
        "what_happened": summary or title,
        "why_it_matters": f"This is a {category.lower()} story. Detailed AI analysis is unavailable on this server right now.",
        "who_is_affected": "Investors with exposure to the companies or sectors named in the story.",
        "what_to_watch": "Follow-up reports from the source and the next market session's reaction.",
    }
