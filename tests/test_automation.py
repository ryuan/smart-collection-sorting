import csv
import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from automation import approved_ids, main, select_targets, validate_batches
from app.exports import collection_orders, read_collections
from test_sorting import BASE, CHILD, OTHER, count, snapshot, write_rows


class AutomationTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.c, self.p, self.o = snapshot(self.root)
        self.rows = [json.loads(line) for line in self.c.read_text().splitlines()]
        for row in self.rows:
            row["sortOrder"] = "MANUAL"
        write_rows(self.c, self.rows)
        config = self.root / "config"
        config.mkdir()
        (config / "scoring.json").write_bytes(
            (Path(__file__).resolve().parents[1] / "config/scoring.example.json").read_bytes())
        (config / "base_collections.json").write_text('{"base_collections":[10]}')

    def test_private_allowlist_schema(self):
        path = self.root / "base_collections.json"
        for ids in ([], [True], ["10"], [0], [10, 10], None):
            path.write_text(json.dumps({"base_collections": ids}))
            with self.assertRaises(ValueError):
                approved_ids(path)
        path.write_text('{"base_collections":[10,20]}')
        self.assertEqual(approved_ids(path), [10, 20])

    def test_only_reciprocal_manual_nonempty_targets(self):
        c = read_collections(self.c)
        self.assertEqual(select_targets(BASE, c), [BASE, CHILD])
        c[BASE]["sortOrder"] = "BEST_SELLING"
        self.assertEqual(select_targets(BASE, c), [CHILD])
        c[CHILD]["productsCount"] = count(0)
        self.assertEqual(select_targets(BASE, c), [])
        c[CHILD]["productsCount"] = count(2)
        c[CHILD]["sortOrder"] = None
        with self.assertRaises(ValueError):
            select_targets(BASE, c)
        c[CHILD]["sortOrder"] = "MANUAL"
        with self.assertRaises(ValueError):
            select_targets(CHILD, c)
        c[CHILD]["filter.base_collection"] = {OTHER}
        with self.assertRaises(ValueError):
            select_targets(BASE, c)

    def test_incomplete_ranking_and_uncertain_counts(self):
        c = read_collections(self.c)
        c[BASE]["productsCount"]["precision"] = "AT_LEAST"
        with self.assertRaises(ValueError):
            select_targets(BASE, c)
        with self.assertRaisesRegex(ValueError, "exactly once"):
            collection_orders(BASE, c, {"a": {}, "b": {}}, ["a", "a"])

    def test_workflow_stages_rank_nonmanual_base_for_manual_child(self):
        self.rows[0]["sortOrder"] = "BEST_SELLING"
        write_rows(self.root / "collections.jsonl", self.rows)
        with patch.object(sys, "argv", ["automation.py", "select", str(self.root)]):
            main()
        directory = self.root / "bases" / "10"
        self.p.rename(directory / "products.jsonl")
        self.o.rename(self.root / "orders.jsonl")
        with patch.object(sys, "argv", ["automation.py", "rank", str(self.root)]):
            main()
        batch, = json.loads((self.root / "batches.json").read_text())
        with (self.root / batch).open() as stream:
            rows = list(csv.DictReader(stream))
        self.assertEqual([r["parentId"] for r in rows], [CHILD, CHILD])
        self.assertEqual([r["newPosition"] for r in rows], ["0", "1"])

    def test_empty_manual_scope_yields_an_empty_batch_list(self):
        for row in self.rows:
            row["sortOrder"] = "BEST_SELLING"
        write_rows(self.root / "collections.jsonl", self.rows)
        for stage in ("select", "rank"):
            with patch.object(sys, "argv", ["automation.py", stage, str(self.root)]):
                main()
        self.assertEqual(json.loads((self.root / "batches.json").read_text()), [])
        self.assertFalse((self.root / "bases" / "10").exists())

    def test_pilot_selects_only_the_private_pair(self):
        extra = dict(self.rows[1], id="gid://shopify/Collection/13", handle="extra")
        self.rows[0]["children"]["value"] = json.dumps([CHILD, extra["id"]])
        extra["productsCount"] = count(0)
        write_rows(self.root / "collections.jsonl", [*self.rows, extra])
        pilot = self.root / "config/pilot.txt"
        for pair in ("family,curated", "subset,family", "family,family", "", "family,extra"):
            pilot.write_text(pair)
            with patch.object(sys, "argv", ["automation.py", "select", str(self.root)]):
                with self.subTest(pair=pair), self.assertRaises((KeyError, ValueError)):
                    main()
        self.rows[0]["children"]["value"] = json.dumps([CHILD, OTHER])
        self.rows[2]["parent"] = self.rows[1]["parent"]
        write_rows(self.root / "collections.jsonl", self.rows)
        pilot.write_text("family,subset")
        with patch.object(sys, "argv", ["automation.py", "select", str(self.root)]):
            main()
        self.p.rename(self.root / "bases/10/products.jsonl")
        self.o.rename(self.root / "orders.jsonl")
        with patch.object(sys, "argv", ["automation.py", "rank", str(self.root)]):
            main()
        expected = json.loads((self.root / "pilot-orders.json").read_text())
        self.assertEqual(set(expected), {BASE, CHILD})
        batch, = json.loads((self.root / "batches.json").read_text())
        with (self.root / batch).open() as stream:
            self.assertEqual({r["parentId"] for r in csv.DictReader(stream)}, {BASE, CHILD})

    def test_serialized_batches_reject_damage_and_extra_targets(self):
        path = self.root / "reorder_001_sample.csv"
        header = "id,parentId,newPosition\n"
        family = {BASE: ["p1", "p2"]}
        good = [f"p1,{BASE},0\n", f"p2,{BASE},1\n"]
        for rows in (good[:1], [good[0], good[0]], [good[0], f"p2,{OTHER},1\n"],
                     [good[0], f"p1,{BASE},1\n"]):
            path.write_text(header + "".join(rows))
            with self.assertRaises(ValueError):
                validate_batches(self.root, [BASE], family)
        path.write_text(header + "".join(good))
        self.assertEqual(validate_batches(self.root, [BASE], family), [path])
