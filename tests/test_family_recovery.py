"""Offline workflow tests. Run with both repositories and their normal requirements installed."""
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

SORTER = Path(__file__).resolve().parents[1]
ADAPTER = SORTER / ".github/scripts/data_manager_dry_run.py"
MANAGER = SORTER.parent / "shopify-data-manager"
if (SORTER / "data-manager").is_dir():
    MANAGER = SORTER / "data-manager"

BASE, CHILD, SECOND = (f"gid://shopify/Collection/{n}" for n in (10, 11, 12))


def read_rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


class SimulatedShop:
    """The adapter, ranker and Data Manager planner run normally. All remote I/O ends here."""

    def __init__(self, root, scenario, settings):
        self.root = root
        self.settings = settings
        self.client = self
        self.scenario = scenario
        self.events = []
        self.metadata = {r["id"]: r for r in read_rows(root / "collections.jsonl")}
        self.product_rows = read_rows(root / "catalog.jsonl")
        self.orders = {cid: [r["__parentId"] for r in self.product_rows if r["id"] == cid]
                       for cid in self.metadata}
        self.changes = 0

    def close(self):
        pass

    def change_membership(self, *, remove=False):
        child = self.orders[CHILD]
        if remove:
            child.pop()
            self.metadata[CHILD]["productsCount"]["count"] = len(child)
        else:
            replacement = next(pid for pid in self.orders[BASE] if pid not in child)
            child[-1] = replacement
        self.changes += 1
        self.events.append("membership changed")

    def execute(self, query, variables=None):
        if "collections(first:" in query:
            ids = ["gid://shopify/Collection/" + n for n in re.findall(r'id:(\d+)', query)]
            return {"collections": {"edges": [{"node": self.metadata[cid]} for cid in ids]}}
        if "query CollectionOrder" in query:
            cid = variables["id"]
            start = int(variables["after"] or 0)
            end = start + 250
            order = self.orders[cid]
            self.events.append("read " + cid)
            return {"collection": {"sortOrder": self.metadata[cid]["sortOrder"], "products": {
                "edges": [{"node": {"id": pid}} for pid in order[start:end]],
                "pageInfo": {"hasNextPage": end < len(order), "endCursor": str(end)}}}}
        if "query ResourceContexts" in query:
            return {"nodes": [{"id": pid, "__typename": "Product"} for pid in variables["ids"]]}
        if "query IntrospectInput" in query:
            fields = [{"name": "newPosition", "type": {"kind": "SCALAR", "name": "UnsignedInt64"}}]
            return {"__type": {"inputFields": fields if variables["name"] == "MoveInput" else []}}
        raise AssertionError("Unexpected GraphQL request")

    def run_query(self, path):
        folder = self.settings.root_dir / "export"
        folder.mkdir()
        result = folder / "result.jsonl"
        if path.name == "orders.graphql":
            self.events.append("export orders")
            result.write_bytes((self.root / "history.jsonl").read_bytes())
        else:
            number = re.search(r'collection_id:(\d+)', path.read_text())[1]
            self.events.append("export " + number)
            if number == "10" and self.scenario == "count-change" and not self.changes:
                self.change_membership(remove=True)
            rows = [r for r in self.product_rows if r["id"] != CHILD]
            rows.extend({"id": CHILD, "__parentId": pid} for pid in self.orders[CHILD])
            result.write_text("".join(json.dumps(row) + "\n" for row in rows))
            if number == "10" and (self.scenario == "repeat" or self.scenario == "before" and not self.changes):
                self.change_membership()
        operation = {"status": "COMPLETED", "fileSize": result.stat().st_size, "url": "synthetic"}
        (folder / "manifest_test.json").write_text(json.dumps({
            "status": "COMPLETED", "result": result.name, "operation": operation}))
        return folder

    def run_mutation(self, spec, definition, variables, result, identifier):
        from app.shopify.bulk import MutationResult
        rows = read_rows(variables)
        for row in rows:
            cid = row["id"]
            assert self.metadata[cid]["sortOrder"] == "MANUAL"
            self.events.append("write " + cid)
            order = self.orders[cid]
            for move in row["moves"]:
                order.remove(move["id"])
                order.insert(int(move["newPosition"]), move["id"])
        if self.scenario == "after-twice" or (not self.changes and self.scenario in ("after", "conflict")):
            self.change_membership(remove=True)
            if self.scenario == "conflict":
                order = self.orders[CHILD]
                order[:2] = reversed(order[:2])
        result.write_text("".join(json.dumps({"data": {spec.response_field: {
            "job": {"id": str(i)}, "userErrors": []}}}) + "\n" for i in range(len(rows))))
        errors = ["simulated failure"] if self.scenario == "mutation-error" else []
        return MutationResult(identifier, "COMPLETED", result, {"url": "synthetic"}, errors)


def simulate(root, scenario):
    import importlib.util
    from app.shopify.bulk import BulkManager
    spec = importlib.util.spec_from_file_location("workflow_adapter", ADAPTER)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    shop = SimulatedShop(root, scenario, SimpleNamespace(root_dir=root))
    shop.extract_job_ids = BulkManager.extract_job_ids
    mode = "dry-run" if scenario == "preview" else "apply-all"
    with patch.object(adapter, "ShopifyClient", return_value=shop), patch.object(
            adapter, "BulkManager", return_value=shop), patch.object(sys, "argv", ["workflow", mode, str(root)]):
        try:
            adapter.main()
            if scenario == "already-sorted":
                shop.events.append("second run")
                adapter.main()
        finally:
            (root / "observed.json").write_text(json.dumps([shop.events, shop.orders]))


@unittest.skipUnless((MANAGER / "app/shopify/bulk.py").is_file(), "requires the Data Manager checkout")
class RecoveryTests(unittest.TestCase):
    def exercise(self, scenario, success=True):
        from test_sorting import snapshot, write_rows
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        c, p, o = snapshot(root, 253)
        collections = [dict(row, sortOrder="MANUAL") for row in read_rows(c)]
        if scenario == "nonmanual":
            collections[0]["sortOrder"] = "BEST_SELLING"
        write_rows(root / "collections.jsonl", collections)
        p.rename(root / "catalog.jsonl")
        o.rename(root / "history.jsonl")
        (root / "config").mkdir()
        (root / "config/scoring.json").write_bytes((SORTER / "config/scoring.example.json").read_bytes())
        (root / "config/base_collections.json").write_text('{"base_collections":[10,12]}')
        (root / "collections.graphql").write_text("{ collections { edges { node { id } } } }")
        (root / "products.graphql").write_text('{ products(query: "collection_id:10") { edges { node { id } } } }')
        subprocess.run([sys.executable, str(SORTER / "automation.py"), "select", str(root)], check=True)
        env = dict(os.environ, PYTHONPATH=str(MANAGER), GITHUB_STEP_SUMMARY=str(root / "summary"))
        result = subprocess.run([sys.executable, __file__, str(root), scenario], env=env,
                                cwd=root, capture_output=True, text=True)
        if success:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
        events, live = json.loads((root / "observed.json").read_text())
        self.assertEqual(events.count("export orders"), 2 if scenario == "already-sorted" else 1)
        if success and scenario != "preview":
            for directory in (root / "bases").iterdir():
                orders = json.loads((directory / "plan.json").read_text())["orders"]
                for cid, expected in orders.items():
                    self.assertEqual(live[cid], expected)
        return events, result.stderr

    def test_before_write_reranks_only_changed_family(self):
        events, _ = self.exercise("before")
        self.assertEqual([e for e in events if e.startswith("export")],
                         ["export orders", "export 10", "export 10", "export 12"])
        self.assertLess(events.index("export 10", events.index("export 10") + 1), events.index("write " + BASE))

    def test_stale_collection_count_refreshes_before_first_write(self):
        events, _ = self.exercise("count-change")
        self.assertEqual(events.count("export 10"), 2)
        self.assertEqual(events.count("export 12"), 1)
        self.assertLess(events.index("export 10", events.index("export 10") + 1), events.index("write " + BASE))

    def test_repeated_live_run_verifies_without_writing_matching_orders(self):
        events, _ = self.exercise("already-sorted")
        self.assertFalse(any(e.startswith("write") for e in events[events.index("second run"):]))

    def test_after_write_reads_order_before_refresh_and_finishes(self):
        events, _ = self.exercise("after")
        start = events.index("membership changed")
        self.assertIn("write " + BASE, events[:start])
        end = events.index("export 10", start)
        self.assertGreaterEqual(events[start:end].count("read " + CHILD), 2)
        self.assertFalse(any(e.startswith("write") for e in events[start:end]))
        self.assertEqual(events.count("export 12"), 1)

    def test_conflicting_post_write_order_stops_before_next_family(self):
        events, error = self.exercise("conflict", False)
        self.assertIn("differs from previously applied positions", error)
        self.assertNotIn("export 12", events)
        self.assertFalse(any(e.startswith("write") for e in events[events.index("membership changed"):]))

    def test_second_post_write_change_stops_after_readback(self):
        events, error = self.exercise("after-twice", False)
        self.assertIn("MembershipChanged", error)
        self.assertNotIn("export 12", events)
        self.assertEqual(events.count("membership changed"), 2)
        end = len(events) - events[::-1].index("membership changed")
        self.assertIn("read " + CHILD, events[end:])

    def test_manual_child_of_nonmanual_base_is_reordered(self):
        events, _ = self.exercise("nonmanual")
        self.assertIn("write " + CHILD, events)
        self.assertNotIn("write " + BASE, events)

    def test_pre_write_retry_is_bounded(self):
        events, error = self.exercise("repeat", False)
        self.assertIn("MembershipChanged", error)
        self.assertEqual(events.count("export 10"), 2)
        self.assertFalse(any(e.startswith("write") for e in events))

    def test_mutation_error_stops_without_replay(self):
        events, error = self.exercise("mutation-error", False)
        self.assertIn("Shopify rejected", error)
        self.assertEqual(events.count("export 10"), 1)
        self.assertNotIn("export 12", events)

    def test_full_preview_does_not_write(self):
        events, _ = self.exercise("preview")
        self.assertEqual(events.count("export 12"), 1)
        self.assertFalse(any(e.startswith("write") for e in events))


if __name__ == "__main__":
    simulate(Path(sys.argv[1]), sys.argv[2])
