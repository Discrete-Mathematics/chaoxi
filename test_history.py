"""Check the shipping browser importer with standard Python and Node 24+. No network."""
import io
import json
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent
STAMP = "2026-11-17T09:00:00Z"
NODE_DRIVER = """
const [impl, path] = process.argv.slice(1);
const source = readFileSync(impl, 'utf8');
const ctx = vm.createContext({ TextDecoder, Blob, DecompressionStream, crypto: webcrypto });
const importer = vm.runInContext(source + '\\nTideHistory', ctx);
try {
  const result = await importer.read(new File([readFileSync(path)], basename(path)));
  console.log(JSON.stringify(result));
} catch(error) {
  console.log(JSON.stringify({error: String(error.message)}));
}
"""
NODE_IMPORTS = """
import vm from 'node:vm';
import { webcrypto } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { basename } from 'node:path';
import { Blob, File } from 'node:buffer';
"""


def v3_messages(selected):
    rows = [
        (3, 1, "ASSISTANT", "RESPONSE", "最新答案"),
        (1, None, "USER", "REQUEST", "用户问题"),
        (2, 1, "ASSISTANT", "RESPONSE", "选中的答案"),
    ]
    return {"id": "demo", "title": "分支案例", "current_message_id": selected,
            "inserted_at": STAMP, "messages": [
                {"id": ident, "parent_id": parent, "role": role, "status": "FINISHED",
                 "inserted_at": STAMP, "fragments": [{"type": "THINK", "content": "推理"},
                                                      {"type": kind, "content": txt}]}
                for ident, parent, role, kind, txt in rows]}


def archive(chat=None, version=3, index=None):
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", zipfile.ZIP_DEFLATED) as z:
        if version is not None:
            z.writestr("manifest.json", json.dumps({"export_format_version": version}))
        z.writestr("chat_session_index.json", json.dumps(index if index is not None else
                   [{"id": "demo", "inserted_at": STAMP, "updated_at": STAMP}]))
        z.writestr("chat_sessions/demo.json", json.dumps(chat or v3_messages(2)))
    return raw.getvalue()


@unittest.skipUnless(shutil.which("node"), "迁移测试需要 Node.js 24 或更高版本")
class HistoryTest(unittest.TestCase):
    def read(self, name, raw):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / name
            path.write_bytes(raw if isinstance(raw, bytes) else json.dumps(raw).encode())
            result = subprocess.run(["node", "--input-type=module", "-e", NODE_IMPORTS + NODE_DRIVER,
                                     str(ROOT / "dist" / "history.js"), str(path)],
                                    capture_output=True, text=True, encoding="utf-8", timeout=15)
            self.assertEqual(result.returncode, 0, result.stderr)
            return json.loads(result.stdout)

    def test_v3_branches_and_date(self):
        for selected, expected in [(2, "选中的答案"), (None, "最新答案"), (999, "最新答案")]:
            with self.subTest(selected=selected):
                chat, = self.read("data.zip", archive(v3_messages(selected)))
                self.assertEqual(chat["title"], "分支案例")
                self.assertEqual(chat["time"], 1794906000000)
                self.assertEqual(chat["messages"], [{"role":"user","content":"用户问题"},
                                                    {"role":"assistant","content":expected}])

    def test_legacy_roleless_mapping(self):
        nodes = {"root": {"id":"root","message":None,"parent":None,"children":["1"]},
                 "1": {"id":"1","message":{"fragments":[{"type":"REQUEST","content":"问"},
                        {"type":"THINK","content":"略"}, {"type":"RESPONSE","content":"答"}]},
                       "parent":"root","children":[]}}
        chat, = self.read("conversations.json", [{"mapping": nodes, "updated_at": STAMP}])
        self.assertEqual(chat["messages"], [{"role":"user","content":"问"},
                                            {"role":"assistant","content":"答"}])

    def test_tide_backup(self):
        data = {"id":"old", "mode":"fast", "title":"备份", "time":1794906000000,
                "messages":[{"role":"assistant","content":"演示回复","demo":True}]}
        saved, = self.read("backup.json", {"format":"tide-v1","conversations":[data]})
        self.assertNotEqual(saved["id"], data["id"])
        for field in ["mode","title","time","messages"]:
            self.assertEqual(saved[field], data[field])

    def test_damaged_archives(self):
        broken_tree = v3_messages(2)
        broken_tree["messages"][-1]["parent_id"] = 100
        loop = v3_messages(2)
        loop["messages"][-1]["parent_id"] = 2
        damaged_crc = bytearray(archive())
        crc_field = damaged_crc.index(b"PK\x01\x02") + 16
        damaged_crc[crc_field] ^= 1
        for label,raw in [("version",archive(version=100)), ("truncated", archive()[:-18]),
                          ("missing manifest",archive(version=None)), ("index",archive(index=[])),
                          ("missing parent",archive(broken_tree)), ("cycle",archive(loop)),
                          ("crc",bytes(damaged_crc))]:
            with self.subTest(label=label):
                self.assertIn("error", self.read("data.zip", raw))


if __name__ == "__main__":
    unittest.main()
