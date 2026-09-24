"""End-to-end tests against real throwaway git repositories."""

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "examples"))

import make_demo  # noqa: E402
from tangle.scan import scan  # noqa: E402
from tangle.verify import verify  # noqa: E402

ID = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


class Repo:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="tangle-test-")
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        subprocess.run(["git", *ID, *args], cwd=self.root, check=True, capture_output=True)

    def write(self, path, text):
        full = os.path.join(self.root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8", newline="\n") as f:
            f.write(textwrap.dedent(text).lstrip())

    def commit(self, msg="c"):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)

    def branch(self, name, files):
        self.git("checkout", "-q", "main")
        self.git("checkout", "-q", "-b", name)
        for path, text in files.items():
            self.write(path, text)
        self.commit(name)
        self.git("checkout", "-q", "main")

    def cleanup(self):
        shutil.rmtree(self.root, ignore_errors=True)


def kinds(result):
    return sorted((f.kind, f.severity) for f in result.findings)


class DemoRepo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = os.path.join(tempfile.mkdtemp(prefix="tangle-demo-"), "repo")
        make_demo.build(cls.root)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(os.path.dirname(cls.root), ignore_errors=True)

    def test_scan_predicts_the_break(self):
        result = scan(["agent/currency", "agent/checkout", "agent/docs"], "main", self.root)
        self.assertEqual(result.verdicts[("agent/currency", "agent/checkout")], "break")
        self.assertEqual(result.verdicts[("agent/currency", "agent/docs")], "ok")
        self.assertEqual(result.verdicts[("agent/checkout", "agent/docs")], "ok")
        [finding] = result.findings
        self.assertEqual((finding.kind, finding.severity, finding.symbol), ("semantic", "high", "total"))

    def test_verify_confirms_green_alone_red_together(self):
        cmd = f'"{sys.executable}" -m unittest -q'
        result = verify(["agent/currency", "agent/checkout", "agent/docs"], "main", cmd, self.root)
        self.assertTrue(all(r.status == "pass" for r in result.singles.values()))
        self.assertEqual(result.pair_verdict("agent/currency", "agent/checkout"), "break")
        self.assertEqual(result.pair_verdict("agent/currency", "agent/docs"), "ok")
        self.assertEqual(result.combined.status, "fail")
        self.assertIn("currency", result.pairs[("agent/currency", "agent/checkout")].tail)

    def test_verify_leaves_no_worktree_behind(self):
        out = subprocess.run(["git", "worktree", "list"], cwd=self.root, capture_output=True, text=True).stdout
        self.assertEqual(len(out.strip().splitlines()), 1)


class Scenarios(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()
        self.repo.write("lib/core.py", """
            def load(path):
                return open(path).read()


            def save(path, data):
                with open(path, "w") as f:
                    f.write(data)
        """)
        self.repo.write("lib/app.py", """
            from lib.core import load


            def main():
                return load("x")
        """)
        self.repo.commit("base")

    def tearDown(self):
        self.repo.cleanup()

    def test_compatible_signature_change_is_not_flagged(self):
        self.repo.branch("a", {"lib/core.py": """
            def load(path, encoding="utf-8"):
                return open(path, encoding=encoding).read()


            def save(path, data):
                with open(path, "w") as f:
                    f.write(data)
        """})
        self.repo.branch("b", {"lib/extra.py": """
            from lib.core import load


            def other():
                return load("y")
        """})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertEqual(result.findings, [])
        self.assertEqual(result.verdicts[("a", "b")], "ok")

    def test_removed_function_used_by_other_branch(self):
        self.repo.branch("a", {"lib/core.py": """
            def load(path):
                return open(path).read()
        """})
        self.repo.branch("b", {"lib/extra.py": """
            from lib.core import save


            def persist():
                save("y", "data")
        """})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertEqual(kinds(result), [("semantic", "high")])
        self.assertIn("removed `save`", result.findings[0].summary)

    def test_moved_function_is_not_a_removal(self):
        self.repo.branch("a", {
            "lib/core.py": """
                def load(path):
                    return open(path).read()
            """,
            "lib/io.py": """
                def save(path, data):
                    with open(path, "w") as f:
                        f.write(data)
            """,
        })
        self.repo.branch("b", {"lib/extra.py": """
            from lib.core import save
            save("y", "z")
        """})
        self.assertEqual(scan(["a", "b"], "main", self.repo.root).findings, [])

    def test_same_function_edited_in_different_hunks_is_overlap(self):
        self.repo.write("lib/long.py", "def f(x):\n" + "".join(f"    x += {i}\n" for i in range(12)) + "    return x\n")
        self.repo.commit("long")
        body = ["def f(x):\n"] + [f"    x += {i}\n" for i in range(12)] + ["    return x\n"]
        a, b = list(body), list(body)
        a[1] = "    x += 100\n"
        b[12] = "    x += 200\n"
        self.repo.branch("a", {"lib/long.py": "".join(a)})
        self.repo.branch("b", {"lib/long.py": "".join(b)})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertEqual(kinds(result), [("overlap", "medium")])
        self.assertEqual(result.verdicts[("a", "b")], "risk")

    def test_textual_conflict(self):
        self.repo.branch("a", {"lib/app.py": 'from lib.core import load\n\n\ndef main():\n    return load("a")\n'})
        self.repo.branch("b", {"lib/app.py": 'from lib.core import load\n\n\ndef main():\n    return load("b")\n'})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertEqual(result.verdicts[("a", "b")], "conflict")
        self.assertEqual(kinds(result), [("textual", "high")])

    def test_js_signature_break(self):
        self.repo.write("web/price.ts", "export function price(amount: number) {\n  return amount;\n}\n")
        self.repo.commit("ts")
        self.repo.branch("a", {"web/price.ts": "export function price(amount: number, currency: string) {\n  return amount;\n}\n"})
        self.repo.branch("b", {"web/cart.ts": "import { price } from './price';\nexport const due = (n: number) => price(n);\n"})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertEqual(kinds(result), [("semantic", "high")])

    def test_both_touch_lockfile_and_migrations(self):
        self.repo.write("package.json", '{\n  "name": "x"\n}\n')
        self.repo.commit("pkg")
        self.repo.branch("a", {"db/migrations/0002_a.sql": "select 1;\n", "package.json": '{\n  "name": "x",\n  "a": 1\n}\n'})
        self.repo.branch("b", {"db/migrations/0002_b.sql": "select 2;\n"})
        result = scan(["a", "b"], "main", self.repo.root)
        self.assertIn(("hotspot", "medium"), kinds(result))


if __name__ == "__main__":
    unittest.main()
