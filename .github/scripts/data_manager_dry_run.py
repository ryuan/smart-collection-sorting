"""Workflow adapter. Run with the pinned Data Manager checkout on PYTHONPATH."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory

from app.shopify.bulk import BulkManager
from app.shopify.client import ShopifyClient
from app.shopify.relationships import RelationshipResolver
from app.shopify.schema import SchemaInspector
from app.source import load_source
from app.workflows.update_resources import UpdateResourcesPlanner
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


def preflight(client, root):
    planner = UpdateResourcesPlanner(SchemaInspector(client), RelationshipResolver(client))
    for batch in json.loads((root / "batches.json").read_text(encoding="utf-8")):
        with TemporaryDirectory(dir=root) as source:
            os.link(root / batch, Path(source) / "reorder.csv")
            data = load_source(Path(source), "id", allow_collection_reorder_duplicates=True)
            if data.columns != ["parentId", "newPosition"]:
                raise ValueError("Unexpected columns in reorder CSV.")
            plan = planner.plan(data)
            if plan.errors or plan.warnings or plan.mutations.keys() - {"collection_reorder"}:
                raise ValueError("Data Manager reorder preflight failed.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("collections", "inputs", "preflight"))
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    root = args.directory
    with TemporaryDirectory(dir=root) as tmp:
        settings = Settings(root_dir=Path(tmp))
        client = ShopifyClient(settings)
        if args.stage == "preflight":
            preflight(client, root)
            return
        bulk = BulkManager(client, settings)
        try:
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
