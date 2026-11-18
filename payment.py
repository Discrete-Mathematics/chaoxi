"""Prepaid CNY credits through Stripe Checkout. Amounts at the API boundary are fen."""
import hashlib
import hmac
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


AMOUNTS = (1000, 3000, 10000)
APPLICATION = "chaoxi-credit-v1"
PAID_EVENTS = {"checkout.session.completed", "checkout.session.async_payment_succeeded"}


class PaymentError(ValueError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _config():
    key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    base = os.getenv("PUBLIC_BASE_URL", "").strip().rstrip("/")
    try:
        url = urlsplit(base)
        live = key.startswith(("sk_live_", "rk_live_"))
        test = key.startswith(("sk_test_", "rk_test_"))
        local = url.hostname in {"127.0.0.1", "localhost", "::1"}
        if (not live and not test) or not secret.startswith("whsec_"):
            return None
        if not url.hostname or url.username or url.password or url.query or url.fragment or url.path:
            return None
        # A test webhook never creates spendable credit on a public service.
        if test and not local:
            return None
        if url.scheme != "https" and not (test and local and url.scheme == "http"):
            return None
    except ValueError:
        return None
    return key, secret, base, live


def payment_enabled():
    return _config() is not None


def init_payments(db):
    db.execute("""CREATE TABLE IF NOT EXISTS payment_orders (
        id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
        request_key TEXT NOT NULL, amount_cents INTEGER NOT NULL,
        live INTEGER NOT NULL, created INTEGER NOT NULL,
        session_id TEXT UNIQUE, checkout_url TEXT, expires INTEGER, paid_at TEXT,
        UNIQUE (user_id, request_key)
    )""")


@contextmanager
def _atomic(db):
    db.execute("BEGIN IMMEDIATE")
    try:
        yield
        db.commit()
    except BaseException:
        db.rollback()
        raise


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _stripe_checkout(key, base, order):
    amount = order["amount_cents"]
    order_id = order["id"]
    fields = {
        "mode": "payment", "locale": "zh", "payment_method_types[0]": "card",
        "success_url": base + "/?payment=return", "cancel_url": base + "/?payment=cancel",
        "client_reference_id": order_id, "metadata[app]": APPLICATION,
        "line_items[0][quantity]": 1, "adaptive_pricing[enabled]": "false",
        "line_items[0][price_data][currency]": "cny",
        "line_items[0][price_data][unit_amount]": amount,
        "line_items[0][price_data][product_data][name]": f"潮汐账户充值 · {amount // 100} 元",
    }
    req = Request("https://api.stripe.com/v1/checkout/sessions", urlencode(fields).encode(),
                  headers={"Authorization": "Bearer " + key, "Idempotency-Key": order_id,
                           "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with build_opener(_NoRedirect()).open(req, timeout=20) as response:
            result = json.loads(response.read(1_048_576))
        if (not result["id"].startswith("cs_") or
                urlsplit(result["url"]).netloc != "checkout.stripe.com" or
                urlsplit(result["url"]).scheme != "https" or
                type(result["expires_at"]) is not int):
            raise ValueError()
        return result
    except (URLError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PaymentError(502, "暂时无法创建支付订单，请稍后重试。") from exc


def create_checkout(db, user_id, amount_cents, idempotency_key):
    cfg = _config()
    if not cfg:
        raise PaymentError(503, "当前服务尚未开放充值。")
    if type(amount_cents) is not int or amount_cents not in AMOUNTS:
        raise PaymentError(400, "请选择 10 元、30 元或 100 元充值。")
    try:
        request_key = str(uuid.UUID(str(idempotency_key)))
    except (ValueError, AttributeError):
        raise PaymentError(400, "订单标识无效，请重新打开充值窗口。") from None
    key, _, base, live = cfg
    now = int(time.time())
    with _atomic(db):
        db.execute("""INSERT INTO payment_orders (id,user_id,request_key,amount_cents,live,created)
                      VALUES (?,?,?,?,?,?) ON CONFLICT(user_id,request_key) DO NOTHING""",
                   (uuid.uuid4().hex, user_id, request_key, amount_cents, int(live), now))
        order = db.execute("SELECT * FROM payment_orders WHERE user_id=? AND request_key=?",
                           (user_id, request_key)).fetchone()
    if order["amount_cents"] != amount_cents or order["live"] != int(live):
        raise PaymentError(409, "订单参数已变更，请重新选择金额。")
    if order["paid_at"]:
        raise PaymentError(409, "该订单已付款，请刷新余额。")
    if order["checkout_url"] and order["expires"] > now:
        return {"url": order["checkout_url"], "order_id": order["id"]}
    # Stripe may discard old idempotency keys. A new intent must get a new order.
    if order["checkout_url"] or now - order["created"] > 23 * 3600:
        raise PaymentError(409, "订单已过期，请重新发起充值。")
    checkout = _stripe_checkout(key, base, order)
    with _atomic(db):
        saved = db.execute("""UPDATE payment_orders SET session_id=?,checkout_url=?,expires=?
                              WHERE id=? AND (session_id IS NULL OR session_id=?)""",
                           (checkout["id"], checkout["url"], checkout["expires_at"],
                            order["id"], checkout["id"]))
        if saved.rowcount != 1:
            raise PaymentError(409, "订单状态已更新，请刷新余额。")
    return {"url": checkout["url"], "order_id": order["id"]}


def _verify(raw, header, secret):
    parts = [item.strip().partition("=") for item in (header or "").split(",")]
    timestamps = [v for k, _, v in parts if k == "t"]
    try:
        if len(timestamps) != 1 or abs(time.time() - int(timestamps[0])) > 300:
            return False
        digest = hmac.new(secret.encode(), timestamps[0].encode("ascii") + b"." + raw,
                          hashlib.sha256).hexdigest()
        return any(hmac.compare_digest(v, digest) for k, _, v in parts if k == "v1")
    except (ValueError, TypeError, UnicodeError):
        return False


def handle_webhook(db, raw_body, signature):
    cfg = _config()
    if not cfg:
        raise PaymentError(503, "支付服务未配置。")
    _, secret, _, live = cfg
    if not _verify(raw_body, signature, secret):
        raise PaymentError(400, "回调签名无效。")
    try:
        event = json.loads(raw_body)
        if event["type"] not in PAID_EVENTS:
            return {"received": True}
        session = event["data"]["object"]
        if not isinstance(session, dict):
            raise TypeError()
        metadata = session.get("metadata") or {}
        if not isinstance(metadata, dict) or metadata.get("app") != APPLICATION:
            return {"received": True}
        order_id = session["client_reference_id"]
        session_id = session["id"]
        amount = session["amount_total"]
        if (event["livemode"] is not live or session["livemode"] is not live
                or session["object"] != "checkout.session" or session["mode"] != "payment"
                or session["currency"] != "cny" or type(amount) is not int
                or not isinstance(order_id, str) or not isinstance(session_id, str)
                or not session_id.startswith("cs_")):
            raise ValueError()
        if session["payment_status"] != "paid":
            return {"received": True}
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise PaymentError(400, "回调订单参数不符。") from exc
    with _atomic(db):
        order = db.execute("SELECT * FROM payment_orders WHERE id=?", (order_id,)).fetchone()
        if (not order or order["session_id"] not in (None, session_id)
                or order["live"] != int(live) or order["amount_cents"] != amount):
            raise PaymentError(409, "回调与充值订单不匹配。")
        if not order["paid_at"]:
            paid_at = datetime.now(timezone.utc).isoformat()
            credit = amount * 10_000
            updated = db.execute("UPDATE users SET balance_micro=balance_micro+? WHERE id=?",
                                 (credit, order["user_id"]))
            if updated.rowcount != 1:
                raise PaymentError(409, "找不到充值账户。")
            db.execute("""INSERT INTO ledger (user_id,type,amount_micro,mode,input,output,created_at)
                          VALUES (?,'recharge',?,'stripe',0,0,?)""",
                       (order["user_id"], credit, paid_at))
            db.execute("UPDATE payment_orders SET session_id=?,paid_at=? WHERE id=?",
                       (session_id, paid_at, order_id))
    return {"received": True}
