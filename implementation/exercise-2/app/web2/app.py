"""
Web2 version of the ticketing app — Flask + SQLite + Flask-Login.

Intentionally mirrors the web3 version's UX so the README comparison is
apples-to-apples. The one structural difference is that "admin" is a row in
the users table here, while on-chain it's whichever address deployed the
contract.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, flash, redirect, render_template, request, url_for
from flask_login import (
    LoginManager,
    current_user,
    login_required,
    login_user,
    logout_user,
)

from models import Event, Ticket, User, db

load_dotenv(Path(__file__).resolve().parent / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "web2-dev-secret")
db_path = Path(__file__).resolve().parent / "instance" / "tickets.db"
db_path.parent.mkdir(parents=True, exist_ok=True)
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{db_path}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"


@login_manager.user_loader
def load_user(user_id: str):
    return db.session.get(User, int(user_id))


# Pricing is stored in wei to match the web3 contract; helpers turn that into ETH text.
def _wei_to_eth(wei: int | None) -> float:
    return (wei or 0) / 10**18


def _eth_to_wei(eth: float) -> int:
    return int(float(eth) * 10**18)


@app.context_processor
def _inject_common():
    return {"wei_to_eth": _wei_to_eth}


def seed_if_empty():
    """Create an admin + two events on first run so the app is usable immediately."""
    if User.query.first() is not None:
        return
    admin = User(email="admin@example.com", is_admin=True)
    admin.set_password("admin")
    db.session.add(admin)
    alice = User(email="alice@example.com", is_admin=False)
    alice.set_password("alice")
    db.session.add(alice)
    bob = User(email="bob@example.com", is_admin=False)
    bob.set_password("bob")
    db.session.add(bob)
    db.session.flush()

    db.session.add(Event(name="Concert: Local Night", price_wei=_eth_to_wei(0.05), max_supply=100))
    db.session.add(Event(name="Tech Conference 2026", price_wei=_eth_to_wei(0.08), max_supply=50))
    db.session.commit()


# ---------- Auth ----------

@app.get("/login")
def login_page():
    return render_template("login.html")


@app.post("/login")
def login():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    user = User.query.filter_by(email=email).first()
    if user is None or not user.check_password(password):
        flash("Invalid email or password.", "error")
        return redirect(url_for("login_page"))
    login_user(user)
    flash(f"Welcome, {user.email}.", "success")
    return redirect(url_for("events_list"))


@app.post("/register")
def register():
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        flash("Email and password required.", "error")
        return redirect(url_for("login_page"))
    if User.query.filter_by(email=email).first() is not None:
        flash("Email already registered.", "error")
        return redirect(url_for("login_page"))
    user = User(email=email, is_admin=False)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    login_user(user)
    flash("Account created.", "success")
    return redirect(url_for("events_list"))


@app.post("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# ---------- Events ----------

@app.get("/")
def root():
    return redirect(url_for("events_list"))


@app.get("/events")
def events_list():
    events = Event.query.order_by(Event.id.asc()).all()
    listed = (
        Ticket.query.filter_by(for_sale=True).order_by(Ticket.id.asc()).all()
        if current_user.is_authenticated
        else []
    )
    return render_template("events.html", events=events, listed=listed)


@app.post("/events/<int:event_id>/buy")
@login_required
def buy_ticket(event_id: int):
    event = db.session.get(Event, event_id)
    if event is None:
        flash("Event not found.", "error")
        return redirect(url_for("events_list"))
    if not event.active:
        flash("Event is not active.", "error")
        return redirect(url_for("events_list"))
    if event.remaining <= 0:
        flash("Sold out.", "error")
        return redirect(url_for("events_list"))

    ticket = Ticket(
        event_id=event.id,
        owner_id=current_user.id,
        original_price_wei=event.price_wei,
        listing_price_wei=None,
        for_sale=False,
    )
    event.sold += 1
    db.session.add(ticket)
    db.session.commit()
    flash(f"Ticket #{ticket.id} purchased.", "success")
    return redirect(url_for("my_tickets"))


@app.get("/my-tickets")
@login_required
def my_tickets():
    tickets = (
        Ticket.query.filter_by(owner_id=current_user.id)
        .order_by(Ticket.id.asc())
        .all()
    )
    return render_template("my_tickets.html", tickets=tickets)


# ---------- Transfer / Resale ----------

@app.post("/tickets/<int:ticket_id>/transfer")
@login_required
def transfer(ticket_id: int):
    recipient_email = request.form.get("recipient", "").strip().lower()
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None:
        flash("Ticket not found.", "error")
        return redirect(url_for("my_tickets"))
    if ticket.owner_id != current_user.id:
        flash("You don't own this ticket.", "error")
        return redirect(url_for("my_tickets"))
    if ticket.for_sale:
        flash("Cancel the resale listing first.", "error")
        return redirect(url_for("my_tickets"))
    recipient = User.query.filter_by(email=recipient_email).first()
    if recipient is None:
        flash("Recipient not found.", "error")
        return redirect(url_for("my_tickets"))
    if recipient.id == current_user.id:
        flash("Cannot transfer to yourself.", "error")
        return redirect(url_for("my_tickets"))

    ticket.owner_id = recipient.id
    db.session.commit()
    flash(f"Transferred ticket #{ticket.id} to {recipient.email}.", "success")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/list")
@login_required
def list_for_resale(ticket_id: int):
    try:
        price_eth = float(request.form.get("priceEth", "0"))
    except ValueError:
        flash("Invalid price.", "error")
        return redirect(url_for("my_tickets"))
    if price_eth <= 0:
        flash("Price must be > 0.", "error")
        return redirect(url_for("my_tickets"))

    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or ticket.owner_id != current_user.id:
        flash("Ticket not found or not yours.", "error")
        return redirect(url_for("my_tickets"))
    if ticket.for_sale:
        flash("Already listed.", "error")
        return redirect(url_for("my_tickets"))

    # Match the web3 contract's 2× face-value cap so behaviour aligns.
    cap_wei = ticket.original_price_wei * 2
    price_wei = _eth_to_wei(price_eth)
    if price_wei > cap_wei:
        flash(f"Price above the 2× cap ({_wei_to_eth(cap_wei):.4f} ETH).", "error")
        return redirect(url_for("my_tickets"))

    ticket.listing_price_wei = price_wei
    ticket.for_sale = True
    db.session.commit()
    flash(f"Listed ticket #{ticket.id} at {price_eth} ETH.", "success")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/cancel-list")
@login_required
def cancel_list(ticket_id: int):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or ticket.owner_id != current_user.id:
        flash("Ticket not found or not yours.", "error")
        return redirect(url_for("my_tickets"))
    if not ticket.for_sale:
        flash("Ticket is not listed.", "error")
        return redirect(url_for("my_tickets"))
    ticket.for_sale = False
    ticket.listing_price_wei = None
    db.session.commit()
    flash("Listing cancelled.", "info")
    return redirect(url_for("my_tickets"))


@app.post("/tickets/<int:ticket_id>/buy-resale")
@login_required
def buy_resale(ticket_id: int):
    ticket = db.session.get(Ticket, ticket_id)
    if ticket is None or not ticket.for_sale:
        flash("Ticket not available.", "error")
        return redirect(url_for("events_list"))
    if ticket.owner_id == current_user.id:
        flash("Cannot buy your own listing.", "error")
        return redirect(url_for("events_list"))

    # In web2 we just flip ownership. (Payments could be simulated with a
    # balance table, but the point of the exercise is the comparison with
    # web3's actual ETH flow, not simulating payments here.)
    ticket.owner_id = current_user.id
    ticket.for_sale = False
    ticket.listing_price_wei = None
    db.session.commit()
    flash(f"Bought resale ticket #{ticket.id}.", "success")
    return redirect(url_for("my_tickets"))


# ---------- Admin ----------

@app.get("/admin")
@login_required
def admin_panel():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("events_list"))
    events = Event.query.all()
    return render_template("admin.html", events=events)


@app.post("/admin/events/new")
@login_required
def admin_create_event():
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("events_list"))
    name = request.form.get("name", "").strip()
    try:
        price_eth = float(request.form.get("priceEth", "0"))
        supply = int(request.form.get("supply", "0"))
    except ValueError:
        flash("Invalid numeric input.", "error")
        return redirect(url_for("admin_panel"))
    if not name or price_eth <= 0 or supply <= 0:
        flash("Name, price > 0, supply > 0 required.", "error")
        return redirect(url_for("admin_panel"))
    db.session.add(Event(name=name, price_wei=_eth_to_wei(price_eth), max_supply=supply))
    db.session.commit()
    flash("Event created.", "success")
    return redirect(url_for("admin_panel"))


@app.post("/admin/events/<int:event_id>/toggle")
@login_required
def admin_toggle_event(event_id: int):
    if not current_user.is_admin:
        flash("Admins only.", "error")
        return redirect(url_for("events_list"))
    event = db.session.get(Event, event_id)
    if event is None:
        flash("Event not found.", "error")
        return redirect(url_for("admin_panel"))
    event.active = not event.active
    db.session.commit()
    return redirect(url_for("admin_panel"))


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        seed_if_empty()
    app.run(host="127.0.0.1", port=5002, debug=True)
