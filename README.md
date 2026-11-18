# Smart Collection Sorting

**A configurable product ranking engine for Shopify collections.**

Smart Collection Sorting turns Shopify GraphQL bulk-operation exports into product rankings that balance **availability, merchandising priorities, sales history, recency, and assortment diversity**. It generates an Excel workbook for reviewing the results and CSV batches that can be applied to Shopify's manual collection order.

To rank locally, start with compatible exports from any tool. The ranking command runs offline and needs no Shopify credentials. The optional GitHub Actions workflow connects to Shopify to fetch fresh data, preview reorders, and apply them in live mode.

## Why it exists

Default collection sorting can place many similar products together, even when a store carries a diverse assortment. Sorting only by sales or inventory can create a different problem: newer products are hard to discover, and useful product-specific attributes may be ignored.

This project approaches collection ordering as a set of configurable merchandising tradeoffs:

- Promote products with relevant in-stock options, without automatically rewarding every additional variant.
- Give established sellers an advantage while allowing newer products to surface.
- Apply supplier or attribute preferences without making them absolute rules.
- Spread out related models, colors, materials, and linked listings so shoppers encounter more variety.

The goal is **a more useful browsing order**, not a rigid category quota or a single universal definition of relevance.

## How it works

```text
Shopify GraphQL bulk-operation exports (JSONL)
            |
            v
    Smart Collection Sorting  <---  config/scoring.json
            |
            +-- Master ranking for a selected collection
            |       \-- Linked filtered collections inherit this order
            |
            +-- Excel review workbook
            \-- CSV reorder batches
                        |
                        v
           External Shopify mutation process
```

For a selected base collection, the program evaluates the products in its export, constructs a master ranking, and derives the ordering of explicitly linked filtered collections from that same ranking. This keeps related collections consistent without independently reranking every subset.

### Ranking signals

| Signal | Purpose |
| --- | --- |
| **Stock rewards** | Prioritize availability, including specific variant sizes or other configured values. |
| **Value rewards** | Express preferences for vendors or product attributes. |
| **Decaying rewards** | Recognize new arrivals and historical sales while reducing the influence of older events. |
| **Repetition penalties** | Discourage recently selected products with matching attributes or linked-listing relationships. |

Repetition penalties consider the recently selected products (currently a **12-product lookback**). They are soft penalties, not hard spacing constraints: a strong product can still rank highly when other scoring signals justify it.

The program also exposes `__title_first_word`, a generated string field taken from the beginning of the product title. It can help identify product families even when products are not explicitly linked as combined listings. It is a heuristic, not a guaranteed model identifier.

## Getting started locally

### Requirements

- Python 3.9 or later
- Packages listed in `requirements.txt`
- Compatible Shopify Admin GraphQL bulk-operation exports in JSONL format

Install the dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create a local scoring configuration from the generic example:

```bash
cp config/scoring.example.json config/scoring.json
```

Edit `config/scoring.json` to reflect your assortment and priorities. The example uses fictional suppliers and illustrative weights; it is **not** a recommended production scoring policy.

### Prepare the exports

Supply these three types of Shopify bulk-query results in `inputs/`:

| Export | Information used |
| --- | --- |
| **Collections** | Collection IDs, membership information, and base/filtered relationships. |
| **Products** | Product and variant identities, inventory, catalog attributes, and associated collection data. |
| **Orders** | Order line items and sales history; the sorter associates sales with current variants using SKU. |

These files must follow the query/data contract expected by the program. The public project does not include any merchant's production exports. Keep the three exports from a consistent catalog snapshot when comparing runs.

### Run a ranking

Launch the interactive collection selector:

```bash
python3 main.py
```

Or select a collection by handle or ID:

```bash
python3 main.py --collection <collection-handle-or-id>
```

For a reproducible comparison, use a fixed scoring date along with unchanged exports and configuration:

```bash
python3 main.py --collection <collection-handle-or-id> --as-of 2026-01-15
```

The default scoring date is the current date in UTC.

## Configuration

The scoring file has a `default` block and optional collection/product-type overrides. A category can override a specific scoring component without requiring a completely separate ranking engine.

```json
{
  "default": {
    "stock_reward": { "totalInventory": 20 },
    "value_reward": {
      "vendor": { "Example Home": 8 }
    },
    "decaying_reward": {
      "createdAt": { "reward": 6, "decay_period": 365 },
      "sales": { "reward": 10, "decay_period": 730 }
    },
    "repeat_value_penalty": {
      "__title_first_word": -2,
      "filter.color": -2
    }
  },
  "Beds": {
    "stock_reward": {
      "totalInventory": 0,
      "filter.size": { "Queen": 10, "King": 10 }
    }
  }
}
```

**Important scoring behaviors:**

- Attribute-specific stock rewards are **additive**. Rewarding every size also rewards having a broader assortment of stocked sizes; choose the values deliberately.
- A category can set `totalInventory` to `0` when the goal is to prioritize specific available variants instead of general inventory.
- Sales rewards accumulate from matching order history and decay over time; a configured sales reward should not be interpreted as an absolute cap on the aggregate sales contribution.
- Repetition penalties are contextual: a product's effective score depends on products already selected in the ranking.
- The double-underscore prefix in `__title_first_word` distinguishes generated fields from Shopify-sourced fields.

See [the scoring configuration guide](config/README.md) for the complete set of supported fields and options, and [the generic scoring example](config/scoring.example.json) for a starting point.

## Optional GitHub Actions workflow

The workflow checks out a pinned version of [Shopify Data Manager](https://github.com/ryuan/shopify-data-manager) for Shopify exports, live checks, and reorder requests. It uses the same ranking code as the local command.

Choose **Actions → Collection reorder → Run workflow** on the default branch.

| Mode | Collections | Effect on Shopify |
| --- | --- | --- |
| `dry-run` (manual default) | All approved families | Checks proposed reorders against live data. |
| `preview-pilot` | One approved base and one linked filtered collection | Checks proposed reorders for the pair. |
| `apply-pilot` | The same pilot pair | Applies and verifies the product order. |
| `apply-all` | All approved families | Applies and verifies the product order for eligible collections. |

Only collections already using Shopify’s **Manual** sort order are eligible for writes. Publishing the workflow on the default branch schedules live `apply-all` runs at midnight and noon in `America/New_York`, following daylight saving time.

| Repository secret | Contents |
| --- | --- |
| `BASE_COLLECTIONS_JSON`, `SCORING_JSON` | Contents of the private base allowlist and scoring configuration. Update the corresponding secret when you edit a local file. |
| `SCS_COLLECTIONS_QUERY`, `SCS_PRODUCTS_QUERY`, `SCS_ORDERS_QUERY` | Saved queries for collection relationships and sort order, products and memberships, and order history. The products query needs one numeric `query: "collection_id:…"` filter. |
| `SHOPIFY_SHOP`, `SHOPIFY_API_VERSION`, `SHOPIFY_ACCESS_TOKEN`, `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET` | Shopify connection settings for the workflow. Live reordering also requires Shopify permission to reorder collection products. |
| `PILOT_COLLECTIONS` | Required for pilot modes only. Enter two comma-separated collection handles in the order `base-handle,child-handle`. The base must be allowlisted, the child linked in both directions, and both collections nonempty and sorted manually. |

The workflow exports order history once per run. For each approved base, it exports products, ranks the family, and checks every CSV batch. In live mode, it checks current positions before each batch, waits for Shopify to finish, and reads the order back. It verifies the complete order of each target collection before moving to the next family.

If product membership changes before the family's first write, the workflow refreshes that family's collection and product data and ranks it again, once. After a write, it stops the remaining batches and independently reads Shopify's order. Recovery requires the surviving products from the applied positions to still be in the expected order. The workflow then rebuilds that family's ranking from fresh data and resumes from Shopify's current positions. A second change during that recovery, an unverifiable order, or a Shopify mutation error stops the run. Earlier successful moves can already be visible.

Exports, rankings and CSVs use temporary runner storage. The workflow suppresses their contents in logs and uploads no artifacts.

## Local output

Each local run writes two types of files in a timestamped directory under `outputs/`.

- **`review_<timestamp>.xlsx`** — product rankings, component score breakdowns, collection positions, and run details for inspection.
- **`reorder_<batch>_<timestamp>.csv`** — Shopify reorder records with `id`, `parentId`, and zero-based `newPosition`, limited to 250 positions per collection per batch.

To apply a local ranking, submit the CSV batches in sequence through a compatible Shopify reorder tool. Each destination collection must already use Shopify’s **Manual** sort order.

Neither `inputs/` nor `outputs/` is committed to Git.

## Design principles

- **Explainable scores:** The review workbook exposes the contributions behind the result.
- **Simple configuration:** Store-specific priorities live in JSON rather than being hard-coded into the algorithm.
- **Consistent filtered collections:** Linked filtered collections retain their relative order from the selected master ranking.
- **Reproducibility:** Fixed exports, configuration, and `--as-of` date make runs comparable.

## Repository privacy

This public repository intentionally excludes merchant-specific scoring configurations, collection IDs, customer/order exports, generated output files, and secrets. The files ending in `.example.json` contain placeholder data for demonstration only.
