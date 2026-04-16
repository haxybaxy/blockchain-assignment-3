"""
Web3 version of the ticketing app — same UX as web2, backend calls the
EventTicketing.sol contract via web3.py instead of hitting SQLite.

Identity is the selected Ganache address (from a dropdown) — there is no
email/password login because the contract doesn't know about emails. This
is the key UX / trust delta with the web2 version.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, session, url_for

from web3_client import build_client_from_env

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "web3-dev-secret")
client = build_client_from_env()


def _selected_account() -> str:
    addr = session.get("account")
    if addr and any(a.address == addr for a in client.accounts):
        return addr
    return client.accounts[0].address


def _account_options() -> list[dict]:
    out = []
    for a in client.accounts:
        out.append(
            {
                "index": a.index,
                "address": a.address,
                "balanceEth": client.eth_balance(a.address) / 10**18,
                "isOwner": client.is_owner(a.address),
            }
        )
    return out


def _wei_to_eth(wei: int | None) -> float:
    return (wei or 0) / 10**18


def _eth_to_wei(eth: float) -> int:
    return int(float(eth) * 10**18)


@app.context_processor
def _inject_common():
    selected = _selected_account()
    return {
        "wei_to_eth": _wei_to_eth,
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
    return redirect(request.referrer or url_for("events_list"))


def _reason(e: Exception) -> str:
    m = str(e)
    for marker in ("revert ", "execution reverted: ", "custom error "):
        if marker in m:
            return m.split(marker, 1)[1]
    return m


# ---------- Events ----------

@app.get("/")
def root():
    return redirect(url_for("events_list"))


@app.get("/events")
def events_list():
    events = client.list_events()
    listed = client.listed_tickets()
    # Enrich listed tickets with event name for the template.
    events_by_id = {e["id"]: e for e in events}
    for t in listed:
        t["eventName"] = events_by_id.get(t["eventId"], {}).get("name", f"Event #{t['eventId']}")
    return render_template("events.html", events=events, listed=listed)


@app.post("/events/<int:event_id>/buy")
def buy_ticket(event_id: int):
    try:
        e = client.get_event(event_id)
    except Exception as exc:
        flash(f"Event not found: {_reason(exc)}", "error")
        return redirect(url_for("events_list"))
    try:
        receipt = client.buy_ticket(_selected_account(), event_id, e["priceWei"])
    except Exception as exc:
        flash(f"Buy failed: {_reason(exc)}", "error")
        return redirect(url_for("events_list"))
    flash(
        f"Bought ticket — tx {receipt['txHash'][:10]}… (block {receipt['blockNumber']}, gas {receipt['gasUsed']})",
        "success",
    )
    return redirect(url_for("my_tickets"))


@app.get("/my-tickets")
def my_tickets():
    addr = _selected_account()
    tickets = client.tickets_of(addr)
    events = {e["id"]: e for e in client.list_events()}
    for t in tickets:
        t["eventName"] = events.get(t["eventId"], {}).get("name", f"Event #{t['eventId']}")
    return render_template("my_tickets.html", tickets=tickets)


@app.post("/tickets/<int:ticket_id>/transfer")
def transfer(ticket_id: int):
    to_addr = request.form.get("recipient", "").strip()
    if not to_addr:
        flash("Recipient address required.", "error")
        return redirect(url_for("my_tickets"))
    try:
        r = client.transfer_ticket(_selected_account(), ticket_id, to_addr)
        flash(f"Transferred — tx {r['txHash'][:10]}…", "success")
    except Exception as e:
        flash(f"Transfer failed: {_reason(e)}", "error")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/list")
def list_for_resale(ticket_id: int):
    try:
        price_eth = float(request.form.get("priceEth", "0"))
    except ValueError:
        flash("Invalid price.", "error")
        return redirect(url_for("my_tickets"))
    if price_eth <= 0:
        flash("Price must be > 0.", "error")
        return redirect(url_for("my_tickets"))
    try:
        r = client.list_for_resale(_selected_account(), ticket_id, _eth_to_wei(price_eth))
        flash(f"Listed — tx {r['txHash'][:10]}…", "success")
    except Exception as e:
        flash(f"List failed: {_reason(e)}", "error")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/cancel-list")
def cancel_list(ticket_id: int):
    try:
        r = client.cancel_resale(_selected_account(), ticket_id)
        flash(f"Cancelled — tx {r['txHash'][:10]}…", "info")
    except Exception as e:
        flash(f"Cancel failed: {_reason(e)}", "error")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/buy-resale")
def buy_resale(ticket_id: int):
    try:
        t = client.get_ticket(ticket_id)
    except Exception as exc:
        flash(f"Ticket not found: {_reason(exc)}", "error")
        return redirect(url_for("events_list"))
    try:
        r = client.buy_resale(_selected_account(), ticket_id, t["listingPriceWei"])
        flash(f"Bought resale — tx {r['txHash'][:10]}…", "success")
    except Exception as e:
        flash(f"Resale purchase failed: {_reason(e)}", "error")
    return redirect(url_for("my_tickets"))


# ---------- Admin ----------

@app.get("/admin")
def admin_panel():
    if not client.is_owner(_selected_account()):
        flash("Admin only. Switch to the owner account in the top-right dropdown.", "error")
        return redirect(url_for("events_list"))
    return render_template(
        "admin.html",
        events=client.list_events(),
        contract_balance_eth=client.eth_balance(client.address) / 10**18,
    )


@app.post("/admin/events/new")
def admin_create_event():
    if not client.is_owner(_selected_account()):
        flash("Admin only.", "error")
        return redirect(url_for("events_list"))
    name = request.form.get("name", "").strip()
    try:
        price_eth = float(request.form.get("priceEth", "0"))
        supply = int(request.form.get("supply", "0"))
    except ValueError:
        flash("Invalid input.", "error")
        return redirect(url_for("admin_panel"))
    if not name or price_eth <= 0 or supply <= 0:
        flash("Name, price > 0, supply > 0.", "error")
        return redirect(url_for("admin_panel"))
    try:
        r = client.create_event(_selected_account(), name, _eth_to_wei(price_eth), supply)
        flash(f"Event created — tx {r['txHash'][:10]}…", "success")
    except Exception as e:
        flash(f"Failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/events/<int:event_id>/toggle")
def admin_toggle_event(event_id: int):
    if not client.is_owner(_selected_account()):
        flash("Admin only.", "error")
        return redirect(url_for("events_list"))
    try:
        e = client.get_event(event_id)
        r = client.set_event_active(_selected_account(), event_id, not e["active"])
        flash(f"Toggled — tx {r['txHash'][:10]}…", "success")
    except Exception as exc:
        flash(f"Failed: {_reason(exc)}", "error")
    return redirect(url_for("admin_panel"))


@app.post("/admin/withdraw")
def admin_withdraw():
    if not client.is_owner(_selected_account()):
        flash("Admin only.", "error")
        return redirect(url_for("events_list"))
    try:
        r = client.withdraw(_selected_account())
        flash(f"Withdrawn — tx {r['txHash'][:10]}…", "success")
    except Exception as e:
        flash(f"Withdraw failed: {_reason(e)}", "error")
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5003, debug=True)
