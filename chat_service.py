"""Chat routes, metering, and retry-safe settlement. Currency uses integer micro-yuan."""
import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def yuan(micro):
    return micro / 1_000_000


def route_config(mode):
    prefix = mode.upper()
    fast = mode == "fast"
    key = os.getenv(prefix + "_API_KEY") or (os.getenv("DEEPSEEK_API_KEY") if fast else "")
    base = os.getenv(prefix + "_BASE_URL") or ("https://api.deepseek.com" if fast else "")
    model = os.getenv(prefix + "_MODEL") or ("deepseek-flash" if fast else "")
    def rate(name, default):
        try:
            value = Decimal(os.getenv(prefix + "_" + name) or default)
            if not value.is_finite() or not Decimal("0.000001") <= value <= 10000:
                raise ValueError
            return value.quantize(Decimal("0.000001"))
        except (InvalidOperation, ValueError):
            raise ValueError(f"{prefix}_{name} 的取值应在 0.000001 到 10000 之间")
    incoming = rate("INPUT_RATE", "3" if fast else "1")
    outgoing = rate("OUTPUT_RATE", "6" if fast else "2")
    host = urlsplit(base)
    secure = host.scheme == "https" or (host.scheme == "http" and host.hostname in ("localhost","127.0.0.1","::1"))
    if base and (not secure or not host.hostname or host.username or host.password or host.query or host.fragment):
        raise ValueError(f"{prefix}_BASE_URL 需要有效的 HTTPS 地址，本机开发服务可用 HTTP")
    return dict(live=bool(key and base and model), base=base.rstrip("/"), model=model,
                key=key, incoming=incoming, outgoing=outgoing)


def cost_micro(route, tokens_in, tokens_out):
    return int((tokens_in * route["incoming"] + tokens_out * route["outgoing"]).to_integral_value(rounding=ROUND_CEILING))


def sanitize_messages(raw):
    if not isinstance(raw, list) or not 1 <= len(raw) <= 4096:
        raise APIError(400, "消息列表应包含 1 到 4096 条消息。")
    messages = []
    for item in raw:
        if not isinstance(item, dict) or item.get("role") not in ("user", "assistant"):
            raise APIError(400, "消息角色或格式无效。")
        content = item.get("content")
        if not isinstance(content, str) or not content.strip():
            raise APIError(400, "不能发送空消息。")
        try:
            content.encode("utf-8")
        except UnicodeEncodeError:
            raise APIError(400, "消息中包含无法编码的字符。")
        messages.append({"role": item["role"], "content": content})
    if messages[-1]["role"] != "user":
        raise APIError(400, "最后一条消息应由你发送。")
    return messages


def demo_reply(prompt):
    if any(w in prompt for w in ("烦", "压力", "倾诉", "聊聊", "心情", "陪", "难过", "焦虑")):
        return "如果愿意，先说说今天让你最挂心的一件事。你希望有人听你讲，还是一起想个具体办法？"
    if any(w in prompt for w in ("写", "灵感", "故事", "创作", "文案")):
        return "试试这个开头：傍晚六点，潮水把一封写给明天的信送回岸边。收信人刚刚决定离开这座小城。\n\n接下来可以写他拆信时看到的第一句话，也可以先写他为什么迟迟没有打开。"
    if any(w in prompt for w in ("计划", "规划", "整理", "学习", "效率")):
        return "先写下一件今天结束时想看到的成果。再把它拆成三个动作：收集手头材料、完成最小的一步、留十分钟检查。\n\n你可以从第一步开始，把目标写成一句能勾选的待办。"
    if any(w in prompt for w in ("你好","您好","嗨","hello","Hi")):
        return "你好，欢迎来到潮汐。可以试着发「写个故事开头」「制定学习计划」或「聊聊今天的心情」，体验几种聊天场景。"
    return "这条消息已加入对话。在演示中，你可以试试「制定学习计划」「写个故事开头」或「聊聊今天的心情」。连接模型并登录后就能获得针对问题的回答。"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def provider_call(route, messages, max_out):
    body = json.dumps({"model": route["model"], "messages": messages,
                       "max_tokens": max_out, "stream": False}).encode()
    req = Request(route["base"] + "/chat/completions", data=body, headers={
        "Content-Type": "application/json", "Authorization": "Bearer " + route["key"]})
    try:
        with build_opener(NoRedirect).open(req, timeout=80) as response:
            data = json.loads(response.read(4_194_305))
        reply = data["choices"][0]["message"]["content"]
        if not isinstance(reply, str) or not reply.strip():
            raise ValueError
        usage = data["usage"]
        incoming, outgoing = usage["prompt_tokens"], usage["completion_tokens"]
        if any(type(v) is not int or not 0 <= v <= 10_000_000 for v in (incoming, outgoing)):
            raise ValueError
        return reply, incoming, outgoing
    except HTTPError as exc:
        if exc.code == 400:
            raise APIError(422, "模型未接受本次请求。长对话可缩短上下文后重试。")
        raise APIError(502, "模型服务暂时不可用，请稍后重试或切换模式。")
    except (URLError, OSError, ValueError, TypeError, IndexError, KeyError):
        raise APIError(502, "模型回复或用量数据不完整，本次未扣费。")


def begin_request(db, uid, req_id, digest, route, messages):
    # A single active generation per account protects a shared wallet across tabs.
    db.execute("BEGIN IMMEDIATE")
    try:
        cutoff = time.time() - 300
        db.execute("UPDATE requests SET state='failed' WHERE user_id=? AND state='pending' AND started<?", (uid, cutoff))
        prev = db.execute("SELECT * FROM requests WHERE user_id=? AND req_id=?", (uid,req_id)).fetchone()
        if prev:
            if prev["digest"] != digest:
                raise APIError(409, "请求编号已用于其他消息。")
            if prev["state"] == "done":
                db.commit()
                return json.loads(prev["result"]), 0, 0, None
            if prev["state"] == "pending":
                raise APIError(409, "这条消息正在生成，请稍后再试。")
        if db.execute("SELECT 1 FROM requests WHERE user_id=? AND state='pending'", (uid,)).fetchone():
            raise APIError(409, "你的其他对话正在生成，请等它完成后再试。")
        balance = db.execute("SELECT balance_micro FROM users WHERE id=?", (uid,)).fetchone()[0]
        # UTF-8 byte count and role overhead are conservative estimates, never billed usage.
        budget_in = sum(len(m["content"].encode()) + 16 for m in messages) + 32
        remaining = balance - cost_micro(route,budget_in,0)
        max_out = min(4096, int(remaining / route["outgoing"]))
        if max_out < 16:
            raise APIError(402, "可用余额不足以完成这次对话，请充值或缩短上下文。")
        attempt = uuid.uuid4().hex
        db.execute("""INSERT INTO requests(user_id,req_id,digest,started,state,attempt)
                      VALUES(?,?,?,?,'pending',?)
                      ON CONFLICT(user_id,req_id) DO UPDATE
                      SET started=excluded.started,state='pending',attempt=excluded.attempt""",
                   (uid,req_id,digest,time.time(),attempt))
        db.commit()
        return None, max_out, balance, attempt
    except Exception:
        db.rollback()
        raise


def chat(db, user, body):
    mode = body.get("mode", "economy")
    if mode not in ("fast", "economy"):
        raise APIError(400, "未知对话模式。")
    req_id = body.get("requestId")
    try:
        req_id = str(uuid.UUID(str(req_id)))
    except ValueError:
        raise APIError(400, "请提供有效的请求编号。")
    messages = sanitize_messages(body.get("messages"))
    route = route_config(mode)
    if not user or not route["live"]:
        return dict(reply=demo_reply(messages[-1]["content"]), demo=True, usage={"input":0,"output":0},
                    cost=0,balance=yuan(user["balance_micro"]) if user else 0)
    uid = user["id"]
    payload = json.dumps([mode,messages], ensure_ascii=False, separators=(",",":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    replay, limit, available, attempt = begin_request(db, uid, req_id, digest, route, messages)
    if replay is not None:
        return replay
    try:
        reply, incoming, outgoing = provider_call(route, messages, limit)
        fee = cost_micro(route,incoming,outgoing)
        if fee > available:
            raise APIError(502, "供应商返回的用量超出预留额度，本次未扣费。")
        db.execute("BEGIN IMMEDIATE")
        active = db.execute("""SELECT 1 FROM requests WHERE user_id=? AND req_id=?
                               AND attempt=? AND state='pending'""",(uid,req_id,attempt)).fetchone()
        if not active:
            raise APIError(409, "请求已过期，本次未扣费。")
        if not db.execute("UPDATE users SET balance_micro=balance_micro-? WHERE id=? AND balance_micro>=?",
                          (fee,uid,fee)).rowcount:
            raise APIError(402, "当前可用余额不足，本次未扣费。")
        balance = db.execute("SELECT balance_micro FROM users WHERE id=?", (uid,)).fetchone()[0]
        db.execute("INSERT INTO ledger(user_id,type,amount_micro,mode,input,output,created_at) VALUES(?,'chat',?,?,?,?,?)",
                   (uid,-fee,mode,incoming,outgoing,now_iso()))
        result = dict(reply=reply,demo=False,usage={"input":incoming,"output":outgoing},cost=yuan(fee),balance=yuan(balance))
        db.execute("UPDATE requests SET state='done',result=? WHERE user_id=? AND req_id=?",
                   (json.dumps(result,ensure_ascii=False),uid,req_id))
        db.commit()
        return result
    except Exception:
        db.rollback()
        db.execute("""UPDATE requests SET state='failed' WHERE user_id=? AND req_id=?
                      AND attempt=? AND state='pending'""", (uid,req_id,attempt))
        raise
