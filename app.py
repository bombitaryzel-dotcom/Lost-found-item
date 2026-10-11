from __future__ import annotations

import argparse
import csv
import io
import os
import secrets
import sqlite3
import json
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

import qrcode
from PIL import Image, UnidentifiedImageError
from flask import (
    Flask, abort, current_app, flash, g, make_response, redirect, render_template, request,
    send_file, session, url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
ITEM_STATUSES = {
    "pending_review", "published", "matched", "claim_pending", "returned",
    "archived", "rejected",
}
CLAIM_STATUSES = {"pending", "approved", "rejected", "released"}


def persistent_secret(instance_path):
    configured = os.getenv("SECRET_KEY")
    if configured:
        return configured
    path = Path(instance_path) / ".secret_key"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_hex(32)
    path.write_text(value, encoding="utf-8")
    return value


def create_app(test_config=None):
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY=persistent_secret(app.instance_path),
        DATABASE=str(Path(app.instance_path) / "lost_found.sqlite3"),
        UPLOAD_FOLDER=str(Path(app.instance_path) / "uploads"),
        MAX_CONTENT_LENGTH=5 * 1024 * 1024,
        SCHOOL_NAME="St. Francis Xavier College",
        SCHOOL_LOCATION="San Francisco, Agusan del Sur",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)

    # Keep an in-memory SQLite database alive across Flask connections. This is
    # useful for tests and local diagnostics; normal deployments use a file.
    if app.config["DATABASE"] == ":memory:":
        app.config["DATABASE"] = "file:sfxc_lost_found_shared?mode=memory&cache=shared"
        app.config["DATABASE_URI"] = True
        app.extensions["memory_db_anchor"] = sqlite3.connect(app.config["DATABASE"], uri=True)

    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)

    app.teardown_appcontext(close_db)
    app.jinja_env.filters["pretty_status"] = lambda value: value.replace("_", " ").title()
    app.jinja_env.globals["current_year"] = datetime.now().year

    @app.context_processor
    def inject_globals():
        return {
            "school_name": app.config["SCHOOL_NAME"],
            "school_location": app.config["SCHOOL_LOCATION"],
            "csrf_token": get_csrf_token,
        }

    @app.before_request
    def protect_forms():
        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            expected = session.get("csrf_token")
            supplied = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token")
            if not expected or not supplied or not secrets.compare_digest(expected, supplied):
                abort(400, "Invalid or missing security token.")

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; "
            "script-src 'self' https://unpkg.com; font-src 'self'; frame-ancestors 'self'"
        )
        return response

    @app.route("/")
    def index():
        db = get_db()
        stats = db.execute(
            """SELECT
                SUM(CASE WHEN report_type='lost' AND status IN ('published','matched','claim_pending') THEN 1 ELSE 0 END) lost_count,
                SUM(CASE WHEN report_type='found' AND status IN ('published','matched','claim_pending') THEN 1 ELSE 0 END) found_count,
                SUM(CASE WHEN status='returned' THEN 1 ELSE 0 END) returned_count
               FROM items"""
        ).fetchone()
        recent = db.execute(
            """SELECT i.*, c.name category_name, l.name location_name
               FROM items i LEFT JOIN categories c ON c.id=i.category_id
               LEFT JOIN locations l ON l.id=i.location_id
               WHERE i.status IN ('published','matched','claim_pending')
               ORDER BY i.approved_at DESC LIMIT 6"""
        ).fetchall()
        return render_template("index.html", stats=stats, recent=recent)

    @app.route("/items")
    def public_items():
        db = get_db()
        q = request.args.get("q", "").strip()
        kind = request.args.get("type", "").strip()
        category = request.args.get("category", "").strip()
        sql = """SELECT i.*, c.name category_name, l.name location_name
                 FROM items i LEFT JOIN categories c ON c.id=i.category_id
                 LEFT JOIN locations l ON l.id=i.location_id
                 WHERE i.status IN ('published','matched','claim_pending')"""
        args = []
        if q:
            sql += " AND (i.title LIKE ? OR i.public_description LIKE ? OR i.public_reference LIKE ?)"
            args.extend([f"%{q}%"] * 3)
        if kind in {"lost", "found"}:
            sql += " AND i.report_type=?"
            args.append(kind)
        if category.isdigit():
            sql += " AND i.category_id=?"
            args.append(category)
        sql += " ORDER BY i.approved_at DESC"
        items = db.execute(sql, args).fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        return render_template("items.html", items=items, categories=categories)

    @app.route("/returned-items")
    def returned_items():
        items = get_db().execute("SELECT * FROM items WHERE status='returned' ORDER BY updated_at DESC LIMIT 24").fetchall()
        return render_template("returned_items.html", items=items)

    @app.route("/items/<reference>")
    def item_detail(reference):
        item = get_db().execute(
            """SELECT i.*, c.name category_name, l.name location_name
               FROM items i LEFT JOIN categories c ON c.id=i.category_id
               LEFT JOIN locations l ON l.id=i.location_id
               WHERE i.public_reference=? AND i.status IN ('published','matched','claim_pending','returned')""",
            (reference,),
        ).fetchone()
        if not item:
            abort(404)
        return render_template("item_detail.html", item=item)

    @app.route("/track/<token>")
    def qr_track(token):
        item = get_db().execute(
            "SELECT public_reference FROM items WHERE qr_token=?", (token,)
        ).fetchone()
        if not item:
            abort(404)
        return redirect(url_for("item_detail", reference=item["public_reference"]))

    @app.route("/report", methods=["GET", "POST"])
    def report_item():
        db = get_db()
        if request.method == "POST":
            report_type = request.form.get("report_type", "")
            title = request.form.get("title", "").strip()
            public_description = request.form.get("public_description", "").strip()
            private_details = request.form.get("private_details", "").strip()
            reporter_name = request.form.get("reporter_name", "").strip()
            reporter_contact = request.form.get("reporter_contact", "").strip()
            event_date = request.form.get("event_date", "").strip()
            category_id = request.form.get("category_id", "")
            location_id = request.form.get("location_id", "")
            errors = []
            if report_type not in {"lost", "found"}: errors.append("Choose Lost or Found.")
            if len(title) < 3: errors.append("Item name must contain at least 3 characters.")
            if len(public_description) < 10: errors.append("Public description must contain at least 10 characters.")
            if len(private_details) < 5: errors.append("Add a private detail that can help verify ownership.")
            if len(reporter_name) < 2: errors.append("Enter the reporter's name.")
            if len(reporter_contact) < 5: errors.append("Enter valid contact information.")
            if not event_date: errors.append("Choose the date lost or found.")
            if event_date:
                try:
                    parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                    if parsed_date > datetime.now(timezone.utc).date():
                        errors.append("The lost/found date cannot be in the future.")
                except ValueError:
                    errors.append("Enter a valid lost/found date.")
            if category_id and (not category_id.isdigit() or not db.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone()):
                errors.append("Choose a valid category.")
            if location_id and (not location_id.isdigit() or not db.execute("SELECT 1 FROM locations WHERE id=?", (location_id,)).fetchone()):
                errors.append("Choose a valid location.")
            image_name = None
            if not errors:
                image_name, image_error = save_image(request.files.get("image"))
                if image_error:
                    errors.append(image_error)
            if errors:
                for error in errors: flash(error, "error")
            else:
                reference = unique_reference(db)
                token = secrets.token_urlsafe(24)
                db.execute(
                    """INSERT INTO items
                    (public_reference, qr_token, report_type, title, public_description,
                     private_details, category_id, location_id, event_date, reporter_name,
                     reporter_contact, image_name, status, created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'pending_review', ?)""",
                    (reference, token, report_type, title, public_description, private_details,
                     category_id or None, location_id or None, event_date, reporter_name,
                     reporter_contact, image_name, now()),
                )
                db.commit()
                flash(f"Report submitted successfully. Save your reference: {reference}", "success")
                return redirect(url_for("submission_status", reference=reference))
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        locations = db.execute("SELECT * FROM locations ORDER BY name").fetchall()
        return render_template("report_item.html", categories=categories, locations=locations)

    @app.route("/submission/<reference>")
    def submission_status(reference):
        item = get_db().execute(
            "SELECT public_reference, title, report_type, status, created_at FROM items WHERE public_reference=?",
            (reference,),
        ).fetchone()
        if not item: abort(404)
        return render_template("submission_status.html", item=item)

    @app.route("/check-status", methods=["GET", "POST"])
    def check_status():
        if request.method == "POST":
            reference = request.form.get("reference", "").strip().upper()
            db = get_db()
            if reference.startswith("LF-"):
                if db.execute("SELECT 1 FROM items WHERE public_reference=?", (reference,)).fetchone():
                    return redirect(url_for("submission_status", reference=reference))
            elif reference.startswith("CLM-"):
                if db.execute("SELECT 1 FROM claims WHERE claim_reference=?", (reference,)).fetchone():
                    return redirect(url_for("claim_status", reference=reference))
            flash("Reference not found. Check the letters and numbers, then try again.", "error")
        return render_template("check_status.html")

    @app.route("/items/<reference>/claim", methods=["GET", "POST"])
    def submit_claim(reference):
        db = get_db()
        item = db.execute(
            "SELECT * FROM items WHERE public_reference=? AND status IN ('published','matched','claim_pending')",
            (reference,),
        ).fetchone()
        if not item: abort(404)
        if request.method == "POST":
            claimant_name = request.form.get("claimant_name", "").strip()
            contact = request.form.get("contact", "").strip()
            identification = request.form.get("identification", "").strip()
            proof = request.form.get("proof_description", "").strip()
            if min(len(claimant_name), len(contact), len(identification), len(proof)) < 3:
                flash("Complete all claim fields with enough information for verification.", "error")
            else:
                claim_ref = unique_claim_reference(db)
                db.execute(
                    """INSERT INTO claims
                       (claim_reference,item_id,claimant_name,contact,identification,proof_description,status,created_at)
                       VALUES (?,?,?,?,?,?,'pending',?)""",
                    (claim_ref, item["id"], claimant_name, contact, identification, proof, now()),
                )
                db.execute("UPDATE items SET status='claim_pending', updated_at=? WHERE id=?", (now(), item["id"]))
                db.commit()
                flash(f"Claim submitted. Save your claim reference: {claim_ref}", "success")
                return redirect(url_for("claim_status", reference=claim_ref))
        return render_template("claim_form.html", item=item)

    @app.route("/claim-status/<reference>")
    def claim_status(reference):
        claim = get_db().execute(
            """SELECT cl.claim_reference, cl.status, cl.created_at, i.title, i.public_reference
               FROM claims cl JOIN items i ON i.id=cl.item_id WHERE cl.claim_reference=?""",
            (reference,),
        ).fetchone()
        if not claim: abort(404)
        return render_template("claim_status.html", claim=claim)

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            user = get_db().execute(
                "SELECT * FROM users WHERE username=? AND active=1",
                (request.form.get("username", "").strip().lower(),),
            ).fetchone()
            if not user or not check_password_hash(user["password_hash"], request.form.get("password", "")):
                flash("Invalid username or password.", "error")
                return render_template("login.html"), 401
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["must_change_password"] = bool(user["must_change_password"])
            session["csrf_token"] = secrets.token_urlsafe(32)
            db = get_db()
            db.execute("UPDATE users SET last_login=? WHERE id=?", (now(), user["id"]))
            db.commit()
            audit("login", "user", user["id"], "Account signed in")
            if user["must_change_password"]:
                flash("Change the temporary password before continuing.", "warning")
                return redirect(url_for("change_password"))
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    @login_required
    def logout():
        audit("logout", "user", session["user_id"], "Account signed out")
        session.clear()
        flash("You have been signed out.", "success")
        return redirect(url_for("index"))

    @app.route("/change-password", methods=["GET", "POST"])
    @login_required
    def change_password():
        if request.method == "POST":
            password = request.form.get("password", "")
            confirm = request.form.get("confirm_password", "")
            if password != confirm or len(password) < 10 or password.isalnum():
                flash("Use at least 10 characters including a symbol, and make both entries match.", "error")
            else:
                db = get_db()
                db.execute(
                    "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                    (generate_password_hash(password), session["user_id"]),
                )
                db.commit()
                session["must_change_password"] = False
                audit("password_changed", "user", session["user_id"], "Password updated")
                flash("Password updated.", "success")
                return redirect(url_for("dashboard"))
        return render_template("change_password.html")

    @app.route("/dashboard")
    @staff_required
    def dashboard():
        db = get_db()
        counts = {
            row["status"]: row["total"]
            for row in db.execute("SELECT status, COUNT(*) total FROM items GROUP BY status")
        }
        pending_claims = db.execute("SELECT COUNT(*) n FROM claims WHERE status='pending'").fetchone()["n"]
        monthly = db.execute("SELECT substr(created_at,1,7) month, COUNT(*) total FROM items GROUP BY month ORDER BY month DESC LIMIT 6").fetchall()
        recent = db.execute(
            "SELECT * FROM items ORDER BY created_at DESC LIMIT 8"
        ).fetchall()
        return render_template("dashboard.html", counts=counts, pending_claims=pending_claims, recent=recent, monthly=monthly)

    @app.route("/manage/items")
    @staff_required
    def manage_items():
        status = request.args.get("status", "").strip()
        q = request.args.get("q", "").strip()
        sql = "SELECT * FROM items WHERE 1=1"
        args = []
        if status in ITEM_STATUSES:
            sql += " AND status=?"; args.append(status)
        if q:
            sql += " AND (title LIKE ? OR public_reference LIKE ? OR reporter_name LIKE ?)"
            args.extend([f"%{q}%"] * 3)
        sql += " ORDER BY created_at DESC"
        return render_template("manage_items.html", items=get_db().execute(sql, args).fetchall())

    @app.route("/manage/items/new", methods=["GET", "POST"])
    @admin_required
    def admin_post_item():
        db = get_db()
        if request.method == "POST":
            report_type = request.form.get("report_type", "").strip()
            title = request.form.get("title", "").strip()
            public_description = request.form.get("public_description", "").strip()
            private_details = request.form.get("private_details", "").strip()
            event_date = request.form.get("event_date", "").strip()
            category_id = request.form.get("category_id", "")
            location_id = request.form.get("location_id", "")
            errors = []
            if report_type not in {"lost", "found"}: errors.append("Choose Lost or Found.")
            if len(title) < 3: errors.append("Item name must contain at least 3 characters.")
            if len(public_description) < 10: errors.append("Public description must contain at least 10 characters.")
            if len(private_details) < 5: errors.append("Add a private verification detail.")
            try:
                parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                if parsed_date > datetime.now(timezone.utc).date(): errors.append("The date cannot be in the future.")
            except ValueError:
                errors.append("Enter a valid date.")
            if category_id and (not category_id.isdigit() or not db.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone()):
                errors.append("Choose a valid category.")
            if location_id and (not location_id.isdigit() or not db.execute("SELECT 1 FROM locations WHERE id=?", (location_id,)).fetchone()):
                errors.append("Choose a valid location.")
            image_name = None
            if not errors:
                image_name, image_error = save_image(request.files.get("image"))
                if image_error: errors.append(image_error)
            if errors:
                for error in errors: flash(error, "error")
            else:
                user = db.execute("SELECT full_name FROM users WHERE id=?", (session["user_id"],)).fetchone()
                reference = unique_reference(db)
                token = secrets.token_urlsafe(24)
                db.execute(
                    """INSERT INTO items
                    (public_reference, qr_token, report_type, title, public_description,
                     private_details, category_id, location_id, event_date, reporter_name,
                     reporter_contact, image_name, status, approved_by, approved_at, created_at, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'published',?,?,?,?)""",
                    (reference, token, report_type, title, public_description, private_details,
                     category_id or None, location_id or None, event_date, user["full_name"],
                     "Lost and Found Office", image_name, session["user_id"], now(), now(), now()),
                )
                item_id = db.execute("SELECT id FROM items WHERE public_reference=?", (reference,)).fetchone()[0]
                db.commit()
                audit("direct_post", "item", item_id, f"Administrator posted {reference} directly")
                flash(f"Item posted successfully and is now visible publicly: {reference}", "success")
                return redirect(url_for("review_item", item_id=item_id))
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        locations = db.execute("SELECT * FROM locations ORDER BY name").fetchall()
        return render_template("admin_post_item.html", categories=categories, locations=locations)

    @app.route("/manage/items/<int:item_id>", methods=["GET", "POST"])
    @staff_required
    def review_item(item_id):
        db = get_db()
        item = db.execute(
            """SELECT i.*, c.name category_name, l.name location_name FROM items i
               LEFT JOIN categories c ON c.id=i.category_id LEFT JOIN locations l ON l.id=i.location_id
               WHERE i.id=?""", (item_id,)
        ).fetchone()
        if not item: abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action == "save":
                title = request.form.get("title", "").strip()
                public_description = request.form.get("public_description", "").strip()
                private_details = request.form.get("private_details", "").strip()
                event_date = request.form.get("event_date", "").strip()
                category_id = request.form.get("category_id", "")
                location_id = request.form.get("location_id", "")
                valid_date = True
                try:
                    parsed_date = datetime.strptime(event_date, "%Y-%m-%d").date()
                    valid_date = parsed_date <= datetime.now(timezone.utc).date()
                except ValueError:
                    valid_date = False
                valid_category = not category_id or (category_id.isdigit() and db.execute("SELECT 1 FROM categories WHERE id=?", (category_id,)).fetchone())
                valid_location = not location_id or (location_id.isdigit() and db.execute("SELECT 1 FROM locations WHERE id=?", (location_id,)).fetchone())
                if len(title) < 3 or len(public_description) < 10 or len(private_details) < 5 or not valid_date or not valid_category or not valid_location:
                    flash("The item name and both descriptions need more detail.", "error")
                else:
                    db.execute(
                        """UPDATE items SET title=?, public_description=?, private_details=?,
                           category_id=?, location_id=?, event_date=?, updated_at=? WHERE id=?""",
                        (title, public_description, private_details,
                         category_id or None, location_id or None, event_date, now(), item_id),
                    )
                    db.commit()
                    notify(db, None, "item_updated", item_id, f"Item {item['public_reference']} information was updated.")
                    audit("edit", "item", item_id, f"Updated item {item['public_reference']}")
                    flash("Item information saved.", "success")
                return redirect(url_for("review_item", item_id=item_id))
            status_map = {"approve": "published", "reject": "rejected", "archive": "archived", "match": "matched"}
            if action not in status_map:
                abort(400)
            allowed_actions = {
                "pending_review": {"approve", "reject", "archive"},
                "published": {"match", "archive"},
                "matched": {"archive"},
                "claim_pending": {"match", "archive"},
                "rejected": {"archive"},
                "returned": {"archive"},
                "archived": set(),
            }
            if action not in allowed_actions.get(item["status"], set()):
                abort(409, "That status change is no longer allowed.")
            if action == "archive":
                open_claims = db.execute(
                    "SELECT COUNT(*) n FROM claims WHERE item_id=? AND status IN ('pending','approved')",
                    (item_id,),
                ).fetchone()["n"]
                if open_claims:
                    flash("Resolve all pending or approved claims before archiving this item.", "error")
                    return redirect(url_for("review_item", item_id=item_id))
            new_status = status_map[action]
            approved_at = now() if action == "approve" else item["approved_at"]
            db.execute(
                "UPDATE items SET status=?, approved_at=?, approved_by=?, updated_at=? WHERE id=?",
                (new_status, approved_at, session["user_id"], now(), item_id),
            )
            db.commit()
            if new_status in {"published", "rejected", "returned"}:
                notify(db, None, "item_status", item_id, f"Item {item['public_reference']} is now {new_status}.")
            audit(action, "item", item_id, f"Item {item['public_reference']} changed to {new_status}")
            flash(f"Item status updated to {new_status.replace('_',' ')}.", "success")
            return redirect(url_for("review_item", item_id=item_id))
        suggestions = db.execute(
            """SELECT i.*, c.name category_name, l.name location_name
               FROM items i LEFT JOIN categories c ON c.id=i.category_id
               LEFT JOIN locations l ON l.id=i.location_id
               WHERE i.id<>? AND i.report_type<>? AND i.status IN ('published','matched','claim_pending')
                 AND (i.category_id=? OR lower(i.title) LIKE lower(?))
               ORDER BY CASE WHEN i.category_id=? THEN 0 ELSE 1 END, i.event_date DESC LIMIT 6""",
            (item_id, item["report_type"], item["category_id"], f"%{item['title']}%", item["category_id"]),
        ).fetchall()
        categories = db.execute("SELECT * FROM categories ORDER BY name").fetchall()
        locations = db.execute("SELECT * FROM locations ORDER BY name").fetchall()
        return render_template("review_item.html", item=item, suggestions=suggestions,
                               categories=categories, locations=locations)

    @app.route("/manage/items/<int:item_id>/qr")
    @staff_required
    def item_qr(item_id):
        item = get_db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item: abort(404)
        target = url_for("qr_track", token=item["qr_token"], _external=True)
        qr = qrcode.QRCode(version=None, box_size=9, border=3)
        qr.add_data(target); qr.make(fit=True)
        image = qr.make_image(fill_color="#063b2b", back_color="white")
        output = io.BytesIO(); image.save(output, format="PNG"); output.seek(0)
        return send_file(output, mimetype="image/png", download_name=f"{item['public_reference']}-qr.png")

    @app.route("/manage/items/<int:item_id>/label")
    @staff_required
    def item_label(item_id):
        item = get_db().execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if not item: abort(404)
        return render_template("qr_label.html", item=item)

    @app.route("/manage/scanner")
    @staff_required
    def qr_scanner():
        return render_template("qr_scanner.html")

    @app.route("/manage/scanner/lookup")
    @staff_required
    def qr_scanner_lookup():
        value = request.args.get("value", "").strip()
        token = value.rsplit("/track/", 1)[-1].split("?", 1)[0].strip()
        item = get_db().execute("SELECT id FROM items WHERE qr_token=?", (token,)).fetchone()
        if not item:
            flash("That QR code is not recognized as a school item record.", "error")
            return redirect(url_for("qr_scanner"))
        return redirect(url_for("review_item", item_id=item["id"]))

    @app.route("/manage/claims")
    @staff_required
    def manage_claims():
        claims = get_db().execute(
            """SELECT cl.*, i.title, i.public_reference FROM claims cl
               JOIN items i ON i.id=cl.item_id ORDER BY cl.created_at DESC"""
        ).fetchall()
        return render_template("manage_claims.html", claims=claims)

    @app.route("/manage/claims/<int:claim_id>", methods=["GET", "POST"])
    @staff_required
    def review_claim(claim_id):
        db = get_db()
        claim = db.execute(
            """SELECT cl.*, i.title, i.public_reference, i.private_details, i.id item_id
               FROM claims cl JOIN items i ON i.id=cl.item_id WHERE cl.id=?""", (claim_id,)
        ).fetchone()
        if not claim: abort(404)
        if request.method == "POST":
            action = request.form.get("action")
            if action not in {"approve", "reject", "release"}: abort(400)
            allowed_actions = {
                "pending": {"approve", "reject"},
                "approved": {"release", "reject"},
                "rejected": set(),
                "released": set(),
            }
            if action not in allowed_actions.get(claim["status"], set()):
                abort(409, "That claim action is no longer allowed.")
            item_status = db.execute("SELECT status FROM items WHERE id=?", (claim["item_id"],)).fetchone()["status"]
            if action in {"approve", "release"} and item_status not in {"published", "matched", "claim_pending"}:
                abort(409, "The item is no longer available for claim approval or release.")
            if action == "release" and item_status == "returned":
                abort(409, "This item was already released.")
            new_status = {"approve": "approved", "reject": "rejected", "release": "released"}[action]
            notes = request.form.get("staff_notes", "").strip()
            released_at = now() if action == "release" else claim["released_at"]
            db.execute(
                "UPDATE claims SET status=?, staff_notes=?, reviewed_by=?, reviewed_at=?, released_at=? WHERE id=?",
                (new_status, notes, session["user_id"], now(), released_at, claim_id),
            )
            if action == "release":
                db.execute("UPDATE items SET status='returned', updated_at=? WHERE id=?", (now(), claim["item_id"]))
                db.execute(
                    """UPDATE claims SET status='rejected', staff_notes=CASE
                       WHEN staff_notes IS NULL OR staff_notes='' THEN 'Closed because another verified claim was released.'
                       ELSE staff_notes || ' | Closed because another verified claim was released.' END,
                       reviewed_by=?, reviewed_at=?
                       WHERE item_id=? AND id<>? AND status IN ('pending','approved')""",
                    (session["user_id"], now(), claim["item_id"], claim_id),
                )
            elif action == "reject":
                remaining = db.execute(
                    "SELECT COUNT(*) n FROM claims WHERE item_id=? AND id<>? AND status IN ('pending','approved')",
                    (claim["item_id"], claim_id),
                ).fetchone()["n"]
                if not remaining:
                    db.execute("UPDATE items SET status='published', updated_at=? WHERE id=?", (now(), claim["item_id"]))
            db.commit()
            db.execute("INSERT INTO claim_events(claim_id,status,notes,created_at) VALUES(?,?,?,?)", (claim_id, new_status, notes, now()))
            notify(db, claim["claimant_name"], "claim_status", claim_id, f"Claim {claim['claim_reference']} is now {new_status}.")
            db.commit()
            audit(action, "claim", claim_id, f"Claim {claim['claim_reference']} changed to {new_status}")
            flash(f"Claim marked {new_status}.", "success")
            return redirect(url_for("review_claim", claim_id=claim_id))
        return render_template("review_claim.html", claim=claim)

    @app.route("/manage/claims/<int:claim_id>/receipt")
    @staff_required
    def claim_receipt(claim_id):
        claim = get_db().execute(
            """SELECT cl.*, i.title, i.public_reference, u.full_name staff_name
               FROM claims cl JOIN items i ON i.id=cl.item_id
               LEFT JOIN users u ON u.id=cl.reviewed_by WHERE cl.id=? AND cl.status='released'""",
            (claim_id,),
        ).fetchone()
        if not claim: abort(404)
        return render_template("receipt.html", claim=claim)

    @app.route("/manage/users", methods=["GET", "POST"])
    @admin_required
    def manage_users():
        db = get_db()
        if request.method == "POST":
            username = request.form.get("username", "").strip().lower()
            full_name = request.form.get("full_name", "").strip()
            role = request.form.get("role", "")
            password = request.form.get("password", "")
            if role not in {"admin", "staff"} or len(username) < 3 or len(full_name) < 2 or len(password) < 10:
                flash("Provide a valid name, username, role, and password of at least 10 characters.", "error")
            else:
                try:
                    cur = db.execute(
                        "INSERT INTO users(username,password_hash,full_name,role,active,must_change_password,created_at) VALUES(?,?,?,?,1,1,?)",
                        (username, generate_password_hash(password), full_name, role, now()),
                    )
                    db.commit(); audit("create", "user", cur.lastrowid, f"Created {role} account {username}")
                    flash("Account created.", "success")
                except sqlite3.IntegrityError:
                    flash("That username already exists.", "error")
            return redirect(url_for("manage_users"))
        users = db.execute("SELECT id,username,full_name,role,active,last_login,created_at FROM users ORDER BY role,full_name").fetchall()
        return render_template("manage_users.html", users=users)

    @app.route("/manage/users/<int:user_id>/toggle", methods=["POST"])
    @admin_required
    def toggle_user(user_id):
        if user_id == session["user_id"]:
            flash("You cannot disable your own account.", "error")
        else:
            db = get_db(); user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not user: abort(404)
            active = 0 if user["active"] else 1
            db.execute("UPDATE users SET active=? WHERE id=?", (active, user_id)); db.commit()
            audit("toggle_account", "user", user_id, f"Account active={active}")
            flash("Account status updated.", "success")
        return redirect(url_for("manage_users"))

    @app.route("/manage/taxonomy", methods=["GET", "POST"])
    @admin_required
    def manage_taxonomy():
        db = get_db()
        if request.method == "POST":
            kind = request.form.get("kind")
            name = request.form.get("name", "").strip()
            table = {"category": "categories", "location": "locations"}.get(kind)
            if not table or len(name) < 2:
                flash("Choose a valid list and enter a name.", "error")
            else:
                try:
                    db.execute(f"INSERT INTO {table}(name) VALUES(?)", (name,))
                    db.commit()
                    audit("create", kind, None, f"Added {kind}: {name}")
                    flash(f"{kind.title()} added.", "success")
                except sqlite3.IntegrityError:
                    flash(f"That {kind} already exists.", "error")
            return redirect(url_for("manage_taxonomy"))
        categories = db.execute(
            """SELECT c.*, COUNT(i.id) usage_count FROM categories c
               LEFT JOIN items i ON i.category_id=c.id GROUP BY c.id ORDER BY c.name"""
        ).fetchall()
        locations = db.execute(
            """SELECT l.*, COUNT(i.id) usage_count FROM locations l
               LEFT JOIN items i ON i.location_id=l.id GROUP BY l.id ORDER BY l.name"""
        ).fetchall()
        return render_template("manage_taxonomy.html", categories=categories, locations=locations)

    @app.route("/manage/reports")
    @staff_required
    def reports():
        db = get_db()
        monthly = db.execute(
            """SELECT substr(created_at,1,7) month, report_type, COUNT(*) total
               FROM items GROUP BY month, report_type ORDER BY month DESC LIMIT 24"""
        ).fetchall()
        categories = db.execute(
            """SELECT COALESCE(c.name,'Uncategorized') name, COUNT(*) total FROM items i
               LEFT JOIN categories c ON c.id=i.category_id GROUP BY c.name ORDER BY total DESC"""
        ).fetchall()
        return render_template("reports.html", monthly=monthly, categories=categories)

    @app.route("/manage/backup")
    @admin_required
    def backup_database():
        db = get_db(); output = io.BytesIO(); output.write(b"-- SFXC Lost and Found backup\n")
        for line in db.iterdump(): output.write((line + "\n").encode())
        output.seek(0)
        return send_file(output, mimetype="application/sql", as_attachment=True, download_name="sfxc-lost-found-backup.sql")

    @app.route("/manage/reports/items.csv")
    @staff_required
    def export_items():
        rows = get_db().execute(
            """SELECT public_reference,report_type,title,event_date,status,created_at,approved_at
               FROM items ORDER BY created_at DESC"""
        ).fetchall()
        out = io.StringIO(); writer = csv.writer(out)
        writer.writerow(["Reference", "Type", "Item", "Event date", "Status", "Submitted", "Approved"])
        writer.writerows([tuple(row) for row in rows])
        response = make_response(out.getvalue())
        response.headers["Content-Type"] = "text/csv; charset=utf-8"
        response.headers["Content-Disposition"] = "attachment; filename=lost-found-items.csv"
        return response

    @app.route("/manage/audit")
    @admin_required
    def audit_log():
        logs = get_db().execute(
            """SELECT a.*, u.full_name FROM audit_logs a LEFT JOIN users u ON u.id=a.user_id
               ORDER BY a.created_at DESC LIMIT 500"""
        ).fetchall()
        return render_template("audit_log.html", logs=logs)

    @app.route("/uploads/<path:name>")
    def uploaded_file(name):
        from flask import send_from_directory
        return send_from_directory(app.config["UPLOAD_FOLDER"], secure_filename(name))

    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    @app.errorhandler(413)
    @app.errorhandler(500)
    def error_page(error):
        code = getattr(error, "code", 500)
        messages = {400:"That request could not be processed.",403:"You do not have permission to view this page.",404:"The requested page was not found.",409:"That record changed and the requested action is no longer allowed.",413:"The uploaded file is too large.",500:"An unexpected error occurred."}
        return render_template("error.html", code=code, message=messages.get(code, messages[500])), code

    return app


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"], uri=current_app.config.get("DATABASE_URI", False))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
        g.db.execute("PRAGMA busy_timeout=5000")
    return g.db


def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None: db.close()


def init_db(application):
    with application.app_context():
        db = sqlite3.connect(application.config["DATABASE"], uri=application.config.get("DATABASE_URI", False))
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=5000")
        db.executescript(SCHEMA)
        for name in ["Electronics", "Identification Cards", "Documents", "Bags", "Clothing", "Accessories", "School Supplies", "Keys", "Money and Wallets", "Others"]:
            db.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
        for name in ["Library", "Canteen", "Gymnasium", "Computer Laboratory", "Classrooms", "Registrar", "School Grounds", "Student Services Office", "Others"]:
            db.execute("INSERT OR IGNORE INTO locations(name) VALUES(?)", (name,))
        users = [
            ("admin", os.getenv("ADMIN_PASSWORD", "ChangeMe!2026"), "System Administrator", "admin"),
            ("staff", os.getenv("STAFF_PASSWORD", "StaffChange!2026"), "Lost and Found Staff", "staff"),
        ]
        for username, password, name, role in users:
            db.execute(
                """INSERT OR IGNORE INTO users(username,password_hash,full_name,role,active,must_change_password,created_at)
                   VALUES(?,?,?,?,1,1,?)""",
                (username, generate_password_hash(password), name, role, now()),
            )
        db.commit(); db.close()


def get_csrf_token():
    if "csrf_token" not in session:
        session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def unique_reference(db):
    while True:
        ref = f"LF-{datetime.now().year}-{secrets.randbelow(900000)+100000}"
        if not db.execute("SELECT 1 FROM items WHERE public_reference=?", (ref,)).fetchone(): return ref


def unique_claim_reference(db):
    while True:
        ref = f"CLM-{secrets.randbelow(900000)+100000}"
        if not db.execute("SELECT 1 FROM claims WHERE claim_reference=?", (ref,)).fetchone(): return ref


def save_image(file):
    if not file or not file.filename: return None, None
    extension = file.filename.rsplit(".", 1)[-1].lower() if "." in file.filename else ""
    if extension not in ALLOWED_EXTENSIONS:
        return None, "Image must be PNG, JPG, JPEG, or WEBP."
    try:
        file.stream.seek(0)
        with Image.open(file.stream) as candidate:
            candidate.verify()
        file.stream.seek(0)
        with Image.open(file.stream) as source:
            detected = (source.format or "").upper()
            output_extension = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp"}.get(detected)
            if not output_extension:
                return None, "The uploaded file is not a supported image."
            source.thumbnail((2400, 2400))
            if output_extension == "jpg":
                cleaned = source.convert("RGB")
            elif source.mode not in {"RGB", "RGBA"}:
                cleaned = source.convert("RGBA")
            else:
                cleaned = source.copy()
            name = f"{secrets.token_hex(16)}.{output_extension}"
            destination = Path(current_app.config["UPLOAD_FOLDER"]) / name
            cleaned.save(destination, format=detected, quality=88, optimize=True)
            return name, None
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        return None, "The uploaded file is damaged or is not a real image."


def audit(action, entity_type, entity_id, details):
    db = get_db()
    db.execute(
        "INSERT INTO audit_logs(user_id,action,entity_type,entity_id,details,ip_address,created_at) VALUES(?,?,?,?,?,?,?)",
        (session.get("user_id"), action, entity_type, entity_id, details, request.remote_addr, now()),
    )
    db.commit()


def notify(db, recipient, kind, entity_id, message):
    """Store a notification without requiring SMTP configuration."""
    db.execute("INSERT INTO notifications(recipient,kind,entity_id,message,created_at) VALUES(?,?,?,?,?)", (recipient, kind, entity_id, message, now()))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            flash("Sign in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def staff_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") not in {"staff", "admin"}: abort(403)
        if session.get("must_change_password") and request.endpoint != "change_password":
            return redirect(url_for("change_password"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"): return redirect(url_for("login"))
        if session.get("role") != "admin": abort(403)
        if session.get("must_change_password"): return redirect(url_for("change_password"))
        return view(*args, **kwargs)
    return wrapped


SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
 id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL,
 full_name TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('admin','staff')),
 active INTEGER NOT NULL DEFAULT 1, must_change_password INTEGER NOT NULL DEFAULT 1,
 last_login TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS locations(id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL);
CREATE TABLE IF NOT EXISTS items(
 id INTEGER PRIMARY KEY, public_reference TEXT UNIQUE NOT NULL, qr_token TEXT UNIQUE NOT NULL,
 report_type TEXT NOT NULL CHECK(report_type IN ('lost','found')), title TEXT NOT NULL,
 public_description TEXT NOT NULL, private_details TEXT NOT NULL,
 category_id INTEGER REFERENCES categories(id), location_id INTEGER REFERENCES locations(id),
 event_date TEXT NOT NULL, reporter_name TEXT NOT NULL, reporter_contact TEXT NOT NULL,
 image_name TEXT, status TEXT NOT NULL DEFAULT 'pending_review',
 approved_by INTEGER REFERENCES users(id), approved_at TEXT, created_at TEXT NOT NULL, updated_at TEXT);
CREATE TABLE IF NOT EXISTS claims(
 id INTEGER PRIMARY KEY, claim_reference TEXT UNIQUE NOT NULL, item_id INTEGER NOT NULL REFERENCES items(id),
 claimant_name TEXT NOT NULL, contact TEXT NOT NULL, identification TEXT NOT NULL,
 proof_description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', staff_notes TEXT,
 reviewed_by INTEGER REFERENCES users(id), reviewed_at TEXT, released_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_logs(
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), action TEXT NOT NULL,
 entity_type TEXT NOT NULL, entity_id INTEGER, details TEXT, ip_address TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
 id INTEGER PRIMARY KEY, recipient TEXT, kind TEXT NOT NULL, entity_id INTEGER,
 message TEXT NOT NULL, read_at TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS claim_events(
 id INTEGER PRIMARY KEY, claim_id INTEGER NOT NULL REFERENCES claims(id), status TEXT NOT NULL,
 notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS scan_logs(
 id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), item_id INTEGER REFERENCES items(id),
 scanned_value TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS custody_events(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id), user_id INTEGER REFERENCES users(id),
 location TEXT NOT NULL, notes TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS item_images(
 id INTEGER PRIMARY KEY, item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
 image_name TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_reference ON items(public_reference);
CREATE INDEX IF NOT EXISTS idx_claims_item ON claims(item_id);
CREATE INDEX IF NOT EXISTS idx_claim_events_claim ON claim_events(claim_id);
CREATE INDEX IF NOT EXISTS idx_item_images_item ON item_images(item_id);
"""


app = create_app()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--init-db", action="store_true")
    args = parser.parse_args()
    if args.init_db:
        init_db(app)
        print("Database initialized.")
    else:
        init_db(app)
        app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")), debug=os.getenv("FLASK_DEBUG") == "1")
