# shopify-product-copier

Copies one product page into a Shopify store (name, price, all variants, all pictures in order) with the Claude Agent SDK.

```
pip install claude-agent-sdk requests playwright
python copy_product.py <product-url>            # plain run, no LLM
python copy_product.py <product-url> --agent    # Claude agent drives the same tools
```

Set `SHOPIFY_STORE` (`x.myshopify.com`) and `SHOPIFY_TOKEN` to create the product for real. Without them, `create_product` writes the exact `productSet` payload to `out/`. Set `CHROMIUM_PATH` if Playwright's bundled browser is not installed.

## Tools
- `get_product_json` reads `/products/<handle>.json`. Variants, prices, compare-at prices and images sorted by `position`.
- `render_page` returns the visible page text after JavaScript runs. It's the fallback for pages that aren't Shopify, and the source for the price check.
- `create_product` runs one Admin GraphQL `productSet` call with options, variants and `files` in source order.
- `verify_copy` compares the variant count, image count and image order, then checks that each variant price is actually shown to shoppers.

## Loop stop
- `max_turns=12` and `max_budget_usd=0.50` in `ClaudeAgentOptions`.
- A `PreToolUse` hook denies the same tool with the same input after 2 calls.
- The system prompt ends the run when `verify_copy` returns `ok`, or reports the problems and stops.

## Run on try-nomore.com (2026-10-21)
The JSON has 1 option (`Pieces`), 3 variants (1x $89, 2x $119, 4x $159, compare-at $120/$240/$480) and 11 images.
The rendered page shows a Kaching bundle widget with different offers: 1 Device $39.90, 2 Devices $29.95 each, 3 Devices $26.64 each.
`verify_copy` flags this mismatch. The store owner decides which prices the copy should use.
