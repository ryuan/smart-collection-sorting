"""Workflow adapter. Run with the pinned Data Manager checkout on PYTHONPATH."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.shopify.bulk import BulkManager, SPECS
from app.shopify.client import ShopifyClient
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector
from app.source import load_source
from app.workflows.update_resources import UpdateResourcesPlanner
from app.utils import iter_jsonl, write_jsonl
from config import Settings


def export(bulk, query_path, destination, *, allow_empty=False):
    # Data Manager records every query. Keep only the complete JSONL in this disposable run.
    result_dir = bulk.run_query(query_path)
    manifest_path, = result_dir.glob("manifest_*.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    operation = manifest["operation"]
    result = result_dir / manifest["result"]
    size = result.stat().st_size
    empty = (allow_empty and size == 0 and str(operation.get("objectCount")) == "0"
             and operation.get("fileSize") in (None, 0, "0"))
    if (manifest["status"] != "COMPLETED" or operation["status"] != "COMPLETED"
            or operation.get("errorCode") or operation.get("partialDataUrl")
            or not empty and (not size or not operation.get("url") or size != int(operation["fileSize"]))):
        raise ValueError("Incomplete bulk export.")
    result.rename(destination)
    shutil.rmtree(result_dir)


class MembershipChanged(ValueError):
    """Only this condition can restart a validated family."""


def verify_order(live, members, verified, *, allow_changed=False):
    """Check the confirmed prefix even when Shopify added or removed members."""
    for cid, order in members.items():
        state = live[cid]
        current = state["products"]
        if cid in verified and state["sortOrder"] != "MANUAL":
            raise ValueError("A target is no longer manually sorted.")
        present = set(current)
        if len(current) != len(present):
            raise ValueError("Ambiguous paginated Shopify order.")
        original = set(order)
        if original != present and not allow_changed:
            raise MembershipChanged("Collection membership changed.")
        end = verified.get(cid, 0)
        if end:
            prefix = [pid for pid in order[:end] if pid in present]
            survivors = [pid for pid in current if pid in original]
            if survivors[:len(prefix)] != prefix:
                raise ValueError("Shopify order differs from previously applied positions.")


def submit_moves(bulk, directory, moves):
    if not moves:
        return
    spec = SPECS["collection_reorder"]
    variables = directory / "moves.jsonl"
    write_jsonl(variables, moves)
    result = bulk.run_mutation(spec, Settings().mutation_definitions_dir / spec.graphql_filename,
                               variables, directory / "result.jsonl", str(uuid4()))
    if not result.completed or result.errors or not result.result_path or not result.operation.get("url"):
        raise ValueError("Shopify rejected a reorder batch or returned an incomplete result.")
    jobs = bulk.extract_job_ids(result.result_path, spec.response_field)
    if len(jobs) != len(moves) or len(set(jobs)) != len(moves):
        raise ValueError("Missing or duplicate Shopify reorder jobs.")


def scoped_metadata(client, template, scope):
    """Read the saved collection selections for just this family's known IDs."""
    rows = {}
    for start in range(0, len(scope), 50):
        group = scope[start:start + 50]
        if not all(re.fullmatch(r"gid://shopify/Collection/[0-9]+", cid) for cid in group):
            raise ValueError("Invalid collection scope.")
        filters = " OR ".join("id:" + cid.rsplit("/", 1)[1] for cid in group)
        query, replacements = re.subn(r"\bcollections\s*\{",
                                     f'collections(first: {len(group)}, query: "{filters}") {{', template)
        if replacements != 1:
            raise ValueError("Expected one collections connection in the saved query.")
        nodes = [edge["node"] for edge in client.execute(query)["collections"]["edges"]]
        if {row["id"] for row in nodes} != set(group) or len(nodes) != len(group):
            raise ValueError("Could not verify the complete collection scope.")
        rows.update((row["id"], row) for row in nodes)
    return rows


def verify_scope(snapshot, current):
    # Counts can change. A changed relationship or sort mode invalidates the write scope.
    for cid, row in snapshot.items():
        for key in row.keys() - {"productsCount", "title"}:
            if row[key] != current[cid][key]:
                raise ValueError("The selected collection family or sort mode changed.")


def rank_family(root, directory):
    # Both projects have an "app" package. Start the offline sorter with its own
    # import path and without the Data Manager credentials.
    script = Path(__file__).resolve().parents[2] / "automation.py"
    env = {k: os.environ[k] for k in ("PATH", "TMPDIR", "LANG") if k in os.environ}
    result = subprocess.run([sys.executable, str(script), "rank", str(root), "--base", directory.name], env=env)
    if result.returncode == 75:
        raise MembershipChanged("Product and collection exports disagree.")
    result.check_returncode()
    return json.loads((directory / "plan.json").read_text())


def plan_batch(planner, source, orders, positions, check_live):
    data = load_source(source, "id", allow_collection_reorder_duplicates=True)
    if data.columns != ["parentId", "newPosition"]:
        raise ValueError("Unexpected reorder columns.")
    for row in data.rows:
        cid, position = row.values["parentId"], int(row.values["newPosition"])
        if positions[cid] != position or orders[cid][position] != row.key:
            raise ValueError("Batch is outside the validated order.")
        positions[cid] += 1
    mutation = planner.plan(data)
    # The pinned planner reports a fully matched CSV as this single error.
    noop = not mutation.mutations and mutation.errors == [
        "All requested collection positions already match Shopify; no moves needed."]
    if mutation.warnings or mutation.mutations.keys() - {"collection_reorder"} or (mutation.errors and not noop):
        check_live()
        raise ValueError("Data Manager preflight failed.")
    moves = mutation.mutations.get("collection_reorder", [])
    if any(payload["id"] not in orders for payload in moves):
        raise ValueError("Unexpected collection in reorder plan.")
    return moves


def run_family(bulk, root, directory, snapshot, collection_query, product_query, apply):
    base = "gid://shopify/Collection/" + directory.name
    relationships = RelationshipResolver(bulk.client)
    planner = UpdateResourcesPlanner(SchemaInspector(bulk.client), relationships)
    refreshed_before = recovered_after = False
    last_write = None
    query_path = bulk.settings.root_dir / "products.graphql"
    query, count = re.subn(r'(?<=query: "collection_id:)[0-9]+(?=")', directory.name, product_query)
    if count != 1:
        raise ValueError("Expected one numeric collection_id in the products query.")
    query_path.write_text(query)
    metadata = snapshot

    def current_metadata():
        current = scoped_metadata(bulk.client, collection_query, list(snapshot))
        verify_scope(snapshot, current)
        return current

    while True:
        write_jsonl(directory / "collections.jsonl", metadata.values())
        try:
            export(bulk, query_path, directory / "products.jsonl", allow_empty=True)
            plan = rank_family(root, directory)
            batches, orders = plan["batches"], plan["orders"]
            if any(cid not in snapshot or snapshot[cid]["sortOrder"] != "MANUAL" for cid in orders):
                raise ValueError("Unexpected collection in offline plan.")
            members = dict(orders)
            if plan["base_members"] is not None:
                members[base] = plan["base_members"]
            verified = dict.fromkeys(orders, 0)
            prior_write = last_write

            def check_live():
                ids = members.keys() | prior_write[0].keys() if prior_write else members
                live = relationships.collection_contents(ids)
                verify_order(live, members, verified)
                if prior_write:
                    # The previous plan was partially applied. Recheck its surviving
                    # positions against the same fresh read until the next actual write.
                    verify_order(live, *prior_write, allow_changed=True)

            check_live()
            for writing in ([False, True] if apply else [False]):
                positions = dict.fromkeys(orders, 0)
                for batch in batches:
                    with TemporaryDirectory(dir=directory) as source:
                        source = Path(source)
                        os.link(root / batch, source / "reorder.csv")
                        moves = plan_batch(planner, source, orders, positions, check_live)
                        if writing:
                            current_metadata()
                            check_live()
                            if moves:
                                submit_moves(bulk, source, moves)
                                prior_write = None
                            verified = positions.copy()
                            if moves or (last_write and not prior_write):
                                last_write = members, verified
                            check_live()
                if any(positions[cid] != len(order) for cid, order in orders.items()):
                    raise ValueError("Incomplete family order.")
            if not apply:
                check_live()
            return len(batches), refreshed_before, recovered_after
        except MembershipChanged:
            if last_write is not None:
                # Never replay a submitted CSV. Independently verify the positions
                # already written before replacing them with a fresh family plan.
                previous_members, previous_positions = last_write
                live = relationships.collection_contents(previous_members)
                verify_order(live, previous_members, previous_positions, allow_changed=True)
                if recovered_after:
                    raise
                recovered_after = True
            else:
                if refreshed_before:
                    raise
                refreshed_before = True
            metadata = current_metadata()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("collections", "dry-run", "preview-pilot", "apply-pilot", "apply-all"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory.resolve()
    with TemporaryDirectory(dir=root) as tmp:
        settings = Settings(root_dir=Path(tmp))
        bulk = BulkManager(ShopifyClient(settings), settings)
        try:
            if args.stage == "collections":
                export(bulk, root / "collections.graphql", root / "collections.jsonl")
                return
            pilot = args.stage.endswith("-pilot")
            if pilot != (root / "config/pilot.txt").exists():
                raise ValueError("Run mode and private scope disagree.")
            bases = sorted((root / "bases").iterdir())
            if pilot and len(bases) != 1:
                raise ValueError("A pilot requires exactly one eligible base.")
            if bases:
                export(bulk, root / "orders.graphql", root / "orders.jsonl")
            snapshot = {row["id"]: row for row in iter_jsonl(root / "collections.jsonl")}
            collections = (root / "collections.graphql").read_text()
            products = (root / "products.graphql").read_text()
            apply = args.stage.startswith("apply-")
            total = refreshes = recoveries = 0
            for directory in bases:
                if not re.fullmatch(r"[1-9][0-9]*", directory.name):
                    raise ValueError("Invalid base ID.")
                scope = json.loads((directory / "scope.json").read_text())
                family = {cid: snapshot[cid] for cid in scope}
                try:
                    n, refreshed, recovered = run_family(bulk, root, directory, family, collections, products, apply)
                except Exception:
                    # Shopify error text can include private identifiers or input data.
                    with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                        print("Stopped while processing a family. Earlier successful moves may be visible."
                              if apply else "Stopped while validating a family.", file=summary)
                    raise
                total += n
                refreshes += refreshed
                recoveries += recovered
            action = "Verified live order for" if apply else "Previewed"
            with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a") as summary:
                print(f"{action} {total} CSV batches across {len(bases)} selected families. "
                      f"Pre-write refreshes: {refreshes}. Verified post-write recoveries: {recoveries}.", file=summary)
        finally:
            bulk.close()


if __name__ == "__main__":
    main()
