import asyncio
import json
import logging
import math
from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import ai, news
from .config import get_settings
from .db import Article, Base, Brief, Payment, User, WebhookEvent, engine, get_db, utcnow
from .paypal import PayPalClient, PayPalError, approval_link
from .security import hash_password, verify_password

settings = get_settings()
log = logging.getLogger("marketpulse")
BASE_DIR = Path(__file__).parent
paypal = PayPalClient(settings)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


def ago(dt) -> str:
    mins = int((utcnow() - dt).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 1440:
        return f"{mins // 60} h ago"
    return f"{mins // 1440} d ago"


templates.env.filters["ago"] = ago


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(engine)
    if settings.secret_key == "change-me":
        log.warning("SECRET_KEY is the default value. Set a long random string before deploying.")
    if settings.seed_demo:
        news.seed_demo()
    task = asyncio.create_task(news.poll_forever()) if settings.poll_enabled else None
    yield
    if task:
        task.cancel()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.base_url.startswith("https"),
    max_age=60 * 60 * 24 * 14,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


# ---- helpers -------------------------------------------------------------------------
class LoginRequired(Exception):
    pass


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def flash(request: Request, message: str, kind: str = "info") -> None:
    request.session["flash"] = {"message": message, "kind": kind}


def render(request: Request, name: str, user: User | None, status_code: int = 200, **ctx):
    now = utcnow()
    ctx.update(user=user, settings=settings, flash=request.session.pop("flash", None),
               today=f"{now:%A}, {now.day} {now:%B %Y}")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    return db.get(User, uid) if uid else None


def require_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = current_user(request, db)
    if not user:
        flash(request, "Log in to continue.")
        raise LoginRequired()
    return user


@app.exception_handler(LoginRequired)
async def login_required_handler(request: Request, exc: LoginRequired):
    return go("/login")


def is_pro(user: User | None) -> bool:
    return bool(user and user.plan == "pro")


def unlock_time(article: Article):
    return article.published_at + timedelta(minutes=settings.feed_delay_minutes)


def is_locked(article: Article, user: User | None) -> bool:
    return not is_pro(user) and unlock_time(article) > utcnow()


def minutes_until_unlock(article: Article) -> int:
    return max(1, math.ceil((unlock_time(article) - utcnow()).total_seconds() / 60))


# ---- pages ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, category: str | None = None, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    query = select(Article).order_by(Article.published_at.desc()).limit(60)
    if category in ai.CATEGORIES:
        query = query.where(Article.category == category)
    articles = db.scalars(query).all()
    owned = set(db.scalars(select(Brief.article_id).where(Brief.user_id == user.id))) if user else set()
    rows = [
        {"a": a, "locked": is_locked(a, user), "unlocks_in": minutes_until_unlock(a), "owned": a.id in owned}
        for a in articles
    ]

    since = utcnow() - timedelta(hours=24)
    counts = dict(
        db.execute(select(Article.sentiment, func.count()).where(Article.published_at >= since).group_by(Article.sentiment)).all()
    )
    total = sum(counts.values())
    pulse = {"total": total}
    for key in ai.SENTIMENTS:
        pulse[key] = counts.get(key, 0)
        pulse[f"{key}_pct"] = round(100 * counts.get(key, 0) / total) if total else 0

    briefs = []
    if user:
        briefs = db.execute(
            select(Brief, Article).join(Article, Article.id == Brief.article_id)
            .where(Brief.user_id == user.id).order_by(Brief.created_at.desc()).limit(10)
        ).all()
    open_rows = [r for r in rows if not r["locked"]]
    return render(
        request, "dashboard.html", user, pulse=pulse, categories=ai.CATEGORIES, active=category, briefs=briefs,
        locked=[r for r in rows if r["locked"]],
        lead=open_rows[0] if open_rows else None,
        secondary=open_rows[1:3],
        latest=open_rows[3:8],
        more=open_rows[8:],
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok"}


@app.get("/api/articles")
def api_articles(category: str | None = None, user: User | None = Depends(current_user), db: Session = Depends(get_db)):
    """JSON feed. Locked stories expose no headline or text, only metadata."""
    query = select(Article).order_by(Article.published_at.desc()).limit(60)
    if category in ai.CATEGORIES:
        query = query.where(Article.category == category)
    out = []
    for a in db.scalars(query):
        item = {"id": a.id, "category": a.category, "sentiment": a.sentiment, "published_at": a.published_at.isoformat()}
        if is_locked(a, user):
            item.update(locked=True, unlocks_in_minutes=minutes_until_unlock(a))
        else:
            item.update(locked=False, title=a.title, insight=a.insight or a.summary, source=a.source, link=a.link)
        out.append(item)
    return out


# ---- auth ----------------------------------------------------------------------------
@app.get("/signup", response_class=HTMLResponse)
def signup_page(request: Request, user: User | None = Depends(current_user)):
    return render(request, "auth.html", user, mode="signup", error=None)


@app.post("/signup")
def signup(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    email = email.strip().lower()
    error = None
    if "@" not in email or len(email) > 255:
        error = "Enter a valid email address."
    elif not 8 <= len(password) <= 64:
        error = "Use a password between 8 and 64 characters."
    elif db.scalar(select(User).where(User.email == email)):
        error = "An account with this email already exists. Log in instead."
    if error:
        return render(request, "auth.html", None, status_code=400, mode="signup", error=error)
    user = User(email=email, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    request.session["uid"] = user.id
    flash(request, "Account created. Welcome to MarketPulse AI.", "success")
    return go("/")


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(current_user)):
    return render(request, "auth.html", user, mode="login", error=None)


@app.post("/login")
def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(User.email == email.strip().lower()))
    if not user or not verify_password(password, user.password_hash):
        return render(request, "auth.html", None, status_code=400, mode="login", error="Email or password is incorrect.")
    request.session["uid"] = user.id
    return go("/")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return go("/")


# ---- Pro subscription (PayPal Subscriptions API) -------------------------------------
@app.post("/subscribe")
async def subscribe(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if is_pro(user):
        flash(request, "You already have Pro.")
        return go("/")
    if not settings.paypal_plan_id:
        flash(request, "Subscriptions are not set up yet. Run scripts/setup_paypal.py first.", "error")
        return go("/")
    try:
        sub = await paypal.create_subscription(
            settings.paypal_plan_id, user.id, f"{settings.base_url}/subscribe/return", f"{settings.base_url}/"
        )
        url = approval_link(sub)
    except PayPalError:
        flash(request, "We couldn't reach PayPal. Try again in a moment.", "error")
        return go("/")
    user.paypal_subscription_id = sub["id"]
    user.subscription_status = sub.get("status")
    db.commit()
    return go(url)


@app.get("/subscribe/return")
async def subscribe_return(request: Request, subscription_id: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    if not subscription_id or user.paypal_subscription_id != subscription_id:
        flash(request, "That subscription doesn't belong to your account.", "error")
        return go("/")
    try:
        sub = await paypal.get_subscription(subscription_id)
    except PayPalError:
        flash(request, "We couldn't confirm your subscription yet. Refresh in a moment.", "error")
        return go("/")
    user.subscription_status = sub.get("status")
    if sub.get("status") == "ACTIVE":
        user.plan = "pro"
        flash(request, "You're on Pro. Every story is now unlocked in real time.", "success")
    else:
        flash(request, "PayPal is still confirming your payment. Pro turns on as soon as it does.")
    db.commit()
    return go("/")


@app.post("/subscription/cancel")
async def subscription_cancel(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    if user.paypal_subscription_id and is_pro(user):
        try:
            await paypal.cancel_subscription(user.paypal_subscription_id, "Cancelled by the customer")
        except PayPalError:
            flash(request, "We couldn't cancel with PayPal. Try again in a moment.", "error")
            return go("/")
        user.plan, user.subscription_status = "free", "CANCELLED"
        db.commit()
        flash(request, "Your Pro subscription is cancelled.", "success")
    return go("/")


# ---- Deep Brief (PayPal Orders API) --------------------------------------------------
def _parse_capture(order: dict) -> dict | None:
    try:
        unit = order["purchase_units"][0]
        cap = unit["payments"]["captures"][0]
        return {
            "capture_id": cap["id"],
            "status": cap["status"],
            "amount": cap["amount"]["value"],
            "currency": cap["amount"]["currency_code"],
            "custom_id": cap.get("custom_id") or unit.get("custom_id") or "",
        }
    except (KeyError, IndexError, TypeError):
        return None


async def fulfil_brief(db: Session, *, capture_id: str, status: str, amount: str, currency: str, custom_id: str) -> Brief | None:
    """Idempotent: record the payment and create the brief. Safe to call from the return URL and the webhook."""
    if status != "COMPLETED":
        return None
    try:
        user_id, article_id = (int(x) for x in custom_id.split(":"))
        paid_ok = Decimal(amount) == Decimal(settings.brief_price_usd) and currency == "USD"
    except (ValueError, InvalidOperation):
        return None
    article = db.get(Article, article_id)
    if not paid_ok or not article or not db.get(User, user_id):
        log.warning("Rejected capture %s (custom_id=%s amount=%s %s)", capture_id, custom_id, amount, currency)
        return None

    if not db.scalar(select(Payment).where(Payment.paypal_id == capture_id)):
        db.add(Payment(user_id=user_id, kind="brief", paypal_id=capture_id, amount=amount, currency=currency, article_id=article_id))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()

    brief = db.scalar(select(Brief).where(Brief.user_id == user_id, Brief.article_id == article_id))
    if not brief:
        sections = await ai.generate_brief(article.title, article.summary, article.category)
        brief = Brief(user_id=user_id, article_id=article_id, content=json.dumps(sections))
        db.add(brief)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            brief = db.scalar(select(Brief).where(Brief.user_id == user_id, Brief.article_id == article_id))
    return brief


@app.post("/brief/{article_id:int}/buy")
async def brief_buy(article_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    article = db.get(Article, article_id)
    if not article or is_locked(article, user):
        raise HTTPException(404, "Story not found")
    if db.scalar(select(Brief).where(Brief.user_id == user.id, Brief.article_id == article_id)):
        return go(f"/brief/{article_id}")
    try:
        order = await paypal.create_order(
            amount=settings.brief_price_usd,
            currency="USD",
            custom_id=f"{user.id}:{article.id}",
            description=f"Deep brief: {article.title[:90]}",
            return_url=f"{settings.base_url}/brief/return",
            cancel_url=f"{settings.base_url}/",
        )
        url = approval_link(order)
    except PayPalError:
        flash(request, "We couldn't reach PayPal. Try again in a moment.", "error")
        return go("/")
    return go(url)


@app.get("/brief/return")
async def brief_return(request: Request, token: str = "", user: User = Depends(require_user), db: Session = Depends(get_db)):
    """PayPal sends the buyer back here with ?token=<order id>. Capture the order and unlock the brief."""
    try:
        capture = _parse_capture(await paypal.capture_order(token))
    except PayPalError:
        flash(request, "We couldn't complete that payment. You haven't been charged.", "error")
        return go("/")
    if not capture or not capture["custom_id"].startswith(f"{user.id}:"):
        flash(request, "That payment doesn't belong to your account.", "error")
        return go("/")
    brief = await fulfil_brief(db, **capture)
    if not brief:
        flash(request, "PayPal hasn't confirmed that payment yet. Your brief will appear once it does.")
        return go("/")
    flash(request, "Payment received. Your deep brief is ready.", "success")
    return go(f"/brief/{brief.article_id}")


@app.get("/brief/{article_id:int}", response_class=HTMLResponse)
def brief_view(article_id: int, request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    brief = db.scalar(select(Brief).where(Brief.user_id == user.id, Brief.article_id == article_id))
    if not brief:
        flash(request, "Buy the deep brief to read it.")
        return go("/")
    sections = json.loads(brief.content)
    return render(request, "brief.html", user, article=db.get(Article, article_id),
                  sections=[(label, sections.get(key, "")) for key, label in ai.BRIEF_SECTIONS])


# ---- PayPal webhooks -----------------------------------------------------------------
def _set_plan(db: Session, subscription_id: str, custom_id: str | None, plan: str, status: str | None) -> None:
    user = db.scalar(select(User).where(User.paypal_subscription_id == subscription_id))
    if not user and custom_id and custom_id.isdigit():
        user = db.get(User, int(custom_id))
        if user:
            user.paypal_subscription_id = subscription_id
    if user:
        user.plan, user.subscription_status = plan, status
        db.commit()


async def handle_event(db: Session, event: dict) -> None:
    kind, res = event.get("event_type"), event.get("resource", {})
    if kind == "BILLING.SUBSCRIPTION.ACTIVATED":
        _set_plan(db, res["id"], res.get("custom_id"), "pro", "ACTIVE")
    elif kind in ("BILLING.SUBSCRIPTION.CANCELLED", "BILLING.SUBSCRIPTION.SUSPENDED", "BILLING.SUBSCRIPTION.EXPIRED"):
        _set_plan(db, res["id"], res.get("custom_id"), "free", kind.rsplit(".", 1)[1])
    elif kind == "PAYMENT.SALE.COMPLETED":
        user = db.scalar(select(User).where(User.paypal_subscription_id == res.get("billing_agreement_id")))
        if user and not db.scalar(select(Payment).where(Payment.paypal_id == res["id"])):
            amount = res.get("amount", {})
            db.add(Payment(user_id=user.id, kind="subscription", paypal_id=res["id"],
                           amount=amount.get("total", "0"), currency=amount.get("currency", "USD")))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
    elif kind == "PAYMENT.CAPTURE.COMPLETED" and ":" in (res.get("custom_id") or ""):
        amount = res.get("amount", {})
        await fulfil_brief(db, capture_id=res["id"], status=res.get("status", ""), amount=amount.get("value", "0"),
                           currency=amount.get("currency_code", ""), custom_id=res["custom_id"])


@app.post("/webhooks/paypal")
async def paypal_webhook(request: Request, db: Session = Depends(get_db)):
    try:
        event = json.loads(await request.body())
    except ValueError:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)
    try:
        verified = await paypal.verify_webhook(request.headers, event)
    except PayPalError:
        return JSONResponse({"error": "verification unavailable"}, status_code=503)  # PayPal will retry
    if not verified:
        return JSONResponse({"error": "invalid signature"}, status_code=400)

    event_id = event.get("id", "")
    if not event_id or db.get(WebhookEvent, event_id):
        return {"status": "duplicate"}
    await handle_event(db, event)
    db.add(WebhookEvent(event_id=event_id, event_type=event.get("event_type", "")))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return {"status": "ok"}