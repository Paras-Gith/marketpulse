import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = "llama-3.3-70b-versatile"

CATEGORIES = [
    "Earnings/Results",
    "Regulatory/Policy",
    "Macroeconomic/Global",
    "Corporate Actions (M&A, buybacks, dividends)",
    "Risk/Alerts (fraud, litigation, governance issues)",
    "Other",
]

SYSTEM_PROMPT = f"""You are a financial news classifier for Indian public markets.
Classify the given headline into exactly one of these categories:
{chr(10).join(f"- {c}" for c in CATEGORIES)}

Instructions:
1. Output ONLY valid JSON, nothing else.
2. Include two fields:
   - "category": one of the categories above (exact match)
   - "confidence": a number between 0 and 1
3. Do not add explanations, markdown formatting, or extra text.
4. If the headline is ambiguous, choose the closest category.
"""

# RSS feeds to poll for Indian market news.
# Verify these are still live before relying on them long-term; RSS URLs
# occasionally change or get retired by the publisher.
RSS_FEEDS = [
    "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
    "https://www.moneycontrol.com/rss/marketreports.xml",
    "https://www.business-standard.com/rss/markets-106.rss",
]

# How often (in seconds) to check feeds for new headlines
POLL_INTERVAL_SECONDS = 60