# Scoring configuration

Edit `scoring.json`. The `default` entry supplies the rules for every product. Add an entry named exactly like an exported Shopify `productType` to override rules for that type. Each product in a mixed collection uses its own effective settings.

Overrides merge dictionaries recursively. For example, changing only `Beds.value_reward.vendor.Modway` preserves the other vendor weights. An explicit zero disables the specified field or value. New keys add contributions. Put collection selection on the command line.

| Section | How points are calculated |
| --- | --- |
| `stock_reward` | A positive `totalInventory` earns its weight once. Each variant with positive `inventoryQuantity` earns the variant weight once. A variant metafield maps distinct values on stocked variants to weights, each paid once per product. |
| `value_reward` | Map a native field or canonical metafield to its value weights. Sum each distinct matching value once. Negative weights subtract points. For a variant field, use the values across all variants. |
| `decaying_reward` | Set `reward` and a positive `decay_period` in days for `createdAt` or `sales`. The multiplier is 1 on day zero, 0.5 halfway through, and 0 from the end of the period. Product creation earns the reward once. Each net unit sold earns its own dated reward. |
| `repeat_value_penalty` | Give a field a negative weight. Apply it once when the candidate's nonempty set exactly equals that field's set on any of the previous 12 ranked products, even if several match. Missing values and partial overlaps contribute zero. A scalar is a one-element set. |

Use Shopify's field names and exact exported values, including capitalization. Write a metafield as `namespace.key`, such as `filter.size`. The supplied query contains the documented product and variant filter fields plus both combined-listing fields. For an additional metafield, query it on its documented owner with `namespace key value` before assigning it a weight. The score for an absent value is zero. Product-reference lists use the same comparison as any other list.

The `__` prefix is reserved for program-generated fields. `__title_first_word` derives the first whitespace-separated word from the Shopify product title and normalizes it with Unicode case folding. For example, `  ALENA   Wood Bed` becomes `alena`. Punctuation stays part of the word. A blank title gives an empty value. This field supports the existing string-field rules in `value_reward` and `repeat_value_penalty`. Use normalized lowercase values in reward mappings. Unknown program-generated field names produce an error when read.

For example, merge these entries into the corresponding sections under `default` or a product type:

```json
{
  "value_reward": {
    "__title_first_word": {"alena": 3}
  },
  "repeat_value_penalty": {
    "__title_first_word": -2
  }
}
```

The reward contributes 3 points for an `alena` title. The penalty contributes −2 once when any of the previous 12 products has the same first word. The field is derived locally from the existing title export.

In the starting Beds settings, Queen and King each earn 24 stock points and Full earns 16. The Fields sheet in Supplier Product Detail Map lists Full, Queen, King and California King for Beds. California King has no starting weight. A stocked Queen plus a stocked King earns 48 even if several variants offer Queen. A product with no populated size metafields receives zero size points. Product types without an override, including Vases, use the default settings.

At each rank, the sorter chooses the remaining product with the highest base score plus the applicable repetition penalties. The first product has no predecessor. A tie in adjusted score goes to the higher base score, then the lexicographically smaller Product GID. Scores retain full precision during ranking. The workbook displays points to two decimal places. A score can exceed 100. Filtered collection positions inherit the master order, so they use the master's scoring context.

For a reproducible comparison, use the same three exports and the same `--as-of YYYY-MM-DD` date on both runs.
