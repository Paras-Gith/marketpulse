import feedparser
from config import RSS_FEEDS


def fetch_latest_headlines():
    """Fetch latest headlines from all configured RSS feeds."""
    headlines = []
    for feed_url in RSS_FEEDS:
        try:
            parsed = feedparser.parse(feed_url)
            for entry in parsed.entries:
                headlines.append({
                    "headline": entry.get("title", "").strip(),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", ""),
                    "source": feed_url,
                })
        except Exception as e:
            print(f"Error fetching {feed_url}: {e}")
    return headlines