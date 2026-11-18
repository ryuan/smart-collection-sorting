import csv
import json
import unittest
from datetime import date, timedelta
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from openpyxl import load_workbook

from app.exports import PRODUCT_FIELDS, VARIANT_FIELDS, collection_orders, identify_exports, read_collections, read_products, read_sales
from app.scoring import load_config, override, product_score, product_values, rank_products
from main import run, select_collection

DAY = date(2026, 11, 16)
BASE = "gid://shopify/Collection/10"
CHILD = "gid://shopify/Collection/11"
OTHER = "gid://shopify/Collection/12"


def metafield(name, value):
    namespace, key = name.split(".")
    return {"namespace": namespace, "key": key, "value": json.dumps(value) if isinstance(value, list) else value}


def count(n):
    return {"count": n, "precision": "EXACT"}


def write_rows(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def snapshot(folder, n=3):
    """Mixed product types, an explicitly linked child and an unrelated co-membership."""
    collection_file, product_file, order_file = (folder / f"result_{x}.jsonl" for x in ("c", "p", "o"))
    write_rows(collection_file, [
        {"id": BASE, "handle": "family", "title": "Family", "productsCount": count(n),
         "parent": None, "children": metafield("filter.filtered_collections", [CHILD])},
        {"id": CHILD, "handle": "subset", "title": "Subset", "productsCount": count((n + 1) // 2),
         "parent": metafield("filter.base_collection", BASE), "children": None},
        {"id": OTHER, "handle": "curated", "title": "Curated", "productsCount": count(n), "parent": None, "children": None},
    ])
    rows = []
    for i in range(n):
        gid = f"gid://shopify/Product/{i+1}"
        rows.extend([{"id": gid, "handle": f"item-{i}", "title": f"=Item {i}", "vendor": "Example Home",
                      "productType": ("Beds", "Vases", "Area Rugs")[i % 3], "createdAt": "2020-01-15T00:00:00Z",
                      "totalInventory": 6, "variantsCount": count(1),
                      **{key.replace(".", "_"): None for key in PRODUCT_FIELDS - {"filter.color"}},
                      "arbitraryAlias": metafield("filter.color", ["Red", "Beige"])},
                     {"id": f"gid://shopify/ProductVariant/{i+1}", "__parentId": gid, "sku": f"SKU{i}",
                      **{key.replace(".", "_"): None for key in VARIANT_FIELDS - {"filter.size"}},
                      "inventoryQuantity": 6, "anotherAlias": metafield("filter.size", "Queen")},
                     {"id": BASE, "__parentId": gid}, {"id": OTHER, "__parentId": gid}])
        if i % 2 == 0:
            rows.append({"id": CHILD, "__parentId": gid})
    write_rows(product_file, reversed(rows))
    rows = []
    for i, (day, units, cancelled, test) in enumerate([(DAY, 500, None, False), (DAY - timedelta(days=365), 4, None, False),
                                                      (DAY - timedelta(days=730), 8, None, False),
                                                      (DAY, 99, "2026-11-16T02:00:00Z", False),
                                                      (DAY, 99, None, True), (DAY + timedelta(days=1), 99, None, False)]):
        order = f"gid://shopify/Order/{i+1}"
        rows.extend([{"id": order, "processedAt": day.isoformat() + "T00:00:00Z", "cancelledAt": cancelled, "test": test},
                     {"id": f"gid://shopify/LineItem/{i+1}", "__parentId": order, "sku": "SKU0", "currentQuantity": units}])
    write_rows(order_file, reversed(rows))
    return collection_file, product_file, order_file


class SortingTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.c, self.p, self.o = snapshot(self.folder)
        self.config = self.folder / "scoring.json"
        self.config.write_text(json.dumps({
            "default": {
                "stock_reward": {"totalInventory": 64},
                "value_reward": {"vendor": {"Example Home": 12}},
                "decaying_reward": {"createdAt": {"reward": 8, "decay_period": 730},
                                    "sales": {"reward": 0.25, "decay_period": 730}},
                "repeat_value_penalty": {"filter.color": -1, "catalog.combined_listing_color": -4},
            },
            "Beds": {"stock_reward": {"totalInventory": 0, "filter.size": {"Queen": 24, "King": 24, "Full": 16}}},
            "Vases": {"stock_reward": {"inventoryQuantity": 6}},
        }), encoding="utf-8")
        self.profiles = load_config(self.config)
        self.products = read_products(self.p)

    def test_stock_inheritance_distinct_values_and_no_option_fallback(self):
        bed, vase, rug = [self.products[f"gid://shopify/Product/{i}"] for i in range(1, 4)]
        original = bed["variants"][0]
        bed["variants"] += [dict(original), dict(original, **{"filter.size": frozenset({"King"})}),
                            dict(original, inventoryQuantity=0, **{"filter.size": frozenset({"Full"})}),
                            {"inventoryQuantity": 8, "selectedOptions": [{"name": "Size", "value": "Full"}]}]
        score = product_score(bed, self.profiles["Beds"], {}, DAY)
        self.assertEqual(score["points"]["stock_reward"], {"totalInventory": 0, "filter.size": 48})
        self.assertEqual(score["stocked"], 4)
        vase["variants"] *= 2
        self.assertEqual(product_score(vase, self.profiles["Vases"], {}, DAY)["base"], 64 + 12 + 12)
        custom = override(self.profiles["default"], {"value_reward": {"filter.color": {"Red": 4, "Beige": -1},
                                                                      "vendor": {"Sample Furnishings": 0}}})
        self.assertEqual(product_score(rug, custom, {}, DAY)["base"], 64 + 12 + 3)

    def test_title_first_word_normalization_and_reserved_prefix(self):
        product = next(iter(self.products.values()))
        for title, expected in [
            ("  ALENA   Wood Bed  ", {"alena"}),
            ("\tAlena\nWood Bed", {"alena"}),
            ("\u00a0Straße Bed", {"strasse"}),
            ("Alena-Wood Bed", {"alena-wood"}),
            ("", set()),
            (" \t\n ", set()),
        ]:
            with self.subTest(title=title):
                product["title"] = title
                product["__title_first_word"] = "exported value"
                self.assertEqual(product_values(product, "__title_first_word"), expected)
        product["__unknown"] = "exported value"
        with self.assertRaisesRegex(ValueError, "Unknown program-generated field"):
            product_values(product, "__unknown")

    def test_title_first_word_uses_existing_rewards_and_repeat_penalties(self):
        rows = [json.loads(line) for line in self.p.read_text().splitlines()]
        titles = ["  ALENA  Bed", "\talena Stool", "Brielle Vase"]
        for row in rows:
            if row["id"].startswith("gid://shopify/Product/"):
                row["title"] = titles[int(row["id"].rsplit("/", 1)[1]) - 1]
        write_rows(self.p, rows)
        products = read_products(self.p)
        config = json.loads(self.config.read_text())
        config["default"]["value_reward"]["__title_first_word"] = {"alena": 5, "brielle": 3}
        config["default"]["repeat_value_penalty"]["__title_first_word"] = -4
        self.config.write_text(json.dumps(config), encoding="utf-8")
        profiles = load_config(self.config)
        # Give the three products the same merchandising settings.
        for product in products.values():
            product["productType"] = "Area Rugs"
        ranked, scores = rank_products(products, profiles, {}, DAY)
        ids = sorted(products)
        self.assertEqual(ranked, [ids[0], ids[2], ids[1]])
        self.assertEqual(scores[ids[0]]["points"]["value_reward"]["__title_first_word"], 5)
        self.assertEqual(scores[ids[2]]["points"]["value_reward"]["__title_first_word"], 3)
        self.assertEqual(scores[ids[1]]["points"]["repeat_value_penalty"]["__title_first_word"], -4)
        products[ids[2]]["title"] = "ALENA Vase"
        ranked, scores = rank_products(products, profiles, {}, DAY)
        self.assertEqual(ranked, ids)
        self.assertEqual(scores[ids[2]]["points"]["repeat_value_penalty"]["__title_first_word"], -4)
        for product in products.values():
            product["title"] = "  "
        _, scores = rank_products(products, profiles, {}, DAY)
        for score in scores.values():
            self.assertEqual(score["points"]["value_reward"]["__title_first_word"], 0)
            self.assertNotIn("__title_first_word", score["points"]["repeat_value_penalty"])

    def test_uncapped_net_sales_dates_and_freshness(self):
        sales, span = read_sales(self.o, self.products, DAY)
        ranked, scores = rank_products(self.products, self.profiles, sales, DAY)
        bed = self.products[ranked[0]]
        self.assertEqual(bed["productType"], "Beds")
        self.assertEqual(scores[bed["id"]]["points"]["decaying_reward"]["sales"], 125.5)
        self.assertEqual(scores[bed["id"]]["units"], 512)
        self.assertEqual(span[0], 6)
        bed["createdAt"] = DAY - timedelta(days=365)
        self.assertEqual(product_score(bed, self.profiles["Beds"], {}, DAY)["points"]["decaying_reward"]["createdAt"], 4)

    def test_repeats_require_identical_nonempty_sets(self):
        ids = sorted(self.products)
        for i, gid in enumerate(ids):
            self.products[gid].update(productType="Area Rugs", **{
                "filter.color": frozenset(["Red", "Black"] if i < 2 else ["Red"]),
                "catalog.combined_listing_color": frozenset(ids[:2] if i < 2 else ids[1:])})
        ranked, scores = rank_products(self.products, self.profiles, {}, DAY)
        self.assertEqual(ranked, [ids[0], ids[2], ids[1]])
        self.assertEqual(scores[ids[1]]["base"] - 5, scores[ids[1]]["adjusted"])
        ranked, scores = rank_products({gid: self.products[gid] for gid in reversed(ids[:2])}, self.profiles, {}, DAY)
        self.assertEqual(ranked, ids[:2])
        self.assertEqual(scores[ids[1]]["points"]["repeat_value_penalty"], {"filter.color": -1, "catalog.combined_listing_color": -4})

    def test_repeat_window_expires_after_twelve_products(self):
        template = next(iter(self.products.values()))
        ids = [f"gid://shopify/Product/{i}" for i in range(15)]
        products = {
            gid: dict(template, id=gid, vendor=str(i), **{
                "catalog.combined_listing_color": frozenset(ids[:2]) if i < 2 else frozenset(),
            }) for i, gid in enumerate(ids)
        }
        profiles = {"default": {
            "stock_reward": {},
            "value_reward": {"vendor": {str(i): 100 - i for i in range(15)}},
            "decaying_reward": {},
            "repeat_value_penalty": {"catalog.combined_listing_color": -20},
        }}
        # A match twelve places back still penalizes the second family member.
        ranked, scores = rank_products({gid: products[gid] for gid in ids[:13]}, profiles, {}, DAY)
        self.assertEqual(ranked, [ids[0], *ids[2:13], ids[1]])
        self.assertEqual(scores[ids[1]]["points"]["repeat_value_penalty"], {"catalog.combined_listing_color": -20})
        # A twelfth intervening product moves the match outside the window.
        ranked, scores = rank_products(products, profiles, {}, DAY)
        self.assertEqual(ranked, [ids[0], *ids[2:14], ids[1], ids[14]])
        self.assertEqual(scores[ids[1]]["base"], scores[ids[1]]["adjusted"])
        # Several recent matches still apply the field's weight just once.
        for gid in ids[:3]:
            products[gid]["catalog.combined_listing_color"] = frozenset(ids[:3])
        ranked, scores = rank_products({gid: products[gid] for gid in ids[:3]}, profiles, {}, DAY)
        self.assertEqual(ranked, ids[:3])
        self.assertEqual(scores[ids[2]]["points"]["repeat_value_penalty"], {"catalog.combined_listing_color": -20})

    def test_linked_positions_batches_and_workbook(self):
        snapshot(self.folder, 302)
        result = run(self.folder, self.folder / "outputs", self.config, "family", DAY)
        batches = sorted(result.glob("*.csv"))
        self.assertEqual(len(batches), 2)
        stamp = result.name.removesuffix("_family")
        self.assertEqual([file.name for file in batches], [f"reorder_001_{stamp}.csv", f"reorder_002_{stamp}.csv"])
        orders = {BASE: [], CHILD: []}
        for file in batches:
            with file.open() as stream:
                rows = csv.DictReader(stream)
                self.assertEqual(rows.fieldnames, ["id", "parentId", "newPosition"])
                batch = list(rows)
            for cid, order in orders.items():
                entries = [r for r in batch if r["parentId"] == cid]
                self.assertLessEqual(len(entries), 250)
                for row in entries:
                    self.assertEqual(int(row["newPosition"]), len(order))
                    order.append(row["id"])
        self.assertEqual(len(orders[BASE]), 302)
        self.assertEqual(orders[CHILD], [g for g in orders[BASE] if int(g.split("/")[-1]) % 2])
        wb = load_workbook(result / f"review_{stamp}.xlsx", read_only=True)
        self.addCleanup(wb.close)
        self.assertEqual(wb["Collection positions"].max_row, 454)
        self.assertEqual(wb["Master ranking"]["B2"].data_type, "s")
        self.assertEqual(wb["Master ranking"]["I2"].value, orders[BASE][0])
        self.assertEqual(dict(wb["Run details"].values)["Orders in export"], 6)

    def test_numbered_collection_picker(self):
        collections = read_collections(self.c)
        with patch("builtins.input", return_value="1"), patch("sys.stdout", new_callable=StringIO) as menu:
            self.assertEqual(select_collection(None, collections, self.products), BASE)
        self.assertEqual(menu.getvalue().splitlines()[1:], ["[0] Curated (curated)", "[1] Family (family)"])

        collections[OTHER]["productsCount"] = count(len(self.products) + 1)
        with patch("builtins.input", return_value="0") as prompt, patch("sys.stdout", new_callable=StringIO) as menu:
            self.assertEqual(select_collection(None, collections, self.products), BASE)
        prompt.assert_called_once()
        self.assertEqual(menu.getvalue().splitlines()[1:], ["[0] Family (family)"])
        for selection in ("family", "10", BASE):
            self.assertEqual(select_collection(selection, collections, self.products), BASE)

    def test_individual_collection_selection(self):
        collections = read_collections(self.c)
        ids = list(self.products)
        self.assertEqual(collection_orders(OTHER, collections, self.products, ids), {OTHER: ids})
        members = {gid: p for gid, p in self.products.items() if CHILD in p["collections"]}
        self.assertEqual(collection_orders(CHILD, collections, members, list(members)), {CHILD: list(members)})

    def test_rejects_wrong_snapshot_and_invalid_exports(self):
        self.assertEqual(identify_exports(self.folder)["Product"], self.p)
        self.o.rename(self.folder / "unused.txt")
        with self.assertRaisesRegex(ValueError, "Missing JSONL.*Order"):
            identify_exports(self.folder)
        (self.folder / "copy.jsonl").write_bytes(self.p.read_bytes())
        with self.assertRaisesRegex(ValueError, "Duplicate Product"):
            identify_exports(self.folder)
        collections = read_collections(self.c)
        with self.assertRaisesRegex(ValueError, "outside the selected"):
            collection_orders(CHILD, collections, self.products, list(self.products))
        collections[BASE]["productsCount"] = count(4)
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            collection_orders(BASE, collections, self.products, list(self.products))
        rows = [json.loads(line) for line in self.p.read_text().splitlines()]
        variant = next(row for row in rows if "inventoryQuantity" in row)
        variant["anotherAlias"] = {"value": "Queen"}
        write_rows(self.p, rows)
        with self.assertRaisesRegex(ValueError, "namespace, key and value"):
            read_products(self.p)
        write_rows(self.p, [row for row in rows if row is not variant])
        with self.assertRaisesRegex(ValueError, "variantsCount"):
            read_products(self.p)


if __name__ == "__main__":
    unittest.main()
