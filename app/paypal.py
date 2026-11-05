"""Thin async client for the PayPal REST APIs used by MarketPulse AI.

Subscriptions API  -> Pro plan (recurring)
Orders v2 API      -> Deep Brief (one-time purchase)
Webhooks           -> signature verification via PayPal's verify endpoint
"""
import logging
import time
import uuid

import httpx

from .config import Settings

log = logging.getLogger("marketpulse.paypal")


class PayPalError(Exception):
    def __init__(self, message: str, status: int | None = None, details: str | None = None):
        super().__init__(message)
        self.status = status
        self.details = details


def approval_link(payload: dict) -> str:
    for link in payload.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            return link["href"]
    raise PayPalError("PayPal did not return an approval link")


class PayPalClient:
    def __init__(self, settings: Settings):
        self.s = settings
        self._token: str | None = None
        self._expires = 0.0

    async def _access_token(self, http: httpx.AsyncClient) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        r = await http.post(
            f"{self.s.paypal_base}/v1/oauth2/token",
            auth=(self.s.paypal_client_id, self.s.paypal_client_secret),
            data={"grant_type": "client_credentials"},
        )
        if r.status_code != 200:
            raise PayPalError("PayPal authentication failed", r.status_code, r.text)
        data = r.json()
        self._token = data["access_token"]
        self._expires = time.time() + data.get("expires_in", 300)
        return self._token

    async def request(self, method: str, path: str, json: dict | None = None, headers: dict | None = None) -> dict:
        if not (self.s.paypal_client_id and self.s.paypal_client_secret):
            raise PayPalError("PayPal credentials are not configured")
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                token = await self._access_token(http)
                h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", **(headers or {})}
                r = await http.request(method, f"{self.s.paypal_base}{path}", json=json, headers=h)
        except httpx.HTTPError as exc:
            raise PayPalError(f"Could not reach PayPal: {exc}") from exc
        if r.status_code >= 400:
            log.error("PayPal %s %s -> %s %s", method, path, r.status_code, r.text)
            raise PayPalError(f"PayPal returned {r.status_code}", r.status_code, r.text)
        return r.json() if r.content else {}

    # ---- Subscriptions (Pro plan) -------------------------------------------------
    async def create_subscription(self, plan_id: str, user_id: int, return_url: str, cancel_url: str) -> dict:
        return await self.request(
            "POST",
            "/v1/billing/subscriptions",
            json={
                "plan_id": plan_id,
                "custom_id": str(user_id),
                "application_context": {
                    "brand_name": self.s.app_name,
                    "shipping_preference": "NO_SHIPPING",
                    "user_action": "SUBSCRIBE_NOW",
                    "return_url": return_url,
                    "cancel_url": cancel_url,
                },
            },
            headers={"PayPal-Request-Id": str(uuid.uuid4())},
        )

    async def get_subscription(self, subscription_id: str) -> dict:
        return await self.request("GET", f"/v1/billing/subscriptions/{subscription_id}")

    async def cancel_subscription(self, subscription_id: str, reason: str) -> None:
        await self.request("POST", f"/v1/billing/subscriptions/{subscription_id}/cancel", json={"reason": reason})

    # ---- Orders (Deep Brief) ------------------------------------------------------
    async def create_order(
        self, amount: str, currency: str, custom_id: str, description: str, return_url: str, cancel_url: str
    ) -> dict:
        return await self.request(
            "POST",
            "/v2/checkout/orders",
            json={
                "intent": "CAPTURE",
                "purchase_units": [
                    {
                        "custom_id": custom_id,
                        "description": description[:127],
                        "amount": {"currency_code": currency, "value": amount},
                    }
                ],
                "payment_source": {
                    "paypal": {
                        "experience_context": {
                            "brand_name": self.s.app_name,
                            "shipping_preference": "NO_SHIPPING",
                            "user_action": "PAY_NOW",
                            "return_url": return_url,
                            "cancel_url": cancel_url,
                        }
                    }
                },
            },
            headers={"PayPal-Request-Id": str(uuid.uuid4())},
        )

    async def capture_order(self, order_id: str) -> dict:
        return await self.request(
            "POST",
            f"/v2/checkout/orders/{order_id}/capture",
            json={},
            headers={"PayPal-Request-Id": f"capture-{order_id}", "Prefer": "return=representation"},
        )

    # ---- Webhooks -----------------------------------------------------------------
    async def verify_webhook(self, headers, event: dict) -> bool:
        body = {
            "auth_algo": headers.get("paypal-auth-algo"),
            "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": self.s.paypal_webhook_id,
        }
        if not all(body.values()):
            log.warning("Webhook missing signature headers or PAYPAL_WEBHOOK_ID is not set")
            return False
        body["webhook_event"] = event
        result = await self.request("POST", "/v1/notifications/verify-webhook-signature", json=body)
        return result.get("verification_status") == "SUCCESS"

    # ---- One-time setup helpers (used by scripts/setup_paypal.py) -----------------
    async def create_product(self, name: str, description: str) -> dict:
        return await self.request(
            "POST",
            "/v1/catalogs/products",
            json={"name": name, "description": description, "type": "SERVICE", "category": "SOFTWARE"},
            headers={"PayPal-Request-Id": str(uuid.uuid4())},
        )

    async def create_plan(self, product_id: str, name: str, price: str) -> dict:
        return await self.request(
            "POST",
            "/v1/billing/plans",
            json={
                "product_id": product_id,
                "name": name,
                "status": "ACTIVE",
                "billing_cycles": [
                    {
                        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                        "tenure_type": "REGULAR",
                        "sequence": 1,
                        "total_cycles": 0,
                        "pricing_scheme": {"fixed_price": {"value": price, "currency_code": "USD"}},
                    }
                ],
                "payment_preferences": {
                    "auto_bill_outstanding": True,
                    "setup_fee_failure_action": "CONTINUE",
                    "payment_failure_threshold": 3,
                },
            },
            headers={"PayPal-Request-Id": str(uuid.uuid4())},
        )

    async def create_webhook(self, url: str, event_types: list[str]) -> dict:
        return await self.request(
            "POST",
            "/v1/notifications/webhooks",
            json={"url": url, "event_types": [{"name": n} for n in event_types]},
        )
