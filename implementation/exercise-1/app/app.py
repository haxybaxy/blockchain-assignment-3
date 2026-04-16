"""
Flask UI for the Vending Machine dApp.

Routes:
- /                 product grid + account selector
- /buy/<id>         POST: purchase `quantity` units as the selected account
- /my-items         receipts + aggregated balances for the selected account
- /admin            admin controls (only rendered for the contract owner)
- /admin/add        POST: add product
- /admin/restock    POST: restock a product
- /admin/price      POST: update price
- /admin/active     POST: toggle active flag
- /admin/withdraw   POST: pull ETH out of the contract
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

from web3_client import build_client_from_env

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

client = build_client_from_env()


def _selected_account() -> str:
    """Return the address currently selected in the UI, defaulting to account 0."""
    addr = session.get("account")
    if addr and any(a.address == addr for a in client.accounts):
        return addr
    return client.accounts[0].address


def _account_options() -> list[dict]:
    out = []
    for a in client.accounts:
        eth = client.eth_balance(a.address) / 10**18
        out.append(
            {
                "index": a.index,
                "address": a.address,
                "balanceEth": eth,
                "isOwner": client.is_owner(a.address),
            }
        )
    return out


@app.context_processor
def _inject_common():
    selected = _selected_account()
    return {
        "selected_account": selected,
        "is_owner": client.is_owner(selected),
        "account_options": _account_options(),
        "contract_address": client.address,
        "owner_address": client.owner,
    }


@app.post("/switch-account")
def switch_account():
    addr = request.form.get("account", "").strip()
    if addr and any(a.address == addr for a in client.accounts):
        session["account"] = addr
        flash(f"Switched to {addr[:8]}…", "info")
    return redirect(request.referrer or url_for("index"))


@app.get("/")
def index():
    products = client.list_products()
    # Enrich with a human-readable ETH price for the template.
    for p in products:
        p["priceEth"] = p["priceWei"] / 10**18
    return render_template("index.html", products=products)


@app.post("/buy/<int:product_id>")
def buy(product_id: int):
    try:
        quantity = int(request.form.get("quantity", "1"))
    except ValueError:
        flash("Quantity must be a number.", "error")
        return redirect(url_for("index"))
    if quantity <= 0:
        flash("Quantity must be at least 1.", "error")
        return redirect(url_for("index"))

    # Look up the current price so the UI sends the right amount.
    product = next((p for p in client.list_products() if p["id"] == product_id), None)
    if product is None:
        flash("Product not found.", "error")
        return redirect(url_for("index"))

    total_wei = product["priceWei"] * quantity
    try:
        receipt = client.purchase(_selected_account(), product_id, quantity, total_wei)
    except Exception as e:
        flash(f"Purchase failed: {_reason(e)}", "error")
        return redirect(url_for("index"))

    flash(
        f"Bought {quantity} × {product['name']} — tx {receipt['txHash'][:10]}… "
        f"(block {receipt['blockNumber']}, gas {receipt['gasUsed']})",
        "success",
    )
    return redirect(url_for("my_items"))


@app.get("/my-items")
def my_items():
    addr = _selected_account()
    receipts = client.receipts_of(addr)
    products = {p["id"]: p for p in client.list_products()}
    # Aggregate owned quantity per product for the summary table.
    totals: dict[int, int] = {}
    for r in receipts:
        totals[r["productId"]] = totals.get(r["productId"], 0) + r["quantity"]

    summary = []
    for pid, qty in sorted(totals.items()):
        p = products.get(pid, {"name": f"#{pid}"})
        summary.append({"productId": pid, "name": p.get("name", f"#{pid}"), "quantity": qty})

    # Enrich receipts with readable fields.
    enriched = []
    for r in receipts:
        p = products.get(r["productId"], {"name": f"#{r['productId']}"})
        enriched.append(
            {
                **r,
                "productName": p.get("name"),
                "unitPriceEth": r["unitPrice"] / 10**18,
                "totalEth": (r["unitPrice"] * r["quantity"]) / 10**18,
            }
        )

    return render_template("owned.html", summary=summary, receipts=enriched)


@app.get("/admin")
def admin_panel():
    if not client.is_owner(_selected_account()):
        flash("Admin panel is only available to the contract owner.", "error")
        return redirect(url_for("index"))
    products = client.list_products()
    for p in products:
        p["priceEth"] = p["priceWei"] / 10**18
    contract_balance_eth = client.eth_balance(client.address) / 10**18
    return render_template(
        "admin.html", products=products, contract_balance_eth=contract_balance_eth
    )


@app.post("/admin/add")
def admin_add():
    if not client.is_owner(_selected_account()):
        flash("Only the owner can add products.", "error")
        return redirect(url_for("index"))
    name = request.form.get("name", "").strip()
    try:
        price_eth = float(request.form.get("priceEth", "0"))
        stock = int(request.form.get("stock", "0"))
    except ValueError:
        flash("Invalid numeric input.", "error")
        return redirect(url_for("admin_panel"))
    if not name or price_eth <= 0 or stock < 0:
        flash("Name required, price must be > 0, stock >= 0.", "error")
        return redirect(url_for("admin_panel"))
    price_wei = int(price_eth * 10**18)
    try:
        r = client.add_product(_selected_account(), name, price_wei, stock)
        flash(f"Product added (tx {r['txHash'][:10]}…).", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/restock")
def admin_restock():
    if not client.is_owner(_selected_account()):
        flash("Only the owner can restock.", "error")
        return redirect(url_for("index"))
    try:
        product_id = int(request.form["productId"])
        amount = int(request.form["amount"])
    except (KeyError, ValueError):
        flash("Invalid restock input.", "error")
        return redirect(url_for("admin_panel"))
    try:
        r = client.restock(_selected_account(), product_id, amount)
        flash(f"Restocked (tx {r['txHash'][:10]}…).", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/price")
def admin_price():
    if not client.is_owner(_selected_account()):
        flash("Only the owner can update prices.", "error")
        return redirect(url_for("index"))
    try:
        product_id = int(request.form["productId"])
        new_price_eth = float(request.form["priceEth"])
    except (KeyError, ValueError):
        flash("Invalid price input.", "error")
        return redirect(url_for("admin_panel"))
    if new_price_eth <= 0:
        flash("Price must be > 0.", "error")
        return redirect(url_for("admin_panel"))
    new_price_wei = int(new_price_eth * 10**18)
    try:
        r = client.update_price(_selected_account(), product_id, new_price_wei)
        flash(f"Price updated (tx {r['txHash'][:10]}…).", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/active")
def admin_active():
    if not client.is_owner(_selected_account()):
        flash("Only the owner can toggle products.", "error")
        return redirect(url_for("index"))
    try:
        product_id = int(request.form["productId"])
        active = request.form.get("active") == "true"
    except (KeyError, ValueError):
        flash("Invalid input.", "error")
        return redirect(url_for("admin_panel"))
    try:
        r = client.set_active(_selected_account(), product_id, active)
        flash(f"Status updated (tx {r['txHash'][:10]}…).", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/withdraw")
def admin_withdraw():
    if not client.is_owner(_selected_account()):
        flash("Only the owner can withdraw.", "error")
        return redirect(url_for("index"))
    try:
        r = client.withdraw(_selected_account())
        flash(f"Withdrawn (tx {r['txHash'][:10]}…).", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


def _reason(exc: Exception) -> str:
    """Extract a human-friendly message from a web3 exception."""
    msg = str(exc)
    # web3 exceptions often embed the revert reason; try a few patterns.
    for marker in ("revert ", "execution reverted: ", "custom error "):
        if marker in msg:
            return msg.split(marker, 1)[1]
    return msg


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5001, debug=True)
