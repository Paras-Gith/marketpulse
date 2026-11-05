import os
import tempfile
import uuid

_tmp = tempfile.mkdtemp()
os.environ.update(
    DATABASE_URL=f"sqlite:///{_tmp}/test.db", POLL_ENABLED="false", SEED_DEMO="true", GROQ_API_KEY="",
    PAYPAL_CLIENT_ID="id", PAYPAL_CLIENT_SECRET="secret", PAYPAL_PLAN_ID="P-TEST", PAYPAL_WEBHOOK_ID="WH-TEST",
    SECRET_KEY="test-secret", BASE_URL="http://testserver",
)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Article, Brief, Payment, SessionLocal, User  # noqa: E402
from app.main import app, paypal  # noqa: E402


@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c


def signup(client) -> str:
    email = f"{uuid.uuid4().hex[:8]}@example.com"
    r = client.post("/signup", data={"email": email, "password": "correct-horse"}, follow_redirects=False)
    assert r.status_code == 303
    return email


def get_user(email) -> User:
    with SessionLocal() as db:
        return db.scalar(select(User).where(User.email == email))


def open_article_id() -> int:
    """Oldest demo story: already past the free-tier delay."""
    with SessionLocal() as db:
        return db.scalar(select(Article.id).order_by(Article.published_at.asc()))


def webhook(client, monkeypatch, event_type, resource, event_id=None, valid=True):
    async def fake_verify(headers, event):
        return valid

    monkeypatch.setattr(paypal, "verify_webhook", fake_verify)
    return client.post("/webhooks/paypal", json={"id": event_id or f"WH-{uuid.uuid4().hex}", "event_type": event_type, "resource": resource})


def test_dashboard_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Indian market news" in r.text


def test_locked_stories_hide_their_text_from_free_users(client):
    data = client.get("/api/articles").json()
    locked = [a for a in data if a["locked"]]
    assert locked and all("title" not in a for a in locked)
    assert any(not a["locked"] for a in data)


def test_webhook_rejects_invalid_signature(client, monkeypatch):
    r = webhook(client, monkeypatch, "BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-X"}, valid=False)
    assert r.status_code == 400


def test_subscription_lifecycle(client, monkeypatch):
    email = signup(client)

    async def fake_create(plan_id, user_id, return_url, cancel_url):
        return {"id": "I-SUB1", "status": "APPROVAL_PENDING", "links": [{"rel": "approve", "href": "https://paypal.test/approve"}]}

    monkeypatch.setattr(paypal, "create_subscription", fake_create)
    r = client.post("/subscribe", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "https://paypal.test/approve"
    assert get_user(email).plan == "free"

    r = webhook(client, monkeypatch, "BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-SUB1"}, event_id="WH-A")
    assert r.json() == {"status": "ok"}
    assert get_user(email).plan == "pro"
    assert not any(a["locked"] for a in client.get("/api/articles").json())

    # PayPal retries are ignored
    assert webhook(client, monkeypatch, "BILLING.SUBSCRIPTION.ACTIVATED", {"id": "I-SUB1"}, event_id="WH-A").json() == {"status": "duplicate"}

    sale = {"id": "SALE-1", "billing_agreement_id": "I-SUB1", "amount": {"total": "5.00", "currency": "USD"}}
    webhook(client, monkeypatch, "PAYMENT.SALE.COMPLETED", sale)
    with SessionLocal() as db:
        assert db.scalar(select(Payment).where(Payment.paypal_id == "SALE-1")).kind == "subscription"

    webhook(client, monkeypatch, "BILLING.SUBSCRIPTION.CANCELLED", {"id": "I-SUB1"})
    assert get_user(email).plan == "free"


def fake_capture(custom_id, capture_id="CAP-1", amount="1.00"):
    async def _capture(order_id):
        cap = {"id": capture_id, "status": "COMPLETED", "amount": {"currency_code": "USD", "value": amount}, "custom_id": custom_id}
        return {"id": order_id, "status": "COMPLETED", "purchase_units": [{"custom_id": custom_id, "payments": {"captures": [cap]}}]}
    return _capture


def test_deep_brief_purchase_and_webhook_is_idempotent(client, monkeypatch):
    email = signup(client)
    uid, aid = get_user(email).id, open_article_id()

    async def fake_create(**kwargs):
        assert kwargs["custom_id"] == f"{uid}:{aid}" and kwargs["amount"] == "1.00"
        return {"id": "ORDER-1", "links": [{"rel": "payer-action", "href": "https://paypal.test/pay"}]}

    monkeypatch.setattr(paypal, "create_order", fake_create)
    r = client.post(f"/brief/{aid}/buy", follow_redirects=False)
    assert r.headers["location"] == "https://paypal.test/pay"
    assert client.get(f"/brief/{aid}", follow_redirects=False).status_code == 303  # not bought yet

    monkeypatch.setattr(paypal, "capture_order", fake_capture(f"{uid}:{aid}"))
    r = client.get("/brief/return?token=ORDER-1", follow_redirects=False)
    assert r.headers["location"] == f"/brief/{aid}"
    page = client.get(f"/brief/{aid}")
    assert page.status_code == 200 and "What happened" in page.text and "Why it matters" in page.text

    # The webhook for the same capture must not create a second payment or brief.
    res = {"id": "CAP-1", "status": "COMPLETED", "custom_id": f"{uid}:{aid}", "amount": {"currency_code": "USD", "value": "1.00"}}
    webhook(client, monkeypatch, "PAYMENT.CAPTURE.COMPLETED", res)
    with SessionLocal() as db:
        assert len(db.scalars(select(Payment).where(Payment.paypal_id == "CAP-1")).all()) == 1
        assert len(db.scalars(select(Brief).where(Brief.user_id == uid)).all()) == 1


def test_capture_for_another_account_is_rejected(client, monkeypatch):
    email = signup(client)
    me, aid = get_user(email).id, open_article_id()
    monkeypatch.setattr(paypal, "capture_order", fake_capture(f"{me + 999}:{aid}", capture_id="CAP-OTHER"))
    r = client.get("/brief/return?token=ORDER-2", follow_redirects=False)
    assert r.headers["location"] == "/"
    with SessionLocal() as db:
        assert db.scalar(select(Brief).where(Brief.user_id == me)) is None


def test_wrong_amount_does_not_unlock(client, monkeypatch):
    email = signup(client)
    uid, aid = get_user(email).id, open_article_id()
    monkeypatch.setattr(paypal, "capture_order", fake_capture(f"{uid}:{aid}", capture_id="CAP-LOW", amount="0.01"))
    client.get("/brief/return?token=ORDER-3", follow_redirects=False)
    with SessionLocal() as db:
        assert db.scalar(select(Brief).where(Brief.user_id == uid)) is None
