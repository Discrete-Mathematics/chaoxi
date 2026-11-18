"""Run with python -m unittest test_payment. Uses an in-memory DB and fake Checkout."""
import hashlib
import hmac
import json
import sqlite3
import time
import unittest
import uuid
from unittest.mock import patch

import payment


class PaymentsTest(unittest.TestCase):
    def setUp(self):
        env = patch.dict("os.environ", {
            "STRIPE_SECRET_KEY": "sk_test_example", "STRIPE_WEBHOOK_SECRET": "whsec_local",
            "PUBLIC_BASE_URL": "http://127.0.0.1:8000",
        })
        env.start()
        self.addCleanup(env.stop)
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE users(id INTEGER PRIMARY KEY,balance_micro INTEGER);
            INSERT INTO users VALUES (1,0);
            CREATE TABLE ledger(user_id,type,amount_micro,mode,input,output,created_at);
        """)
        payment.init_payments(self.db)
        self.request_id = str(uuid.uuid4())
        checkout = patch.object(payment, "_stripe_checkout", return_value={
            "url": "https://checkout.stripe.com/c/pay/test",
            "expires_at": int(time.time()) + 3600, "id": "cs_test_example",
        })
        self.gateway = checkout.start()
        self.addCleanup(checkout.stop)
        self.order = payment.create_checkout(self.db, 1, 1000, self.request_id)
        self.session = {
            "metadata": {"app": payment.APPLICATION}, "id": "cs_test_example",
            "client_reference_id": self.order["order_id"], "livemode": False,
            "object": "checkout.session", "mode": "payment", "currency": "cny",
            "amount_total": 1000, "payment_status": "paid",
        }

    def notify(self, event_type="checkout.session.completed", skew=0, signature=None):
        body = json.dumps({"type": event_type, "livemode": False,
                           "data": {"object": self.session}}).encode()
        ts = str(int(time.time()) + skew)
        digest = hmac.new(b"whsec_local", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        return payment.handle_webhook(self.db, body, signature or f"t={ts},v1={digest}")

    def balance(self):
        return self.db.execute("SELECT balance_micro FROM users").fetchone()[0]

    def test_idempotent_creation_and_completion(self):
        self.assertEqual(self.order, payment.create_checkout(self.db, 1, 1000, self.request_id))
        self.assertEqual(self.gateway.call_count, 1)
        with self.assertRaises(payment.PaymentError):
            payment.create_checkout(self.db, 1, 3000, self.request_id)
        self.notify()
        self.notify("checkout.session.async_payment_succeeded")
        self.assertEqual(self.balance(), 10_000_000)
        self.assertEqual(self.db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0], 1)

    def test_unpaid_event(self):
        self.session["payment_status"] = "unpaid"
        self.notify()
        self.assertEqual(self.balance(), 0)
        self.session["payment_status"] = "paid"
        self.notify("checkout.session.async_payment_succeeded")
        self.assertEqual(self.balance(), 10_000_000)

    def test_rejects_invalid_payment(self):
        for field, bad_value in [("id","cs_wrong"), ("amount_total",1), ("currency","usd"),
                                 ("livemode",True), ("client_reference_id","unknown")]:
            with self.subTest(field=field), patch.dict(self.session, {field: bad_value}):
                with self.assertRaises(payment.PaymentError):
                    self.notify()
                self.assertEqual(self.balance(), 0)

    def test_rejects_expired_or_invalid_signature(self):
        for args in [{"skew": -600}, {"skew": 600}, {"signature": "v1=bad"}]:
            with self.subTest(args=args), self.assertRaises(payment.PaymentError):
                self.notify(**args)
        self.assertEqual(self.balance(), 0)

    def test_rollback_and_redelivery(self):
        self.db.execute("""CREATE TRIGGER fail BEFORE INSERT ON ledger
                           BEGIN SELECT RAISE(ABORT,'mock crash'); END""")
        with self.assertRaises(sqlite3.DatabaseError):
            self.notify()
        self.assertEqual(self.balance(), 0)
        self.db.execute("DROP TRIGGER fail")
        self.notify()
        self.assertEqual(self.balance(), 10_000_000)


if __name__ == "__main__":
    unittest.main()
