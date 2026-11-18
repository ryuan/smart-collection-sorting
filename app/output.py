from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

BATCH_SIZE = 250


def finish_sheet(sheet, widths):
    sheet.freeze_panes = "C2" if sheet.title == "Master ranking" else "A2"
    sheet.auto_filter.ref = sheet.dimensions
    for i, cell in enumerate(sheet[1], 1):
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="21504B")
        sheet.column_dimensions[get_column_letter(i)].width = max(widths.get(i, 14), len(cell.value) + 3)
    # Shopify text (including titles starting with '=') is always literal workbook data.
    for row in sheet.iter_rows(min_row=2):
        for cell in row:
            if cell.data_type in ("f", "e"):
                cell.data_type = "s"


def write_review(path, family, collections, products, scores, as_of, history, files):
    workbook = Workbook()
    review = workbook.active
    review.title = "Master ranking"
    selected = next(iter(family))
    labels = {"stock_reward": "Stock", "value_reward": "Value", "decaying_reward": "Decay",
              "repeat_value_penalty": "Repeat"}
    columns = [(section, field) for section in labels
               for field in sorted({field for score in scores.values() for field in score["points"][section]})]
    stock_fields = sorted({field for score in scores.values() for field in score["stocked_values"]})
    review.append(["Rank", "Product", "Adjusted score", "Base score", "Repeat penalty", "Vendor",
                   "Product type", "Handle", "Product ID", "Total inventory", "Stocked variants", "Variants",
                   "Net units in export", *[f"{labels[s]} · {f}" for s, f in columns],
                   *[f"Stocked values · {field}" for field in stock_fields]])
    for i, gid in enumerate(family[selected], 1):
        p, score = products[gid], scores[gid]
        points = score["points"]
        review.append([i, p["title"], score["adjusted"], score["base"], sum(points["repeat_value_penalty"].values()),
                       p["vendor"], p["productType"], p["handle"], gid, p["totalInventory"], score["stocked"],
                       len(p["variants"]), score["units"], *[points[s].get(f, 0) for s, f in columns],
                       *[", ".join(sorted(score["stocked_values"].get(field, ()))) for field in stock_fields]])
    for row in review.iter_rows(min_row=2):
        for cell in (*row[2:5], *row[13:13 + len(columns)]):
            cell.number_format = "0.00"
    finish_sheet(review, {2: 58, 6: 23, 7: 20, 8: 44, 9: 40})

    ranks = {gid: i for i, gid in enumerate(family[selected], 1)}
    sheet = workbook.create_sheet("Collection positions")
    sheet.append(["Collection", "Rank", "Master rank", "Product", "Collection handle", "Collection ID", "Product ID"])
    for cid, order in family.items():
        c = collections[cid]
        for i, gid in enumerate(order, 1):
            sheet.append([c["title"], i, ranks[gid], products[gid]["title"], c["handle"], cid, gid])
    finish_sheet(sheet, {1: 36, 4: 58, 5: 36, 6: 40, 7: 40})

    info = workbook.create_sheet("Run details")
    info.append(["Setting", "Value"])
    for key, value in [("Selected collection", collections[selected]["title"]), ("Collection ID", selected),
                       ("Scoring date (UTC)", as_of.isoformat()),
                       ("Orders in export", history[0]), ("Earliest processed order", history[1].isoformat()),
                       ("Latest processed order", history[2].isoformat()),
                       *((f"{kind} export", file.name) for kind, file in sorted(files.items()))]:
        info.append([key, value])
    finish_sheet(info, {1: 32, 2: 72})
    workbook.save(path)


def write_outputs(directory: Path, family, collections, products, scores, as_of, history, files, *, targets=None):
    directory.mkdir(exist_ok=True, parents=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
    destination = directory / f"{stamp}_{collections[next(iter(family))]['handle']}"
    # Publish the workbook and all batches together after every write succeeds.
    with TemporaryDirectory(dir=directory) as temporary:
        stage = Path(temporary) / "run"
        stage.mkdir()
        write_review(stage / f"review_{stamp}.xlsx", family, collections, products, scores, as_of, history, files)
        orders = family if targets is None else {cid: family[cid] for cid in targets}
        for start in range(0, max(map(len, orders.values()), default=0), BATCH_SIZE):
            with (stage / f"reorder_{start // BATCH_SIZE + 1:03d}_{stamp}.csv").open("w", encoding="utf-8", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["id", "parentId", "newPosition"])
                for cid, order in orders.items():
                    writer.writerows((gid, cid, i) for i, gid in enumerate(order[start:start + BATCH_SIZE], start))
        stage.rename(destination)
    return destination
