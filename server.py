"""潮汐 · zero-dependency local application server (Python 3.10+)."""
import argparse
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from contextlib import closing
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import payment
from chat_service import APIError, chat, route_config, yuan

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "dist"
MAX_BODY = 8 * 1024 * 1024
SESSION_TTL = 14 * 86400
PBKDF_ROUNDS = 600_000
AUTH_WINDOWS = {}
AUTH_LOCK = threading.Lock()


def load_env():
    path = ROOT / ".env"
    if path.is_file():
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                name, value = line.split("=",1)
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                    value = value[1:-1]
                os.environ.setdefault(name.strip(),value)


def database():
    path = Path(os.getenv("DATABASE_PATH") or ROOT / "data" / "chaoxi.sqlite3")
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def initialize():
    for mode in ("fast","economy"):
        route_config(mode)
    with closing(database()) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
          CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY, username TEXT UNIQUE COLLATE NOCASE NOT NULL,
            salt BLOB NOT NULL, password_hash BLOB NOT NULL,
            balance_micro INTEGER NOT NULL DEFAULT 0 CHECK(balance_micro>=0)
          );
          CREATE TABLE IF NOT EXISTS sessions (
            digest TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id), expires INTEGER NOT NULL
          );
          CREATE INDEX IF NOT EXISTS ix_session_expiry ON sessions(expires);
          CREATE TABLE IF NOT EXISTS ledger (
            id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
            type TEXT NOT NULL, amount_micro INTEGER NOT NULL, mode TEXT,
            input INTEGER DEFAULT 0, output INTEGER DEFAULT 0, created_at TEXT NOT NULL
          );
          CREATE INDEX IF NOT EXISTS ix_ledger_user ON ledger(user_id,id);
          CREATE TABLE IF NOT EXISTS requests (
            user_id INTEGER NOT NULL REFERENCES users(id), req_id TEXT NOT NULL, digest TEXT NOT NULL,
            started REAL NOT NULL, state TEXT NOT NULL, result TEXT, attempt TEXT, PRIMARY KEY(user_id,req_id)
          );
          CREATE UNIQUE INDEX IF NOT EXISTS ix_one_active ON requests(user_id) WHERE state='pending';
        """)
        if "attempt" not in {row["name"] for row in db.execute("PRAGMA table_info(requests)")}:
            db.execute("ALTER TABLE requests ADD COLUMN attempt TEXT")
        payment.init_payments(db)
        db.execute("DELETE FROM sessions WHERE expires<?",(time.time(),))


def password_digest(password,salt):
    return hashlib.pbkdf2_hmac("sha256",password.encode(),salt,PBKDF_ROUNDS)


def auth_limit(address):
    now = time.monotonic()
    with AUTH_LOCK:
        queue = AUTH_WINDOWS.setdefault(address,deque())
        while queue and queue[0] < now - 60:
            queue.popleft()
        if len(queue) >= 20:
            raise APIError(429,"操作过于频繁，请在一分钟后重试。")
        queue.append(now)
        if len(AUTH_WINDOWS)>2048:
            for key in list(AUTH_WINDOWS):
                if AUTH_WINDOWS[key][-1] < now-60:
                    del AUTH_WINDOWS[key]


def session_info(user):
    modes = {}
    for mode in ("economy","fast"):
        route = route_config(mode)
        modes[mode] = {"live":route["live"], "inputRate":float(route["incoming"]),
                       "outputRate":float(route["outgoing"]), "model":route["model"]}
    payments = payment.payment_enabled() and any(m["live"] for m in modes.values())
    return {"user":{"id":user["id"],"username":user["username"]} if user else None,
            "balance":yuan(user["balance_micro"]) if user else 0, "modes":modes,
            "paymentsEnabled":payments, "payments_enabled":payments}


class Handler(BaseHTTPRequestHandler):
    server_version = "Chaoxi"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def log_message(self, *_):
        # Avoid logging credentials, user content, or untrusted request text.
        pass

    def send_data(self,status,content,kind,cookie=None):
        self.send_response(status)
        self.send_header("Content-Type",kind)
        self.send_header("Content-Length",str(len(content)))
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Content-Type-Options","nosniff")
        self.send_header("Referrer-Policy","same-origin")
        self.send_header("X-Frame-Options","DENY")
        if cookie:
            secure = "; Secure" if os.getenv("PUBLIC_BASE_URL","").startswith("https://") else ""
            token,age = cookie
            self.send_header("Set-Cookie",f"chaoxi_sid={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax{secure}")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(content)

    def reply(self,status,data,cookie=None):
        self.send_data(status,json.dumps(data,ensure_ascii=False).encode(),"application/json; charset=utf-8",cookie)

    def digest_cookie(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie",""))
        except CookieError:
            return ""
        if "chaoxi_sid" in jar:
            token = jar["chaoxi_sid"].value
            if re.fullmatch("[a-zA-Z0-9_-]{43}",token):
                return hashlib.sha256(token.encode()).hexdigest()
        return ""

    def current_user(self,db):
        return db.execute("""SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id
                             WHERE digest=? AND expires>?""",(self.digest_cookie(),time.time())).fetchone()

    def login(self,db,body,register):
        auth_limit(self.client_address[0])
        username,password = body.get("username"),body.get("password")
        username = username.strip() if isinstance(username,str) else ""
        if not re.fullmatch(r"[\w.@-]{2,40}",username):
            raise APIError(400,"用户名需为 2–40 个字符，可使用汉字、字母、数字、下划线、点、短横线或 @。")
        if not isinstance(password,str) or not 8 <= len(password) <= 128:
            raise APIError(400,"密码长度需为 8–128 个字符。")
        try:
            password.encode()
        except UnicodeEncodeError:
            raise APIError(400,"密码中包含无效字符。")
        if register:
            salt = secrets.token_bytes(16)
            verifier = password_digest(password,salt)
            try:
                db.execute("INSERT INTO users(username,salt,password_hash) VALUES(?,?,?)",(username,salt,verifier))
            except sqlite3.IntegrityError:
                raise APIError(409,"这个用户名已被使用。")
        user = db.execute("SELECT * FROM users WHERE username=?",(username,)).fetchone()
        if not register:
            candidate = password_digest(password,user["salt"] if user else b"chaoxi_salt_stub")
            if not user or not hmac.compare_digest(candidate,user["password_hash"]):
                raise APIError(401,"用户名或密码不正确。")
        token = secrets.token_urlsafe(32)
        db.execute("BEGIN IMMEDIATE")
        db.execute("DELETE FROM sessions WHERE digest=? OR expires<?",(self.digest_cookie(),time.time()))
        db.execute("INSERT INTO sessions VALUES(?,?,?)",(hashlib.sha256(token.encode()).hexdigest(),
                   user["id"],int(time.time())+SESSION_TTL))
        db.commit()
        self.reply(200,session_info(user),(token,SESSION_TTL))

    def read_body(self):
        if "Transfer-Encoding" in self.headers:
            raise APIError(400,"请使用带长度的 JSON 请求。")
        length = self.headers.get("Content-Length","0")
        if not re.fullmatch(r"\d{1,10}",length):
            raise APIError(400,"请求长度无效。")
        if int(length)>MAX_BODY:
            raise APIError(413,"请求过大，请将聊天内容拆分后重试。")
        raw = self.rfile.read(int(length))
        if len(raw)!=int(length):
            raise APIError(400,"请求不完整。")
        return raw

    def serve_static(self,path):
        if self.command not in ("GET","HEAD"):
            raise APIError(405,"此资源仅接受读取请求。")
        requested = unquote(path).lstrip("/") or "index.html"
        if "\x00" in requested:
            raise APIError(400,"地址无效。")
        target = (STATIC / requested).resolve()
        if not target.is_relative_to(STATIC.resolve()) or not target.is_file():
            raise APIError(404,"未找到此页面。")
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if target.suffix == ".js":
            mime = "application/javascript"
        self.send_data(200,target.read_bytes(),mime)

    def route(self):
        host = self.headers.get("Host","").lower()
        public = urlsplit(os.getenv("PUBLIC_BASE_URL",""))
        allowed_hosts = {public.netloc.lower()} if public.netloc else set()
        port = self.server.server_address[1]
        for hostname in ("localhost","127.0.0.1","[::1]",self.server.server_address[0]):
            allowed_hosts.add(f"{hostname}:{port}")
            if port == 80:
                allowed_hosts.add(hostname)
        if host not in allowed_hosts:
            raise APIError(403,"主机名未配置。请通过启动时显示的地址访问。")
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            return self.serve_static(path)
        with closing(database()) as db:
            if self.command == "GET":
                user = self.current_user(db)
                if path in ("/api/session","/api/me"):
                    return self.reply(200,session_info(user))
                if path=="/api/ledger":
                    if not user:
                        return self.reply(200,{"items":[]})
                    rows = db.execute("SELECT * FROM ledger WHERE user_id=? ORDER BY id DESC LIMIT 500",(user["id"],))
                    return self.reply(200,{"items":[dict(id=r["id"],type="topup" if r["type"]=="recharge" else r["type"],amount=yuan(r["amount_micro"]),
                         mode=r["mode"],createdAt=r["created_at"],input=r["input"] or 0,output=r["output"] or 0) for r in rows]})
            if self.command == "POST":
                if path == "/api/payments/webhook":
                    data = payment.handle_webhook(db,self.read_body(),self.headers.get("Stripe-Signature",""))
                    return self.reply(200,data)
                origin = self.headers.get("Origin")
                origins = {"http://"+host}
                if public.netloc:
                    origins = {public.scheme + "://" + public.netloc}
                if origin and origin not in origins:
                    raise APIError(403,"请求来源不匹配。")
                if self.headers.get_content_type()!="application/json":
                    raise APIError(415,"请提交 JSON 格式的数据。")
                try:
                    body = json.loads(self.read_body())
                except (ValueError,RecursionError):
                    raise APIError(400,"JSON 格式无效。")
                if not isinstance(body,dict):
                    raise APIError(400,"JSON 顶层必须是对象。")
                if path in ("/api/register","/api/login"):
                    return self.login(db,body,path=="/api/register")
                if path=="/api/logout":
                    db.execute("DELETE FROM sessions WHERE digest=?",(self.digest_cookie(),))
                    return self.reply(200,session_info(None),("",0))
                user = self.current_user(db)
                if "expectedUserId" in body:
                    expected = body["expectedUserId"]
                    if expected is not None and type(expected) is not int:
                        raise APIError(400,"账号标识格式无效。")
                    if expected != (user["id"] if user else None):
                        raise APIError(409,"账号状态已改变，请刷新页面后再继续。")
                if path=="/api/chat":
                    return self.reply(200,chat(db,user,body))
                if path=="/api/payments/checkout":
                    if not user:
                        raise APIError(401,"请先登录再充值。")
                    if not session_info(user)["paymentsEnabled"]:
                        raise APIError(503,"当前工作空间尚未开通充值。")
                    return self.reply(200,payment.create_checkout(db,user["id"],body.get("amount_cents"),body.get("idempotency_key")))
        raise APIError(404,"未找到此接口。")

    def dispatch(self):
        try:
            self.route()
        except (APIError,payment.PaymentError) as exc:
            self.reply(exc.status,{"error":str(exc)})
        except (BrokenPipeError,ConnectionResetError,TimeoutError):
            return
        except Exception as exc:
            print(f"Request failed ({type(exc).__name__})",file=sys.stderr)
            self.reply(500,{"error":"服务暂时不可用，请稍后重试。"})

    do_GET = do_HEAD = do_POST = dispatch


def main():
    load_env()
    parser = argparse.ArgumentParser(description="潮汐聊天服务")
    parser.add_argument("--host",default="127.0.0.1")
    parser.add_argument("--port",type=int,default=8765)
    args = parser.parse_args()
    initialize()
    with ThreadingHTTPServer((args.host,args.port),Handler) as httpd:
        print(f"Chaoxi: http://{args.host}:{httpd.server_port}",flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
