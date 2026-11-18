"""Local administration. Run alongside server.py with the same .env and database."""
import argparse
import json
import re
import sqlite3
import sys
from contextlib import closing
from decimal import Decimal

from chat_service import now_iso
from server import database, initialize, load_env


def money(micro):
    return format(Decimal(micro) / 1_000_000, ".6f")


def grant_credit(db, username, amount, reference):
    if not re.fullmatch(r"[0-9]{1,5}(?:\.[0-9]{1,2})?", amount) or not 0 < Decimal(amount) <= 10000:
        raise ValueError("发放金额需大于零且不超过 10000 元，最多两位小数。")
    micro = int(Decimal(amount) * 1_000_000)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}", reference):
        raise ValueError("业务编号需为 1–96 位字母、数字、点、下划线或短横线，以字母或数字开头。")
    db.execute("BEGIN IMMEDIATE")
    try:
        db.execute("""CREATE TABLE IF NOT EXISTS admin_grants (
            reference TEXT PRIMARY KEY, ledger_id INTEGER UNIQUE NOT NULL REFERENCES ledger(id)
        )""")
        user = db.execute("SELECT id,username,balance_micro FROM users WHERE username=?", (username,)).fetchone()
        if not user:
            raise ValueError("未找到该用户名，请先在当前服务注册。")
        prior = db.execute("""SELECT l.id, l.user_id, l.amount_micro FROM admin_grants g
                              JOIN ledger l ON l.id=g.ledger_id WHERE g.reference=?""", (reference,)).fetchone()
        if prior and (prior["user_id"] != user["id"] or prior["amount_micro"] != micro):
            raise ValueError("该业务编号对应其他账号或金额，请核对发放记录。")
        if prior:
            ledger_id = prior["id"]
            balance = user["balance_micro"]
        else:
            balance = user["balance_micro"] + micro
            db.execute("UPDATE users SET balance_micro=? WHERE id=?", (balance,user["id"]))
            ledger_id = db.execute("""INSERT INTO ledger(user_id,type,amount_micro,created_at)
                                      VALUES(?,'credit',?,?)""", (user["id"],micro,now_iso())).lastrowid
            db.execute("INSERT INTO admin_grants VALUES(?,?)", (reference,ledger_id))
        db.commit()
        return dict(username=user["username"], granted=not bool(prior), reference=reference,
                    amount=money(micro), balance=money(balance), ledgerId=ledger_id)
    except Exception:
        db.rollback()
        raise


def main(args=None):
    parser = argparse.ArgumentParser(description="潮汐本机账号管理")
    command = parser.add_subparsers(dest="command", required=True)
    users = command.add_parser("users",help="按 ID 升序列出账号和人民币余额")
    users.add_argument("--after",type=int,default=0,help="从此 ID 之后列起")
    users.add_argument("--limit",type=int,default=100,help="返回 1–1000 行")
    credit = command.add_parser("credit",help="按唯一业务编号发放体验额度")
    credit.add_argument("--username",required=True)
    credit.add_argument("--amount",required=True,help="人民币金额，最多两位小数")
    credit.add_argument("--reference",required=True,help="唯一业务编号；重复执行同笔发放可复用")
    options = parser.parse_args(args)
    try:
        load_env()
        initialize()
        with closing(database()) as db:
            if options.command == "credit":
                result = grant_credit(db,options.username.strip(),options.amount,options.reference)
            else:
                if options.after < 0 or not 1 <= options.limit <= 1000:
                    raise ValueError("after 需为非负整数，limit 取 1–1000。")
                rows = db.execute("""SELECT id,username,balance_micro FROM users WHERE id>?
                                     ORDER BY id LIMIT ?""",(options.after,options.limit))
                result = [dict(id=r["id"],username=r["username"],balance=money(r["balance_micro"])) for r in rows]
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(str(exc),file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
