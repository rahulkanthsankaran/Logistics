"""
Parcel Trail - Courier Tracking System
Flask + SQLite application with a staff console, public tracking page and JSON API.
"""
import hmac
import os
import re
import secrets
import sqlite3
from datetime import datetime, timedelta
from functools import wraps

from flask import (Flask, abort, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)

# --------------------------------------------------------------------------
# Domain constants
# --------------------------------------------------------------------------
STATUSES = ["Booked", "Picked Up", "In Transit", "At Hub",
            "Out for Delivery", "Delivered", "Exception"]

# Steps shown on the progress bar of the tracking page
PROGRESS_STEPS = ["Booked", "Picked Up", "In Transit", "Out for Delivery", "Delivered"]
STATUS_TO_STEP = {"Booked": 0, "Picked Up": 1, "In Transit": 2, "At Hub": 2,
                  "Out for Delivery": 3, "Delivered": 4}

ACTIVE_STATUSES = ("Picked Up", "In Transit", "At Hub", "Out for Delivery")

# service -> base fee (Rs), fee per kg (Rs), delivery promise (days)
SERVICES = {
    "Standard": {"base": 40, "per_kg": 20, "days": 5, "blurb": "Road network, 4 to 5 days"},
    "Express": {"base": 80, "per_kg": 35, "days": 2, "blurb": "Priority hubs, 2 days"},
    "Overnight": {"base": 150, "per_kg": 55, "days": 1, "blurb": "Air and road, next day"},
}

PHONE_RE = re.compile(r"^[6-9]\d{9}$")
TRACKING_RE = re.compile(r"^PT\d{10}$")


# --------------------------------------------------------------------------
# Pure helpers (unit tested directly)
# --------------------------------------------------------------------------
def calculate_price(service, weight_kg):
    """Return the shipping price in rupees, rounded to 2 decimals."""
    rate = SERVICES[service]
    return round(rate["base"] + rate["per_kg"] * weight_kg, 2)


def estimate_delivery(service, booked_on=None):
    booked_on = booked_on or datetime.now()
    return (booked_on + timedelta(days=SERVICES[service]["days"])).strftime("%Y-%m-%d")


def mask_phone(phone):
    return "XXXXXX" + phone[-4:] if phone and len(phone) >= 4 else ""


def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M")


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS shipments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tracking_id TEXT NOT NULL UNIQUE,
    sender_name TEXT NOT NULL,
    sender_phone TEXT NOT NULL,
    receiver_name TEXT NOT NULL,
    receiver_phone TEXT NOT NULL,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    weight_kg REAL NOT NULL,
    service TEXT NOT NULL,
    contents TEXT NOT NULL DEFAULT '',
    price REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'Booked',
    eta TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shipment_id INTEGER NOT NULL REFERENCES shipments(id),
    status TEXT NOT NULL,
    location TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_shipment ON events(shipment_id);
"""


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app_config("DATABASE"))
        g.db.row_factory = sqlite3.Row
    return g.db


def current_app_config(key):
    from flask import current_app
    return current_app.config[key]


def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


def new_tracking_id(db):
    while True:
        tid = "PT" + "".join(secrets.choice("0123456789") for _ in range(10))
        if not db.execute("SELECT 1 FROM shipments WHERE tracking_id=?", (tid,)).fetchone():
            return tid


def fetch_shipment(db, tracking_id):
    return db.execute("SELECT * FROM shipments WHERE tracking_id=?", (tracking_id,)).fetchone()


def fetch_events(db, shipment_id):
    return db.execute(
        "SELECT * FROM events WHERE shipment_id=? ORDER BY id DESC", (shipment_id,)).fetchall()


def progress_step(status, events):
    """Index on PROGRESS_STEPS; an Exception keeps the last healthy step."""
    if status in STATUS_TO_STEP:
        return STATUS_TO_STEP[status]
    for ev in events:  # newest first
        if ev["status"] in STATUS_TO_STEP:
            return STATUS_TO_STEP[ev["status"]]
    return 0


# --------------------------------------------------------------------------
# Application factory
# --------------------------------------------------------------------------
def create_app(test_config=None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-only-change-me"),
        DATABASE=os.environ.get(
            "DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "courier.db")),
        STAFF_USERNAME=os.environ.get("STAFF_USERNAME", "staff"),
        STAFF_PASSWORD=os.environ.get("STAFF_PASSWORD", "courier123"),
    )
    if test_config:
        app.config.update(test_config)
    init_db(app.config["DATABASE"])

    @app.teardown_appcontext
    def close_db(_exc):
        db = g.pop("db", None)
        if db is not None:
            db.close()

    # ---- CSRF protection for every POST --------------------------------
    @app.before_request
    def csrf_protect():
        if request.method == "POST":
            expected = session.get("_csrf", "")
            sent = request.form.get("_csrf", "")
            if not expected or not hmac.compare_digest(expected, sent):
                abort(400, "Form expired. Reload the page and try again.")

    @app.context_processor
    def inject_globals():
        if "_csrf" not in session:
            session["_csrf"] = secrets.token_hex(16)
        return {"csrf_token": session["_csrf"], "STATUSES": STATUSES,
                "SERVICES": SERVICES, "is_staff": bool(session.get("staff"))}

    @app.template_filter("slug")
    def slug(value):
        return str(value).lower().replace(" ", "-")

    @app.template_filter("nice_date")
    def nice_date(value):
        if not value:
            return ""
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                d = datetime.strptime(value, fmt)
                return d.strftime("%d %b %Y, %H:%M") if "%H" in fmt else d.strftime("%d %b %Y")
            except ValueError:
                continue
        return value

    def staff_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if not session.get("staff"):
                return redirect(url_for("staff_login", next=request.path))
            return view(*args, **kwargs)
        return wrapper

    # ---- Public pages ---------------------------------------------------
    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/health")
    def health():
        try:
            get_db().execute("SELECT 1").fetchone()
            return jsonify(status="ok")
        except sqlite3.Error:
            return jsonify(status="database error"), 503

    @app.route("/book", methods=["GET", "POST"])
    def book():
        form = {}
        if request.method == "POST":
            form = {k: request.form.get(k, "").strip() for k in (
                "sender_name", "sender_phone", "receiver_name", "receiver_phone",
                "origin", "destination", "weight_kg", "service", "contents")}
            errors = validate_booking(form)
            if errors:
                for e in errors:
                    flash(e, "error")
                return render_template("book.html", form=form), 400

            db = get_db()
            weight = round(float(form["weight_kg"]), 2)
            tid = new_tracking_id(db)
            created = now_str()
            cur = db.execute(
                """INSERT INTO shipments (tracking_id, sender_name, sender_phone, receiver_name,
                   receiver_phone, origin, destination, weight_kg, service, contents, price,
                   status, eta, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tid, form["sender_name"], form["sender_phone"], form["receiver_name"],
                 form["receiver_phone"], form["origin"].title(), form["destination"].title(),
                 weight, form["service"], form["contents"], calculate_price(form["service"], weight),
                 "Booked", estimate_delivery(form["service"]), created))
            db.execute(
                "INSERT INTO events (shipment_id, status, location, note, created_at) VALUES (?,?,?,?,?)",
                (cur.lastrowid, "Booked", form["origin"].title(),
                 "Booking confirmed. Waiting for pickup.", created))
            db.commit()
            return redirect(url_for("receipt", tracking_id=tid))
        return render_template("book.html", form=form)

    @app.route("/receipt/<tracking_id>")
    def receipt(tracking_id):
        shipment = fetch_shipment(get_db(), tracking_id.upper())
        if not shipment:
            abort(404)
        return render_template("receipt.html", s=shipment)

    @app.route("/track")
    def track():
        raw = request.args.get("id", "").strip().upper()
        shipment, events, step, error = None, [], 0, None
        if raw:
            if not TRACKING_RE.match(raw):
                error = "Tracking numbers start with PT followed by 10 digits."
            else:
                db = get_db()
                shipment = fetch_shipment(db, raw)
                if shipment:
                    events = fetch_events(db, shipment["id"])
                    step = progress_step(shipment["status"], events)
                else:
                    error = "No parcel found with that number. Check it and try again."
        return render_template("track.html", q=raw, s=shipment, events=events, step=step,
                               steps=PROGRESS_STEPS, error=error, mask_phone=mask_phone)

    @app.route("/api/track/<tracking_id>")
    def api_track(tracking_id):
        db = get_db()
        shipment = fetch_shipment(db, tracking_id.upper())
        if not shipment:
            return jsonify(error="not_found"), 404
        events = fetch_events(db, shipment["id"])
        return jsonify(
            tracking_id=shipment["tracking_id"], status=shipment["status"],
            origin=shipment["origin"], destination=shipment["destination"],
            service=shipment["service"], eta=shipment["eta"],
            events=[{"status": e["status"], "location": e["location"],
                     "note": e["note"], "time": e["created_at"]} for e in events])

    # ---- Staff console ---------------------------------------------------
    @app.route("/staff/login", methods=["GET", "POST"])
    def staff_login():
        if request.method == "POST":
            user = request.form.get("username", "")
            pw = request.form.get("password", "")
            ok_user = hmac.compare_digest(user.encode(), app.config["STAFF_USERNAME"].encode())
            ok_pass = hmac.compare_digest(pw.encode(), app.config["STAFF_PASSWORD"].encode())
            if ok_user and ok_pass:
                session.clear()
                session["staff"] = True
                nxt = request.args.get("next", "")
                if not nxt.startswith("/") or nxt.startswith("//"):
                    nxt = url_for("console")
                return redirect(nxt)
            flash("Wrong username or password.", "error")
            return render_template("login.html"), 401
        return render_template("login.html")

    @app.route("/staff/logout", methods=["POST"])
    def staff_logout():
        session.clear()
        flash("You are signed out.", "ok")
        return redirect(url_for("index"))

    @app.route("/staff")
    @staff_required
    def console():
        db = get_db()
        q = request.args.get("q", "").strip()
        status = request.args.get("status", "")
        sql, params = "SELECT * FROM shipments WHERE 1=1", []
        if q:
            sql += " AND (tracking_id LIKE ? OR sender_name LIKE ? OR receiver_name LIKE ?)"
            params += [f"%{q}%"] * 3
        if status in STATUSES:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC LIMIT 100"
        rows = db.execute(sql, params).fetchall()

        stats = {
            "total": db.execute("SELECT COUNT(*) FROM shipments").fetchone()[0],
            "booked": db.execute("SELECT COUNT(*) FROM shipments WHERE status='Booked'").fetchone()[0],
            "active": db.execute(
                f"SELECT COUNT(*) FROM shipments WHERE status IN ({','.join('?' * len(ACTIVE_STATUSES))})",
                ACTIVE_STATUSES).fetchone()[0],
            "delivered": db.execute("SELECT COUNT(*) FROM shipments WHERE status='Delivered'").fetchone()[0],
            "exceptions": db.execute("SELECT COUNT(*) FROM shipments WHERE status='Exception'").fetchone()[0],
            "revenue": db.execute("SELECT COALESCE(SUM(price),0) FROM shipments").fetchone()[0],
        }
        return render_template("console.html", rows=rows, stats=stats, q=q, status=status)

    @app.route("/staff/shipment/<tracking_id>")
    @staff_required
    def shipment_detail(tracking_id):
        db = get_db()
        shipment = fetch_shipment(db, tracking_id.upper())
        if not shipment:
            abort(404)
        return render_template("shipment.html", s=shipment, events=fetch_events(db, shipment["id"]))

    @app.route("/staff/shipment/<tracking_id>/update", methods=["POST"])
    @staff_required
    def update_shipment(tracking_id):
        db = get_db()
        shipment = fetch_shipment(db, tracking_id.upper())
        if not shipment:
            abort(404)
        back = redirect(url_for("shipment_detail", tracking_id=shipment["tracking_id"]))

        status = request.form.get("status", "")
        location = request.form.get("location", "").strip()
        note = request.form.get("note", "").strip()

        if shipment["status"] == "Delivered":
            flash("This parcel is already delivered and can no longer be changed.", "error")
            return back
        if status not in STATUSES:
            flash("Choose a valid status.", "error")
            return back
        if not location or len(location) > 80:
            flash("Enter the current location (up to 80 characters).", "error")
            return back
        if len(note) > 200:
            flash("Keep the note under 200 characters.", "error")
            return back

        stamp = now_str()
        db.execute("INSERT INTO events (shipment_id, status, location, note, created_at) VALUES (?,?,?,?,?)",
                   (shipment["id"], status, location.title(), note, stamp))
        db.execute("UPDATE shipments SET status=?, delivered_at=? WHERE id=?",
                   (status, stamp if status == "Delivered" else None, shipment["id"]))
        db.commit()
        flash(f"Status updated to {status}.", "ok")
        return back

    @app.errorhandler(404)
    def not_found(_e):
        return render_template("error.html", code=404,
                               message="We could not find that page."), 404

    @app.errorhandler(400)
    def bad_request(e):
        return render_template("error.html", code=400,
                               message=getattr(e, "description", "Bad request.")), 400

    return app


def validate_booking(f):
    errors = []
    for key, label in (("sender_name", "Sender name"), ("receiver_name", "Receiver name"),
                       ("origin", "Pickup city"), ("destination", "Delivery city")):
        if not f[key] or len(f[key]) > 60:
            errors.append(f"{label} is required (up to 60 characters).")
    for key, label in (("sender_phone", "Sender phone"), ("receiver_phone", "Receiver phone")):
        if not PHONE_RE.match(f[key]):
            errors.append(f"{label} must be a 10-digit Indian mobile number.")
    if f["origin"] and f["destination"] and f["origin"].lower() == f["destination"].lower():
        errors.append("Pickup and delivery cities must be different.")
    if f["service"] not in SERVICES:
        errors.append("Choose a delivery service.")
    try:
        w = float(f["weight_kg"])
        if not 0.1 <= w <= 50:
            errors.append("Weight must be between 0.1 and 50 kg.")
    except ValueError:
        errors.append("Weight must be a number.")
    if len(f["contents"]) > 120:
        errors.append("Describe the contents in 120 characters or fewer.")
    return errors


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
