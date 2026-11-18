"""Admin credit operations on a disposable database, including concurrent retries."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import admin
import server


class AdminTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        config = patch.dict(os.environ,{"DATABASE_PATH":os.path.join(temp.name,"admin-test.db")},clear=True)
        config.start()
        self.addCleanup(config.stop)
        server.initialize()
        with contextlib.closing(server.database()) as db:
            db.executemany("INSERT INTO users(username,salt,password_hash) VALUES(?,X'00',X'00')",[("甲乙",),("用户B",)])

    def grant(self,username="甲乙",amount="1.23",reference="trial-01"):
        with contextlib.closing(server.database()) as db:
            return admin.grant_credit(db,username,amount,reference)

    def test_concurrent_retry_and_conflicting_reference(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _:self.grant(),range(4)))
        self.assertEqual(sum(r["granted"] for r in results),1)
        self.assertEqual(len({r["ledgerId"] for r in results}),1)
        for username,amount in [("甲乙","2.00"),("用户B","1.23")]:
            with self.assertRaises(ValueError):
                self.grant(username,amount)
        with contextlib.closing(server.database()) as db:
            self.assertEqual(db.execute("SELECT balance_micro FROM users WHERE id=1").fetchone()[0],1_230_000)
            self.assertEqual([tuple(r) for r in db.execute("SELECT type,amount_micro FROM ledger")],[("credit",1_230_000)])

    def test_invalid_grants_and_unknown_user_leave_wallet_unchanged(self):
        for value in ["0","-1","0.001","NaN","1e3","10001"]:
            with self.assertRaises(ValueError):
                self.grant(amount=value)
        with self.assertRaises(ValueError):
            self.grant(username="missing")
        with self.assertRaises(ValueError):
            self.grant(reference=" ")
        with contextlib.closing(server.database()) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM ledger").fetchone()[0],0)

    def test_command_entry_points(self):
        out = io.StringIO()
        with patch.object(admin,"load_env"),contextlib.redirect_stdout(out):
            self.assertEqual(admin.main(["credit","--username","用户B","--amount","2.50","--reference","trial-B"]),0)
            self.assertEqual(json.loads(out.getvalue())["balance"],"2.500000")
            out.seek(0)
            out.truncate()
            self.assertEqual(admin.main(["users","--after","1","--limit","1"]),0)
            self.assertEqual(json.loads(out.getvalue()),[{"id":2,"username":"用户B","balance":"2.500000"}])


if __name__ == "__main__":
    unittest.main()
