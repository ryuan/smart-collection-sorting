"""Read the three Data Manager bulk results. Connections are rows with __parentId."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

GID = re.compile(r"gid://shopify/(\w+)/[0-9]+")
ROOTS = {"Collection", "Product", "Order"}

# Minimum selections in the supplied queries. Null metafields have no returned identity.
PRODUCT_FIELDS = {
    "catalog.combined_listing_color", "catalog.combined_listing_shape",
    "filter.sub_type", "filter.design", "filter.construction", "filter.color", "filter.material",
    "filter.pattern", "filter.style", "filter.firmness", "filter.feature",
}
VARIANT_FIELDS = {
    "filter.width", "filter.depth", "filter.height", "filter.seat_depth", "filter.seat_height",
    "filter.thickness", "filter.size", "filter.tv_size", "filter.shape", "filter.seating_capacity",
    "filter.orientation",
}
COLLECTION_FIELDS = {"filter.base_collection", "filter.filtered_collections"}


def jsonl_rows(path):
    with path.open(encoding="utf-8-sig") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                match = GID.fullmatch(str(row.get("id", ""))) if isinstance(row, dict) else None
                if not match:
                    raise ValueError("Expected a Shopify node with a GID.")
                if "__parentId" in row and not GID.fullmatch(str(row["__parentId"])):
                    raise ValueError("Invalid __parentId. Expected a Shopify GID.")
            except ValueError as exc:
                raise ValueError(f"{path.name}, line {number}: {exc}") from exc
            yield number, match[1], row


def identify_exports(directory: Path):
    files = {}
    for path in sorted(directory.glob("*.jsonl")):
        # Membership rows have Collection IDs, so only a root identifies the file.
        root = next((kind for _, kind, row in jsonl_rows(path) if "__parentId" not in row), None)
        if root not in ROOTS:
            raise ValueError(f"{path.name}: empty or unrecognized export (no identifiable root).")
        if root in files:
            raise ValueError(f"Duplicate {root} exports: {files[root].name} and {path.name}.")
        files[root] = path
    missing = ROOTS - files.keys()
    if missing:
        raise ValueError(f"Missing JSONL export in {directory}: {', '.join(sorted(missing))}.")
    return files


def fields(row, required, counts=()):
    missing = set(required) - row.keys()
    if missing:
        raise ValueError(f"Missing {', '.join(sorted(missing))} on {row['id']}.")
    for key in counts:
        if type(row[key]) is not int:
            raise ValueError(f"Expected an integer for {key} on {row['id']}.")


def utc_day(value, gid):
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if instant.tzinfo is None:
            raise ValueError
        return instant.astimezone(timezone.utc).date()
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"Invalid Shopify timestamp on {gid}: {value!r}.") from exc


def metafields(row, native, required):
    """Read aliased singular metafields inline, using their returned identity."""
    result = {}
    nulls = 0
    for alias in row.keys() - set(native):
        field = row[alias]
        if field is None:
            nulls += 1
            continue
        if not isinstance(field, dict) or not all(isinstance(field.get(k), str) for k in ('namespace', 'key', 'value')):
            raise ValueError(f"Expected namespace, key and value on {row['id']} ({alias}).")
        name = f"{field['namespace']}.{field['key']}"
        value = field["value"].strip()
        values = json.loads(value) if value.startswith("[") else [value]
        if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
            raise ValueError(f"Expected string or list of strings for {name} on {row['id']}.")
        result[name] = frozenset(item.strip() for item in values if item.strip())
    if len(required - result.keys()) > nulls:
        raise ValueError(f"Incomplete metafield selections on {row['id']}. Use the supplied query.")
    return result


def unique_add(mapping, key, value):
    if key in mapping:
        raise ValueError(f"Duplicate export row for {key}.")
    mapping[key] = value


def count_matches(count, actual):
    if not isinstance(count, dict) or type(count.get("count")) is not int:
        return False
    return (count.get("precision") == "EXACT" and actual == count["count"]
            or count.get("precision") == "AT_LEAST" and actual >= count["count"])


def check_count(node, field, actual):
    count = node[field]
    if not count_matches(count, actual):
        raise ValueError(f"Incomplete or incompatible export for {node['id']}: {field} {count}, found {actual}.")


def read_collections(path):
    result = {}
    native = ("id", "handle", "title", "productsCount")
    for number, kind, row in jsonl_rows(path):
        if kind != "Collection" or "__parentId" in row:
            raise ValueError(f"{path.name}, line {number}: expected a root Collection.")
        fields(row, native)
        handle = row["handle"]
        if not isinstance(handle, str) or not handle or "/" in handle or not isinstance(row["title"], str):
            raise ValueError(f"{path.name}, line {number}: invalid collection handle or title.")
        links = metafields(row, (*native, "sortOrder"), COLLECTION_FIELDS)
        if links.keys() - COLLECTION_FIELDS:
            raise ValueError(f"{path.name}: unexpected collection relationship field.")
        unique_add(result, row["id"], {**{k: row[k] for k in native}, "sortOrder": row.get("sortOrder"), **links})
    return result


def read_products(path):
    products, variants = {}, {}
    memberships = defaultdict(set)
    native = ("id", "title", "handle", "vendor", "productType", "createdAt", "totalInventory", "variantsCount")
    variant_native = ("id", "__parentId", "sku", "inventoryQuantity")
    for number, kind, row in jsonl_rows(path):
        gid, parent = row["id"], row.get("__parentId")
        if kind == "Product" and parent is None:
            fields(row, native, ("totalInventory",))
            if not all(isinstance(row[k], str) for k in ("title", "handle", "vendor", "productType", "createdAt")):
                raise ValueError(f"{path.name}, line {number}: invalid product identity or createdAt.")
            data = {k: row[k] for k in native}
            data["createdAt"] = utc_day(row["createdAt"], gid)
            unique_add(products, gid, {**data, **metafields(row, native, PRODUCT_FIELDS), "variants": []})
        elif kind == "ProductVariant" and parent:
            fields(row, variant_native, ("inventoryQuantity",))
            if row["sku"] is not None and not isinstance(row["sku"], str):
                raise ValueError(f"Invalid SKU on {gid}.")
            data = {k: row[k] for k in variant_native}
            data.update(metafields(row, variant_native, VARIANT_FIELDS))
            unique_add(variants, gid, data)
        elif kind == "Collection" and parent:
            if gid in memberships[parent]:
                raise ValueError(f"Repeated membership for {gid} on {parent}.")
            memberships[parent].add(gid)
        else:
            raise ValueError(f"{path.name}, line {number}: unexpected {kind} or missing parent.")
    for variant in variants.values():
        parent = variant["__parentId"]
        if parent not in products:
            raise ValueError(f"Variant {variant['id']} has missing parent {parent}.")
        products[parent]["variants"].append(variant)
    if memberships.keys() - products.keys():
        raise ValueError(f"{path.name}: collection membership references a missing product.")
    for gid, product in products.items():
        check_count(product, "variantsCount", len(product["variants"]))
        product["collections"] = memberships[gid]
    return products


def collection_family(selected, collections):
    children = collections[selected].get("filter.filtered_collections", ())
    if selected in children:
        raise ValueError(f"Collection {selected} links to itself.")
    for gid in children:
        if gid not in collections or collections[gid].get("filter.base_collection") != {selected}:
            raise ValueError(f"Missing or conflicting filter.base_collection for linked collection {gid}.")
    return [selected, *sorted(children)]


class CollectionMembershipChanged(ValueError):
    """The collection snapshot and the product memberships disagree."""


def collection_orders(selected, collections, products, ranking):
    if len(ranking) != len(products) or set(ranking) != products.keys():
        raise ValueError("Ranking must contain every exported product exactly once.")
    family = {}
    for gid in collection_family(selected, collections):
        members = [pid for pid in ranking if gid in products[pid]["collections"]]
        if gid == selected and len(members) != len(products):
            raise CollectionMembershipChanged("Products export includes products outside the selected collection. Check collection_id.")
        count = collections[gid]["productsCount"]
        if isinstance(count, dict) and count.get("precision") == "EXACT" and type(count.get("count")) is int:
            if count["count"] != len(members):
                raise CollectionMembershipChanged(f"Incomplete collection membership for {gid}.")
        else:
            check_count(collections[gid], "productsCount", len(members))
        family[gid] = members
    return family


def read_sales(path, products, as_of):
    sku_owner = {}
    for gid, product in products.items():
        for variant in product["variants"]:
            sku = variant["sku"]
            if sku and sku_owner.setdefault(sku, gid) != gid:
                raise ValueError(f"SKU {sku!r} belongs to two selected products; sales attribution is ambiguous.")

    orders, lines = {}, set()
    sold = Counter()
    for number, kind, row in jsonl_rows(path):
        if kind == "Order" and "__parentId" not in row:
            fields(row, ("processedAt", "cancelledAt", "test"))
            day = utc_day(row["processedAt"], row["id"])
            if type(row["test"]) is not bool:
                raise ValueError(f"Invalid test order flag on {row['id']}.")
            if row["cancelledAt"] is not None:
                utc_day(row["cancelledAt"], row["id"])
            unique_add(orders, row["id"], (day, row["test"] or row["cancelledAt"] is not None))
        elif kind == "LineItem" and row.get("__parentId"):
            fields(row, ("sku", "currentQuantity"), ("currentQuantity",))
            if row["currentQuantity"] < 0 or row["id"] in lines:
                raise ValueError(f"{path.name}, line {number}: duplicate line item or invalid net quantity.")
            if row["sku"] is not None and not isinstance(row["sku"], str):
                raise ValueError(f"Invalid SKU on {row['id']}.")
            lines.add(row["id"])
            sold[row["__parentId"], sku_owner.get(row["sku"])] += row["currentQuantity"]
        else:
            raise ValueError(f"{path.name}, line {number}: unexpected order export row.")
    if not lines:
        raise ValueError(f"{path.name}: no line items. Check the orders query and export.")
    sales = defaultdict(Counter)
    for (order, gid), units in sold.items():
        if order not in orders:
            raise ValueError(f"Line item has missing order {order}.")
        day, excluded = orders[order]
        if gid and not excluded and day <= as_of:
            sales[gid][day] += units
    days = [day for day, _ in orders.values()]
    return sales, (len(days), min(days), max(days))
