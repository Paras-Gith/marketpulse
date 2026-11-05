from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_FEEDS = ",".join(
    [
        "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
        "https://www.livemint.com/rss/markets",
        "https://www.business-standard.com/rss/markets-106.rss",
    ]
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "MarketPulse AI"
    base_url: str = "http://localhost:8000"
    secret_key: str = "change-me"
    database_url: str = "sqlite:///./marketpulse.db"

    # PayPal (use sandbox credentials from developer.paypal.com)
    paypal_env: str = "sandbox"
    paypal_client_id: str = ""
    paypal_client_secret: str = ""
    paypal_plan_id: str = ""
    paypal_webhook_id: str = ""
    pro_price_usd: str = "5.00"
    brief_price_usd: str = "1.00"

    # AI (Groq free tier). Without a key the app falls back to keyword rules.
    groq_api_key: str = ""
    groq_model: str = "llama-3.3-70b-versatile"

    # News feed behaviour
    news_feeds: str = DEFAULT_FEEDS
    poll_enabled: bool = True
    poll_interval_seconds: int = 300
    feed_delay_minutes: int = 30
    seed_demo: bool = True

    @property
    def paypal_base(self) -> str:
        if self.paypal_env == "live":
            return "https://api-m.paypal.com"
        return "https://api-m.sandbox.paypal.com"

    @property
    def feeds(self) -> list[str]:
        return [f.strip() for f in self.news_feeds.split(",") if f.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
