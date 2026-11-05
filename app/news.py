"""RSS polling, AI classification and demo data."""
import asyncio
import html
import logging
import re
from datetime import datetime, timedelta

import feedparser
import httpx
from sqlalchemy import select

from . import ai
from .config import get_settings
from .db import Article, SessionLocal, utcnow

log = logging.getLogger("marketpulse.news")
settings = get_settings()
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketPulseBot/1.0)"}


def _clean(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _entry_time(entry) -> datetime:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    try:
        return min(datetime(*parsed[:6]), utcnow())
    except (TypeError, ValueError):
        return utcnow()


async def fetch_entries(http: httpx.AsyncClient, url: str) -> list[dict]:
    try:
        r = await http.get(url, headers=HEADERS, follow_redirects=True)
        r.raise_for_status()
    except httpx.HTTPError as exc:
        log.warning("Feed failed %s: %s", url, exc)
        return []
    feed = feedparser.parse(r.content)
    source = _clean(feed.feed.get("title", "")) or httpx.URL(url).host
    entries = []
    for e in feed.entries[:25]:
        title, link = _clean(e.get("title", "")), e.get("link", "")
        if title and link:
            entries.append(
                {
                    "title": title[:500],
                    "link": link[:1000],
                    "summary": _clean(e.get("summary", ""))[:600],
                    "source": source[:120],
                    "published_at": _entry_time(e),
                }
            )
    return entries


async def poll_once(max_new: int = 12) -> int:
    """Fetch all feeds, classify new stories (newest first) and store them."""
    async with httpx.AsyncClient(timeout=15) as http:
        batches = await asyncio.gather(*(fetch_entries(http, u) for u in settings.feeds))
    entries = sorted((e for b in batches for e in b), key=lambda e: e["published_at"], reverse=True)
    with SessionLocal() as db:
        known = set(db.scalars(select(Article.link).where(Article.link.in_([e["link"] for e in entries]))))
        fresh, seen = [], set(known)
        for e in entries:
            if e["link"] not in seen:
                seen.add(e["link"])
                fresh.append(e)
        for e in fresh[:max_new]:
            db.add(Article(**e, **await ai.classify(e["title"], e["summary"])))
        db.commit()
    return min(len(fresh), max_new)


async def poll_forever() -> None:
    while True:
        try:
            added = await poll_once()
            log.info("Poll finished, %s new stories", added)
        except Exception:
            log.exception("Poll failed")
        await asyncio.sleep(settings.poll_interval_seconds)


# (minutes ago, title, summary, category, sentiment, insight)
DEMO = [
    (6, "Nifty 50 closes higher as banks and IT stocks lead the gains", "Benchmark indices ended the session in the green with broad buying in financials and technology shares.", "Markets", "bullish", "Broad participation suggests the rally is not limited to a few heavyweight stocks."),
    (14, "RBI policy panel signals a cautious stance on rate cuts", "Members of the monetary policy committee said inflation risks keep them watchful before easing further.", "RBI & Policy", "neutral", "Borrowing costs may stay elevated for longer, which matters for banks and rate-sensitive sectors."),
    (22, "Mid-size IT services firm reports weaker quarterly profit", "The company said client spending delays and currency effects weighed on its latest quarter.", "Earnings", "bearish", "Soft guidance from mid-tier IT names can drag sector sentiment."),
    (48, "Mainboard IPO draws strong demand on the final day of bidding", "The issue was subscribed several times over, led by institutional investors.", "IPO & Listings", "bullish", "Strong demand points to healthy risk appetite in the primary market."),
    (95, "Rupee slips against the dollar on crude oil concerns", "Higher oil prices and importer dollar demand pushed the currency lower in early trade.", "Commodities & Forex", "bearish", "A weaker rupee raises import costs and can pressure oil marketing companies."),
    (190, "Asian shares mixed as investors await US inflation data", "Regional markets traded in a narrow range ahead of a key economic release.", "Global Cues", "neutral", "Global inflation data can shift foreign investor flows into Indian equities."),
    (320, "Large private lender agrees to acquire a minority stake in a fintech", "The deal gives the bank a foothold in digital lending and payments.", "Mergers & Deals", "bullish", "Banks buying into fintech signals a push toward digital distribution."),
    (610, "Gold steadies near recent highs as festive demand picks up", "Jewellers reported improved retail demand while global prices held firm.", "Commodities & Forex", "bullish", "Firm gold prices support jewellery retailers but can widen the import bill."),
]


def seed_demo() -> None:
    """Insert sample stories, or move existing ones to 'recent' so the Pro delay is visible."""
    now = utcnow()
    with SessionLocal() as db:
        for i, (mins, title, summary, category, sentiment, insight) in enumerate(DEMO, start=1):
            link = f"demo://{i}"
            article = db.scalar(select(Article).where(Article.link == link))
            published = now - timedelta(minutes=mins)
            if article:
                article.published_at = published
            else:
                db.add(
                    Article(title=title, link=link, source="Demo feed", summary=summary, category=category,
                            sentiment=sentiment, insight=insight, published_at=published)
                )
        db.commit()
