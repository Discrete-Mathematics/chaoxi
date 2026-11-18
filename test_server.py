"""Offline HTTP integration checks: auth, demo, paid routing, concurrency, and settlement."""
import http.client
import hmac
import json
import os
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

import server


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.append((self.path, self.headers["Authorization"], payload))
        self.server.started.set()
        self.server.release.wait(8)
        result = {"choices":[{"message":{"content":"测试回复"}}],"usage":{"prompt_tokens":21,"completion_tokens":11}}
        if self.server.bad:
            del result["usage"]
        data = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Length",str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self,*_):
        pass


class ServiceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.upstream = ThreadingHTTPServer(("127.0.0.1",0),Provider)
        cls.upstream.calls = []
        cls.upstream.started = threading.Event()
        cls.upstream.release = threading.Event()
        cls.upstream.release.set()
        cls.upstream.bad = False
        provider_url = f"http://127.0.0.1:{cls.upstream.server_port}"
        config = patch.dict(os.environ,{"DATABASE_PATH":os.path.join(cls.tmp.name,"test.sqlite3"),
                          "FAST_BASE_URL":provider_url,"FAST_API_KEY":"test-key","FAST_MODEL":"mock",
                          "FAST_INPUT_RATE":"3","FAST_OUTPUT_RATE":"6","ECONOMY_API_KEY":"","PUBLIC_BASE_URL":""})
        config.start()
        cls.addClassCleanup(config.stop)
        server.initialize()
        cls.srv = ThreadingHTTPServer(("127.0.0.1",0),server.Handler)
        for httpd in (cls.upstream,cls.srv):
            threading.Thread(target=httpd.serve_forever,daemon=True).start()
            cls.addClassCleanup(httpd.server_close)
            cls.addClassCleanup(httpd.shutdown)

    def request(self,method,path,body=None,cookie=None,headers=None):
        fields = {"Content-Type":"application/json"}
        if cookie:
            fields["Cookie"] = cookie
        fields.update(headers or {})
        with closing(http.client.HTTPConnection("127.0.0.1",self.srv.server_port,timeout=15)) as conn:
            conn.request(method,path,json.dumps(body) if body is not None else None,fields)
            res = conn.getresponse()
            return res.status,json.loads(res.read()),(res.getheader("Set-Cookie") or "").split(";")[0]

    @staticmethod
    def body(mode="fast"):
        return {"requestId":str(uuid.uuid4()),"mode":mode,"messages":[{"role":"user","content":"你好"}]}

    def register(self):
        fields = {"username":uuid.uuid4().hex,"password":"password1234"}
        status,session,cookie = self.request("POST","/api/register",fields)
        self.assertEqual(status,200)
        return session["user"]["id"],cookie,fields

    def credit(self,uid):
        with closing(server.database()) as db:
            db.execute("UPDATE users SET balance_micro=1000000 WHERE id=?",(uid,))

    def test_auth_and_csrf(self):
        _,cookie,fields = self.register()
        self.assertEqual(self.request("POST","/api/logout",{},cookie,{"Origin":"https://foreign.example"})[0],403)
        self.assertEqual(self.request("GET","/api/session",cookie=cookie)[1]["user"]["username"],fields["username"])
        self.assertEqual(self.request("POST","/api/logout",{},cookie)[0],200)
        self.assertIsNone(self.request("GET","/api/session",cookie=cookie)[1]["user"])
        self.assertEqual(self.request("POST","/api/login",fields)[0],200)
        self.assertEqual(self.request("POST","/api/register",fields)[0],409)
        self.assertEqual(self.request("POST","/api/login",{**fields,"password":"wrongpassword"})[0],401)

    def test_demo_and_quota(self):
        _,cookie,_ = self.register()
        calls = len(self.upstream.calls)
        self.assertTrue(self.request("POST","/api/chat",self.body())[1]["demo"])
        self.assertTrue(self.request("POST","/api/chat",self.body("economy"),cookie)[1]["demo"])
        self.assertEqual(self.request("POST","/api/chat",self.body(),cookie)[0],402)
        self.assertEqual(calls,len(self.upstream.calls))
        self.assertEqual(self.request("GET","/api/ledger",cookie=cookie)[1]["items"],[])

    def test_checkout_webhook_and_ledger(self):
        config = {"STRIPE_SECRET_KEY":"sk_test_local","STRIPE_WEBHOOK_SECRET":"whsec_http",
                  "PUBLIC_BASE_URL":f"http://127.0.0.1:{self.srv.server_port}"}
        test_session = {"url":"https://checkout.stripe.com/c/pay/test",
                        "id":"cs_test_http","expires_at":int(time.time())+1800}
        with patch.dict(os.environ,config), patch("payment._stripe_checkout",return_value=test_session):
            uid,cookie,_ = self.register()
            self.assertTrue(self.request("GET","/api/session",cookie=cookie)[1]["paymentsEnabled"])
            order_body = {"amount_cents":1000,"idempotency_key":str(uuid.uuid4()),"expectedUserId":uid}
            status,order,_ = self.request("POST","/api/payments/checkout",order_body,cookie)
            self.assertEqual(status,200)
            self.assertEqual(order["url"],test_session["url"])
            event = {"type":"checkout.session.completed","livemode":False,"data":{"object":{
                "id":test_session["id"],"metadata":{"app":"chaoxi-credit-v1"},
                "client_reference_id":order["order_id"],"amount_total":1000,"currency":"cny",
                "livemode":False,"mode":"payment","object":"checkout.session","payment_status":"paid"}}}
            stamp = str(int(time.time()))
            digest = hmac.digest(b"whsec_http",(stamp+"."+json.dumps(event)).encode(),"sha256").hex()
            signature = {"Stripe-Signature":f"t={stamp},v1={digest}"}
            for attempt in range(2):
                with self.subTest(attempt=attempt):
                    self.assertEqual(self.request("POST","/api/payments/webhook",event,headers=signature)[0],200)
            self.assertEqual(self.request("GET","/api/me",cookie=cookie)[1]["balance"],10)
            rows = self.request("GET","/api/ledger",cookie=cookie)[1]["items"]
            self.assertEqual([(r["type"],r["amount"]) for r in rows],[("topup",10)])

    def test_paid_idempotent_and_concurrent(self):
        uid,cookie,_ = self.register()
        self.credit(uid)
        body = self.body()
        calls = len(self.upstream.calls)
        self.upstream.release.clear()
        self.upstream.started.clear()
        initial = []
        worker = threading.Thread(target=lambda:initial.append(self.request("POST","/api/chat",body,cookie)))
        worker.start()
        try:
            self.assertTrue(self.upstream.started.wait(4))
            self.assertEqual(self.request("POST","/api/chat",self.body(),cookie)[0],409)
        finally:
            self.upstream.release.set()
            worker.join(8)
        self.assertEqual(initial[0][0],200)
        result = initial[0][1]
        self.assertFalse(result["demo"])
        self.assertEqual(result["usage"],{"input":21,"output":11})
        self.assertEqual(result["cost"],0.000129)
        self.assertEqual(result["balance"],0.999871)
        self.assertEqual(self.request("POST","/api/chat",body,cookie)[1],result)
        changed = {**body,"messages":[{"role":"user","content":"另一条消息"}]}
        self.assertEqual(self.request("POST","/api/chat",changed,cookie)[0],409)
        self.assertEqual(len(self.upstream.calls),calls+1)
        self.assertEqual(self.upstream.calls[-1][0:2],("/chat/completions","Bearer test-key"))
        self.assertEqual(self.upstream.calls[-1][2]["model"],"mock")
        self.assertEqual(self.request("GET","/api/ledger",cookie=cookie)[1]["items"][0]["amount"],-0.000129)
        self.assertEqual(self.request("GET","/api/ledger")[1]["items"],[])

    def test_invalid_usage_never_charged(self):
        uid,cookie,_ = self.register()
        self.credit(uid)
        body = self.body()
        self.upstream.bad = True
        try:
            self.assertEqual(self.request("POST","/api/chat",body,cookie)[0],502)
        finally:
            self.upstream.bad = False
        self.assertEqual(self.request("GET","/api/session",cookie=cookie)[1]["balance"],1)
        self.assertEqual(self.request("GET","/api/ledger",cookie=cookie)[1]["items"],[])
        self.assertEqual(self.request("POST","/api/chat",body,cookie)[0],200)
        self.assertEqual(len(self.request("GET","/api/ledger",cookie=cookie)[1]["items"]),1)

    def test_expected_user_is_checked_before_billing_or_checkout(self):
        uid,cookie,_ = self.register()
        self.credit(uid)
        calls = len(self.upstream.calls)
        for expected,session in [(None,cookie),(uid+1,cookie),(uid,None)]:
            self.assertEqual(self.request("POST","/api/chat",{**self.body(),"expectedUserId":expected},session)[0],409)
            self.assertEqual(self.request("POST","/api/payments/checkout",{"expectedUserId":expected},session)[0],409)
        self.assertEqual(len(self.upstream.calls),calls)
        self.assertEqual(self.request("POST","/api/chat",{**self.body(),"expectedUserId":uid},cookie)[0],200)

    def test_superseded_attempt_cannot_settle(self):
        uid,cookie,_ = self.register()
        self.credit(uid)
        body = self.body()
        first,second = [],[]
        calls = len(self.upstream.calls)
        self.upstream.release.clear()
        self.upstream.started.clear()
        def issue(out):
            out.append(self.request("POST","/api/chat",body,cookie))
        old = threading.Thread(target=issue,args=(first,))
        new = threading.Thread(target=issue,args=(second,))
        old.start()
        try:
            self.assertTrue(self.upstream.started.wait(3))
            with closing(server.database()) as db:
                db.execute("UPDATE requests SET started=0 WHERE user_id=? AND req_id=?",(uid,body["requestId"]))
            self.upstream.started.clear()
            new.start()
            self.assertTrue(self.upstream.started.wait(3))
        finally:
            self.upstream.release.set()
            old.join(8)
            if new.ident is not None:
                new.join(8)
        self.assertEqual(first[0][0],409)
        self.assertEqual(second[0][0],200)
        self.assertEqual(self.request("POST","/api/chat",body,cookie)[1],second[0][1])
        self.assertEqual(len(self.upstream.calls),calls+2)
        self.assertEqual(len(self.request("GET","/api/ledger",cookie=cookie)[1]["items"]),1)


if __name__ == "__main__":
    unittest.main()
