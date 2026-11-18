"""Workflow adapter. Run with the pinned Data Manager checkout on PYTHONPATH."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from app.shopify.bulk import BulkManager, SPECS
from app.shopify.client import ShopifyClient
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector
from app.source import load_source
from app.workflows.update_resources import UpdateResourcesPlanner
from app.utils import write_jsonl
from config import Settings


def export(bulk, query_path, destination):
    # Data Manager records every query. Keep only the complete JSONL in this disposable run.
    result_dir = bulk.run_query(query_path)
    manifest_path, = result_dir.glob("manifest_*.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    operation = manifest["operation"]
    result = result_dir / manifest["result"]
    size = result.stat().st_size
    if (manifest["status"] != "COMPLETED" or operation["status"] != "COMPLETED"
            or operation.get("errorCode") or not operation.get("url")
            or not size or size != int(operation["fileSize"])):
        raise ValueError("Incomplete bulk export.")
    result.rename(destination)
    shutil.rmtree(result_dir)


def check_live_order(relationships, expected, verified):
    live = relationships.collection_contents(expected)
    for cid, order in expected.items():
        state = live.get(cid, {})
        current = state.get("products", [])
        end = verified[cid]
        if (state.get("sortOrder") != "MANUAL" or len(current) != len(order)
                or set(current) != set(order) or current[:end] != order[:end]):
            raise ValueError("Pilot membership, sort mode or verified positions changed.")


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


def reorder_batches(client, root, *, apply_with=None):
    relationships = RelationshipResolver(client)
    planner = UpdateResourcesPlanner(SchemaInspector(client), relationships)
    if apply_with is not None:
        expected = json.loads((root / "pilot-orders.json").read_text(encoding="utf-8"))
        if len(expected) != 2 or not all(expected.values()):
            raise ValueError("Expected exactly two nonempty pilot collections.")
        verified = dict.fromkeys(expected, 0)
        check_live_order(relationships, expected, verified)
    for batch in json.loads((root / "batches.json").read_text(encoding="utf-8")):
        with TemporaryDirectory(dir=root) as source:
            os.link(root / batch, Path(source) / "reorder.csv")
            data = load_source(Path(source), "id", allow_collection_reorder_duplicates=True)
            if data.columns != ["parentId", "newPosition"]:
                raise ValueError("Unexpected columns in reorder CSV.")
            plan = planner.plan(data)
            if plan.errors or plan.warnings or plan.mutations.keys() - {"collection_reorder"}:
                raise ValueError("Data Manager reorder preflight failed.")

            if apply_with is not None:
                # Only submit a consecutive slice of the two validated target orders.
                for row in data.rows:
                    cid = row.values["parentId"]
                    position = int(row.values["newPosition"])
                    if position != verified[cid] or expected[cid][position] != row.key:
                        raise ValueError("Batch is outside the expected pilot order.")
                    verified[cid] += 1
                moves = plan.mutations.get("collection_reorder", [])
                if any(payload["id"] not in expected for payload in moves):
                    raise ValueError("Unexpected collection in reorder plan.")
                submit_moves(apply_with, Path(source), moves)
                check_live_order(relationships, expected, verified)
    if apply_with is not None and any(verified[cid] != len(order) for cid, order in expected.items()):
        raise ValueError("Pilot did not verify every position.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("collections", "inputs", "preflight", "apply-pilot"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    with TemporaryDirectory(dir=root) as tmp:
        settings = Settings(root_dir=Path(tmp))
        client = ShopifyClient(settings)
        if args.stage == "preflight":
            reorder_batches(client, root)
            return
        bulk = BulkManager(client, settings)
        try:
            if args.stage == "apply-pilot":
                reorder_batches(client, root, apply_with=bulk)
                return
            if args.stage == "collections":
                export(bulk, root / "collections.graphql", root / "collections.jsonl")
                return
            bases = sorted((root / "bases").iterdir())
            if bases:
                export(bulk, root / "orders.graphql", root / "orders.jsonl")
            template = (root / "products.graphql").read_text(encoding="utf-8")
            for directory in bases:
                if not re.fullmatch(r"[1-9][0-9]*", directory.name):
                    raise ValueError("Invalid collection ID.")
                query, replacements = re.subn(r'(?<=query: "collection_id:)[0-9]+(?=")',
                                              directory.name, template)
                if replacements != 1:
                    raise ValueError("Expected exactly one numeric collection_id in the saved products query.")
                query_path = settings.root_dir / "products.graphql"
                query_path.write_text(query, encoding="utf-8")
                export(bulk, query_path, directory / "products.jsonl")
        finally:
            bulk.close()


if __name__ == "__main__":
    main()
