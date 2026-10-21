"""Copy a product page into a Shopify store with the Claude Agent SDK.

Usage:
  python copy_product.py URL              # deterministic run, no LLM, no API cost
  python copy_product.py URL --agent      # same tools, driven by a Claude agent

Without SHOPIFY_STORE and SHOPIFY_TOKEN set, create_product is a dry run:
it writes the exact productSet payload to out/ instead of calling Shopify.
"""

import asyncio
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import requests

OUT = Path(__file__).parent / "out"
UA = {"User-Agent": "Mozilla/5.0"}
API_VERSION = "2025-01"
MAX_TURNS = 12
MAX_SAME_CALL = 2


# ---------- plain functions (the tools call these) ----------

def fetch_product(url: str) -> dict:
    """Shopify stores expose /products/<handle>.json. Normalize it."""
    p = urlparse(url)
    handle = p.path.rstrip("/").split("/products/")[-1]
    r = requests.get(f"{p.scheme}://{p.netloc}/products/{handle}.json", headers=UA, timeout=20)
    r.raise_for_status()
    src = r.json()["product"]
    return {
        "title": src["title"],
        "body_html": src.get("body_html") or "",
        "vendor": src.get("vendor") or "",
        "options": [{"name": o["name"], "values": o["values"]} for o in src["options"]],
        "variants": [
            {
                "options": [v[f"option{i}"] for i in (1, 2, 3) if v.get(f"option{i}")],
                "price": v["price"],
                "compare_at_price": v.get("compare_at_price"),
                "sku": v.get("sku") or "",
            }
            for v in src["variants"]
        ],
        # sort by position so the copy keeps the same picture order
        "images": [i["src"] for i in sorted(src["images"], key=lambda i: i["position"])],
    }


def render_page(url: str) -> str:
    """Visible page text after JavaScript runs (fallback source and price check)."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        b = pw.chromium.launch(executable_path=os.getenv("CHROMIUM_PATH"))
        page = b.new_page()
        # networkidle never fires on stores with trackers, so wait for load plus widgets
        page.goto(url, wait_until="load", timeout=60000)
        page.wait_for_timeout(6000)
        text = page.inner_text("body")
        b.close()
    return text


def build_payload(prod: dict) -> dict:
    names = [o["name"] for o in prod["options"]]
    return {
        "input": {
            "title": prod["title"],
            "descriptionHtml": prod["body_html"],
            "vendor": prod["vendor"],
            "productOptions": [
                {"name": o["name"], "values": [{"name": v} for v in o["values"]]}
                for o in prod["options"]
            ],
            "variants": [
                {
                    "optionValues": [{"optionName": n, "name": val} for n, val in zip(names, v["options"])],
                    "price": v["price"],
                    "compareAtPrice": v["compare_at_price"],
                    "sku": v["sku"],
                }
                for v in prod["variants"]
            ],
            # files keep list order, so images land in the source order
            "files": [{"originalSource": src, "contentType": "IMAGE"} for src in prod["images"]],
        }
    }


PRODUCT_SET = """
mutation($input: ProductSetInput!) {
  productSet(input: $input, synchronous: true) {
    product { id title variants(first: 100) { nodes { price } } media(first: 100) { nodes { id } } }
    userErrors { field message }
  }
}"""


def create_product(prod: dict) -> dict:
    payload = build_payload(prod)
    store, token = os.getenv("SHOPIFY_STORE"), os.getenv("SHOPIFY_TOKEN")
    if not (store and token):
        OUT.mkdir(exist_ok=True)
        (OUT / "productSet_payload.json").write_text(json.dumps(payload, indent=2))
        return {"dry_run": True, "variants": len(payload["input"]["variants"]),
                "images": len(payload["input"]["files"]), "file": "out/productSet_payload.json"}
    r = requests.post(
        f"https://{store}/admin/api/{API_VERSION}/graphql.json",
        headers={"X-Shopify-Access-Token": token},
        json={"query": PRODUCT_SET, "variables": payload},
        timeout=60,
    )
    r.raise_for_status()
    res = r.json()["data"]["productSet"]
    if res["userErrors"]:
        raise RuntimeError(res["userErrors"])
    p = res["product"]
    return {"dry_run": False, "id": p["id"],
            "variants": len(p["variants"]["nodes"]), "images": len(p["media"]["nodes"])}


def verify_copy(url: str, prod: dict, created: dict) -> dict:
    """Catch the two likely breaks: wrong prices and missing or reordered images."""
    problems = []
    if created["variants"] != len(prod["variants"]):
        problems.append(f"variants {created['variants']} != {len(prod['variants'])}")
    if created["images"] != len(prod["images"]):
        problems.append(f"images {created['images']} != {len(prod['images'])}")
    if created.get("dry_run"):
        sent = json.loads((OUT / "productSet_payload.json").read_text())["input"]["files"]
        if [f["originalSource"] for f in sent] != prod["images"]:
            problems.append("image order changed")
    # bundle widgets (Kaching here) draw their own prices with JavaScript,
    # so check the rendered page, not the raw HTML
    shown = set(re.findall(r"\$\s?(\d+(?:\.\d{2})?)", render_page(url)))
    for v in prod["variants"]:
        if v["price"] not in shown and v["price"].removesuffix(".00") not in shown:
            problems.append(f"variant price {v['price']} not shown to shoppers, the page shows {sorted(shown)}")
    return {"ok": not problems, "problems": problems}


def run_plain(url: str) -> None:
    prod = fetch_product(url)
    created = create_product(prod)
    report = verify_copy(url, prod, created)
    OUT.mkdir(exist_ok=True)
    (OUT / "source_product.json").write_text(json.dumps(prod, indent=2))
    print(json.dumps({"title": prod["title"], "variants": prod["variants"],
                      "images": len(prod["images"]), "created": created, "verify": report}, indent=2))


# ---------- the agent ----------

async def run_agent(url: str) -> None:
    from claude_agent_sdk import (ClaudeAgentOptions, ClaudeSDKClient, HookMatcher,
                                  create_sdk_mcp_server, tool)

    state: dict = {}

    def text(obj) -> dict:
        return {"content": [{"type": "text", "text": json.dumps(obj)[:20000]}]}

    @tool("get_product_json", "Read a Shopify product as clean JSON", {"url": str})
    async def t_get(args):
        state["prod"] = fetch_product(args["url"])
        return text(state["prod"])

    @tool("render_page", "Visible text of a page after JavaScript runs", {"url": str})
    async def t_render(args):
        return text(render_page(args["url"]))

    @tool("create_product", "Create the product from the last get_product_json result", {})
    async def t_create(args):
        state["created"] = create_product(state["prod"])
        return text(state["created"])

    @tool("verify_copy", "Compare the created product with the source page", {"url": str})
    async def t_verify(args):
        state["verify"] = verify_copy(args["url"], state["prod"], state["created"])
        return text(state["verify"])

    server = create_sdk_mcp_server("shop", tools=[t_get, t_render, t_create, t_verify])

    # loop guard: the same tool with the same input is denied after MAX_SAME_CALL tries
    seen: Counter = Counter()

    async def loop_guard(inp, tool_use_id, ctx):
        key = inp["tool_name"] + json.dumps(inp["tool_input"], sort_keys=True)
        seen[key] += 1
        if seen[key] > MAX_SAME_CALL:
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Repeated call. Stop and report the problem to a human.",
            }}
        return {}

    options = ClaudeAgentOptions(
        system_prompt=(
            "You copy one product into Shopify. Call get_product_json, then create_product, "
            "then verify_copy. Use render_page only if get_product_json fails. "
            "Stop when verify_copy returns ok. If it is not ok, report the problems and stop."
        ),
        mcp_servers={"shop": server},
        allowed_tools=[f"mcp__shop__{n}" for n in ("get_product_json", "render_page", "create_product", "verify_copy")],
        tools=[],  # no built-in tools, only ours
        max_turns=MAX_TURNS,
        max_budget_usd=0.50,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[loop_guard])]},
    )
    async with ClaudeSDKClient(options=options) as client:
        await client.query(f"Copy this product: {url}")
        async for msg in client.receive_response():
            print(msg)
    print(json.dumps(state.get("verify"), indent=2))


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    if "--agent" in sys.argv:
        asyncio.run(run_agent(sys.argv[1]))
    else:
        run_plain(sys.argv[1])
