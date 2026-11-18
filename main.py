from __future__ import annotations

import argparse
import math
from datetime import date, datetime, timezone
from pathlib import Path

from app.exports import collection_orders, count_matches, identify_exports, read_collections, read_products, read_sales
from app.output import write_outputs
from app.scoring import load_config, rank_products

ROOT = Path(__file__).resolve().parent


def select_collection(selection, collections, products):
    if selection is None:
        common = set.intersection(*(p["collections"] for p in products.values()))
        eligible = sorted(
            (gid for gid in common & collections.keys() if count_matches(collections[gid]["productsCount"], len(products))),
            key=lambda gid: (collections[gid]["title"].casefold(), collections[gid]["handle"]),
        )
        if not eligible:
            raise ValueError("No collection matches the exported products and product count. Check the input exports.")
        print("Eligible collections:")
        for number, gid in enumerate(eligible):
            c = collections[gid]
            print(f"[{number}] {c['title']} ({c['handle']})")
        selection = input("Select a collection by number, handle or ID: ").strip()
        if selection.isascii() and selection.isdecimal() and 0 <= int(selection) < len(eligible):
            return eligible[int(selection)]
    matches = [gid for gid, c in collections.items() if selection in (gid, gid.rsplit("/", 1)[1], c["handle"])]
    if len(matches) != 1:
        raise ValueError(f"Unknown or ambiguous collection {selection!r}. Use its handle or full GID.")
    return matches[0]


def run(inputs, outputs, config, collection, as_of):
    files = identify_exports(inputs)
    profiles = load_config(config)
    collections = read_collections(files["Collection"])
    destination, _ = rank_collection(files, outputs, profiles, collections, collection, as_of)
    return destination


def rank_collection(files, outputs, profiles, collections, collection, as_of, *, targets=None):
    products = read_products(files["Product"])
    selected = select_collection(collection, collections, products)
    sales, history = read_sales(files["Order"], products, as_of)
    ranked, scores = rank_products(products, profiles, sales, as_of)
    if any(not math.isfinite(score[key]) for score in scores.values() for key in ("base", "adjusted")):
        raise ValueError("Ranking contains a nonfinite score.")
    family = collection_orders(selected, collections, products, ranked)
    destination = write_outputs(outputs, family, collections, products, scores, as_of, history, files, targets=targets)
    print(f"{len(ranked)} products in the master ranking, {len(family)} collection(s).")
    print(f"Orders in export: {history[0]}, dated {history[1]} through {history[2]}.")
    print(f"Review workbook and reorder CSVs: {destination}")
    return destination, family


def main():
    parser = argparse.ArgumentParser(description="Smart Collection Sorting — rank a Shopify collection offline.")
    parser.add_argument("--collection", help="Collection handle, numeric ID or GID. Prompts when omitted.")
    parser.add_argument("--as-of", type=date.fromisoformat, default=datetime.now(timezone.utc).date(),
                        help="Scoring date YYYY-MM-DD. Defaults to today in UTC.")
    args = parser.parse_args()
    try:
        run(ROOT / "inputs", ROOT / "outputs", ROOT / "config/scoring.json",
            args.collection, args.as_of)
    except (ValueError, OSError, EOFError) as exc:
        parser.exit(1, f"Sorting stopped: {exc}\n")


if __name__ == "__main__":
    main()
