"""Offline entry point for the workflow's temporary run directory."""
from __future__ import annotations

import argparse
import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from app.exports import CollectionMembershipChanged, collection_family, read_collections
from app.output import BATCH_SIZE
from app.scoring import load_config
from main import rank_collection


def approved_ids(config):
    document = json.loads(config.read_text(encoding="utf-8"))
    ids = document.get("base_collections") if isinstance(document, dict) else None
    if (not isinstance(ids, list) or not ids or any(type(i) is not int or i <= 0 for i in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError("Expected a nonempty list of distinct positive integer base collection IDs.")
    return ids


def select_targets(base, collections):
    if base not in collections or collections[base].get("filter.base_collection"):
        raise ValueError("An approved ID is missing or identifies a filtered collection.")
    family = collection_family(base, collections)
    targets = []
    for cid in family:
        row = collections[cid]
        count = row["productsCount"]
        if (not isinstance(count, dict) or count.get("precision") != "EXACT"
                or type(count.get("count")) is not int or count["count"] < 0):
            raise ValueError("An exact product count is required to validate a family.")
        if not isinstance(row["sortOrder"], str) or not row["sortOrder"]:
            raise ValueError("Missing sortOrder on a collection in an approved family.")
        if row["sortOrder"] == "MANUAL" and count["count"]:
            targets.append(cid)
    return targets


def validate_batches(destination, targets, family):
    """Compare the serialized files with the intended products, positions and batch bounds."""
    batches = sorted(destination.glob("*.csv"), key=lambda p: int(p.name.split("_")[1]))
    largest = max(len(family[cid]) for cid in targets)
    if len(batches) != (largest + BATCH_SIZE - 1) // BATCH_SIZE:
        raise ValueError("Missing or unexpected reorder batch.")
    for start, path in zip(range(0, largest, BATCH_SIZE), batches):
        expected = {(cid, str(i)): pid for cid in targets
                    for i, pid in enumerate(family[cid][start:start + BATCH_SIZE], start)}
        with path.open(encoding="utf-8", newline="") as stream:
            rows = csv.reader(stream)
            if next(rows, []) != ["id", "parentId", "newPosition"]:
                raise ValueError("Invalid reorder CSV header.")
            for pid, cid, position in rows:
                if expected.pop((cid, position), None) != pid:
                    raise ValueError("Unexpected product, collection or position in reorder CSV.")
        if expected:
            raise ValueError("Incomplete reorder CSV.")
    return batches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("select", "rank"))
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base", type=int, help="Rank one previously selected family.")
    args = parser.parse_args()
    root = args.directory
    profiles = load_config(root / "config/scoring.json")
    ids = approved_ids(root / "config/base_collections.json")
    collection_file = root / "collections.jsonl"
    if args.base is not None:
        if args.stage != "rank" or args.base not in ids:
            raise ValueError("Cannot rank an unapproved base.")
        ids = [args.base]
        collection_file = root / "bases" / str(args.base) / "collections.jsonl"
    collections = read_collections(collection_file)
    as_of = datetime.now(timezone.utc).date()
    batches = []
    pilot = root / "config/pilot.txt"
    pilot_targets = []
    if pilot.exists():
        base_handle, child_handle = (s.strip() for s in pilot.read_text().split(","))
        by_handle = {row["handle"]: gid for gid, row in collections.items()}
        base_id, child_id = by_handle[base_handle], by_handle[child_handle]
        number = int(base_id.rsplit("/", 1)[1])
        if number not in ids or base_id == child_id:
            raise ValueError("Pilot requires an approved base and a distinct child.")
        ids = [number]
        pilot_targets = [base_id, child_id]
    for number in ids:
        base = f"gid://shopify/Collection/{number}"
        targets = select_targets(base, collections)
        if pilot_targets:
            if not set(pilot_targets).issubset(targets):
                raise ValueError("Both pilot collections must be eligible members of the same family.")
            targets = pilot_targets
        directory = root / "bases" / str(number)
        if args.stage == "select":
            if targets:
                directory.mkdir(parents=True)
                (directory / "scope.json").write_text(json.dumps(collection_family(base, collections)))
            continue
        if not targets:
            if args.base is not None:
                empty_orders = {cid: [] for cid in collection_family(base, collections)
                                if collections[cid]["sortOrder"] == "MANUAL"}
                (directory / "plan.json").write_text(json.dumps(
                    {"orders": empty_orders, "base_members": None, "batches": []}))
            continue
        files = {"Collection": collection_file, "Order": root / "orders.jsonl",
                 "Product": directory / "products.jsonl"}
        destination, family = rank_collection(files, directory, profiles, collections, base, as_of,
                                              targets=targets)
        paths = [str(p.relative_to(root)) for p in validate_batches(destination, targets, family)]
        batches.extend(paths)
        if args.base is not None:
            # Include empty manual children so a newly populated target triggers a refresh.
            monitored = pilot_targets or [cid for cid in family if collections[cid]["sortOrder"] == "MANUAL"]
            plan = {"orders": {cid: family[cid] for cid in monitored},
                    "base_members": family[base], "batches": paths}
            (directory / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
        if pilot_targets and args.base is None:
            expected = {cid: family[cid] for cid in targets}
            (root / "pilot-orders.json").write_text(json.dumps(expected), encoding="utf-8")
    if args.stage == "rank" and args.base is None:
        # Publish the exact batch list only after every family's files have passed validation.
        (root / "batches.json").write_text(json.dumps(batches), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except CollectionMembershipChanged:
        raise SystemExit(75)  # Family needs a fresh collection/product snapshot.
