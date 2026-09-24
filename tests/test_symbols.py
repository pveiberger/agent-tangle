import unittest

from tangle.scan import signature_compatible
from tangle.symbols import extract


class PythonSymbols(unittest.TestCase):
    SRC = (
        "import os\n"
        "LIMIT = 3\n"
        "def total(items, tax_rate=0.2) -> float:\n"
        "    return helper(items) * tax_rate\n"
        "class Cart:\n"
        "    def __init__(self, currency):\n"
        "        self.c = currency\n"
        "    def add(self, x):\n"
        "        self.items.append(x)\n"
    )

    def test_definitions_and_signatures(self):
        fs = extract("a.py", self.SRC)
        self.assertEqual(fs.defs["total"].signature, "(items, tax_rate=0.2) -> float")
        self.assertEqual(fs.defs["Cart"].signature, "(self, currency)")
        self.assertEqual(fs.defs["Cart.add"].kind, "method")
        self.assertEqual(fs.defs["LIMIT"].kind, "variable")

    def test_references(self):
        refs = {(r.name, r.line, r.strength) for r in extract("a.py", self.SRC).refs}
        self.assertIn(("helper", 4, "direct"), refs)
        self.assertIn(("append", 9, "attribute"), refs)

    def test_body_hash_ignores_position(self):
        a = extract("a.py", "def f(x):\n    return x\n").defs["f"].body_hash
        b = extract("a.py", "\n\n\ndef f(x):\n    return x\n").defs["f"].body_hash
        self.assertEqual(a, b)

    def test_syntax_error_is_reported_not_raised(self):
        self.assertFalse(extract("a.py", "def (:\n").parsed)


class JsSymbols(unittest.TestCase):
    SRC = (
        "import { formatPrice } from './fmt';\n"
        "export function total(items: Item[], taxRate: number): number {\n"
        "  return formatPrice(items.length * taxRate);\n"
        "}\n"
        "export const toCents = async (value, currency = 'EUR') => value * 100;\n"
        "export class Cart {\n"
        "  add(item) {\n"
        "    this.items.push(item);\n"
        "  }\n"
        "}\n"
        "export interface Item { price: number }\n"
    )

    def test_definitions(self):
        defs = extract("a.ts", self.SRC).defs
        self.assertEqual(defs["total"].signature, "(items: Item[], taxRate: number)")
        self.assertEqual(defs["toCents"].signature, "(value, currency = 'EUR')")
        self.assertEqual(defs["Cart.add"].kind, "method")
        self.assertEqual(defs["Item"].kind, "type")

    def test_references_skip_strings_and_own_definition(self):
        refs = extract("a.ts", self.SRC).refs
        names = {(r.name, r.line) for r in refs}
        self.assertIn(("formatPrice", 1), names)
        self.assertIn(("formatPrice", 3), names)
        self.assertNotIn(("total", 2), names)
        self.assertNotIn(("EUR", 5), names)


class SignatureCompatibility(unittest.TestCase):
    def test_compatible(self):
        self.assertTrue(signature_compatible("(a, b)", "(a, b)"))
        self.assertTrue(signature_compatible("(a, b)", "(a, b, c=1)"))
        self.assertTrue(signature_compatible("(a, b)", "(a, b, *args, **kwargs)"))
        self.assertTrue(signature_compatible("(a: number)", "(a: number, b?: string)"))
        self.assertTrue(signature_compatible("(a) -> int", "(a) -> float"))

    def test_breaking(self):
        self.assertFalse(signature_compatible("(a, b)", "(a, b, c)"))
        self.assertFalse(signature_compatible("(a, b)", "(b, a)"))
        self.assertFalse(signature_compatible("(a, b)", "(a)"))
        self.assertFalse(signature_compatible("(a, b)", None))
        self.assertFalse(signature_compatible("(a: dict[str, int])", "(a: dict[str, int], b: list[int])"))


if __name__ == "__main__":
    unittest.main()
