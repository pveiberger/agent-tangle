"""Build a tiny repo where three "agents" worked in parallel - and two of them silently break each other.

    python examples/make_demo.py /tmp/tangle-demo
    cd /tmp/tangle-demo
    tangle scan
    tangle verify --test "python -m unittest -q"

  main            shop/pricing.py: total(items, tax_rate)
  agent/currency  makes `currency` a required parameter of total() and updates every existing caller
  agent/checkout  adds a new checkout module that calls total(items, 0.19)
  agent/docs      only touches the README

Every branch is green on its own. git merges all of them without a single conflict.
main + currency + checkout is red.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

ID = ["-c", "user.name=demo", "-c", "user.email=demo@example.com", "-c", "commit.gpgsign=false"]


def sh(*args: str, cwd: str) -> None:
    subprocess.run(["git", *ID, *args], cwd=cwd, check=True, capture_output=True)


def write(root: str, path: str, text: str) -> None:
    full = os.path.join(root, path)
    os.makedirs(os.path.dirname(full) or root, exist_ok=True)
    with open(full, "w", encoding="utf-8", newline="\n") as f:
        f.write(textwrap.dedent(text).lstrip())


def commit(root: str, msg: str) -> None:
    sh("add", "-A", cwd=root)
    sh("commit", "-q", "-m", msg, cwd=root)


def build(root: str) -> None:
    os.makedirs(root, exist_ok=True)
    if os.listdir(root):
        sys.exit(f"{root} is not empty")
    sh("init", "-q", "-b", "main", cwd=root)

    write(root, "shop/__init__.py", "")
    write(root, "shop/pricing.py", '''
        def total(items, tax_rate):
            """Sum of item prices including tax."""
            net = sum(price for _, price in items)
            return round(net * (1 + tax_rate), 2)
    ''')
    write(root, "shop/cart.py", '''
        from shop.pricing import total


        class Cart:
            def __init__(self):
                self.items = []

            def add(self, name, price):
                self.items.append((name, price))

            def amount_due(self):
                return total(self.items, 0.2)
    ''')
    write(root, "tests/__init__.py", "")
    write(root, "tests/test_cart.py", '''
        import unittest

        from shop.cart import Cart


        class CartTest(unittest.TestCase):
            def test_amount_due(self):
                cart = Cart()
                cart.add("book", 10.0)
                self.assertEqual(cart.amount_due(), 12.0)
    ''')
    write(root, "README.md", "# demo shop\n")
    commit(root, "initial shop")

    # agent 1: multi-currency support. Changes the signature and fixes every caller that exists *on its base*.
    sh("checkout", "-q", "-b", "agent/currency", cwd=root)
    write(root, "shop/pricing.py", '''
        RATES = {"EUR": 1.0, "USD": 1.1}


        def total(items, tax_rate, currency):
            """Sum of item prices including tax, converted to `currency`."""
            net = sum(price for _, price in items)
            return round(net * (1 + tax_rate) * RATES[currency], 2)
    ''')
    write(root, "shop/cart.py", '''
        from shop.pricing import total


        class Cart:
            def __init__(self, currency="EUR"):
                self.items = []
                self.currency = currency

            def add(self, name, price):
                self.items.append((name, price))

            def amount_due(self):
                return total(self.items, 0.2, self.currency)
    ''')
    commit(root, "pricing: multi-currency totals")

    # agent 2: new checkout flow, written against the *old* total().
    sh("checkout", "-q", "main", cwd=root)
    sh("checkout", "-q", "-b", "agent/checkout", cwd=root)
    write(root, "shop/checkout.py", '''
        from shop.pricing import total


        def invoice(items):
            lines = [f"{name}: {price:.2f}" for name, price in items]
            lines.append(f"TOTAL (19% VAT): {total(items, 0.19):.2f}")
            return "\\n".join(lines)
    ''')
    write(root, "tests/test_checkout.py", '''
        import unittest

        from shop.checkout import invoice


        class CheckoutTest(unittest.TestCase):
            def test_invoice_total(self):
                self.assertIn("TOTAL (19% VAT): 11.90", invoice([("pen", 10.0)]))
    ''')
    commit(root, "checkout: invoice rendering")

    # agent 3: harmless.
    sh("checkout", "-q", "main", cwd=root)
    sh("checkout", "-q", "-b", "agent/docs", cwd=root)
    write(root, "README.md", "# demo shop\n\nRun the tests with `python -m unittest`.\n")
    commit(root, "docs: how to run tests")

    sh("checkout", "-q", "main", cwd=root)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build(os.path.abspath(sys.argv[1]))
    print(f"demo repo ready at {sys.argv[1]}")
