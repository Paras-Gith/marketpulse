"""One-time PayPal sandbox setup.

Creates the Pro product and monthly plan, and (when BASE_URL is a public https URL)
registers the webhook. Prints the values to paste into your .env file.

Run from the project root:  python -m scripts.setup_paypal
"""
import asyncio

from app.config import get_settings
from app.paypal import PayPalClient, PayPalError

EVENTS = [
    "BILLING.SUBSCRIPTION.ACTIVATED",
    "BILLING.SUBSCRIPTION.CANCELLED",
    "BILLING.SUBSCRIPTION.SUSPENDED",
    "BILLING.SUBSCRIPTION.EXPIRED",
    "PAYMENT.SALE.COMPLETED",
    "PAYMENT.CAPTURE.COMPLETED",
]


async def main() -> None:
    s = get_settings()
    pp = PayPalClient(s)
    try:
        product = await pp.create_product(f"{s.app_name} Pro", "Real-time AI-classified Indian market news")
        plan = await pp.create_plan(product["id"], f"{s.app_name} Pro (monthly)", s.pro_price_usd)
    except PayPalError as exc:
        raise SystemExit(f"PayPal setup failed: {exc}\n{exc.details or ''}")
    print("\nAdd these to your .env file:\n")
    print(f"PAYPAL_PLAN_ID={plan['id']}")

    if s.base_url.startswith("https://"):
        try:
            hook = await pp.create_webhook(f"{s.base_url}/webhooks/paypal", EVENTS)
            print(f"PAYPAL_WEBHOOK_ID={hook['id']}")
        except PayPalError as exc:
            print(f"\nCould not create the webhook: {exc}\n{exc.details or ''}")
            print("If it already exists, copy its ID from developer.paypal.com > your app > Webhooks.")
    else:
        print("\nBASE_URL is not a public https URL, so no webhook was created.")
        print(f"Set BASE_URL to your public URL, then run this again, or add {EVENTS[0]} and the other events")
        print("by hand in developer.paypal.com > your app > Webhooks, then set PAYPAL_WEBHOOK_ID.")


if __name__ == "__main__":
    asyncio.run(main())
