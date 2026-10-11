# Web-Based Lost and Found Management System with QR Code Item Tracking

A responsive Flask and SQLite application for St. Francis Xavier College. Public visitors can browse approved records, submit lost/found reports, check references, and submit claims. Staff moderate and correct reports, review possible matches, print QR labels, verify ownership, release items, and retain a complete audit history. Administrators additionally manage accounts, categories, campus locations, and system records.

## Version 2 highlights

- Full St. Francis Xavier College name throughout the interface
- Modern school-themed responsive design with persistent light/dark mode
- Public report and claim reference lookup
- Staff editing for submitted item records before publication
- Automatic lost/found possible-match suggestions
- Administrator-managed categories and campus locations
- Safer upgrade startup that preserves existing records while applying schema updates
- Verified image uploads that are sanitized and resized before storage
- Protected item and claim status transitions to prevent repeated or skipped actions
- Automatic closure of competing claims after a verified item release
- Safer form redirects and SQLite locking behavior for repeated/concurrent use

## Version 3 debugging pass

- Rejected renamed or damaged non-image uploads
- Prevented unused image files when report validation fails
- Blocked skipped, repeated, and stale item/claim actions
- Prevented more than one claimant from receiving the same item
- Blocked archiving while unresolved claims still exist
- Added future-date and invalid category/location validation
- Added post/redirect/get behavior to prevent duplicate form submissions on refresh
- Enabled SQLite WAL mode and a busy timeout for more reliable multi-device use

## Version 4 visual redesign

- Completely separate visual identity from the student clearance system
- Campus bulletin-board and wayfinding concept
- Warm paper-grid background instead of soft gradient/particle backgrounds
- Bold navy sign panels, gold labels, emerald actions, and editorial typography
- Pinned notice-style item cards and a corkboard-inspired landing visual
- Square record sheets and offset print-style shadows instead of glass cards
- Restyled staff navigation, forms, tables, status labels, dark mode, and print views

## Version 5 workflow update

- Administrators can directly publish official lost/found records
- Direct posts are marked with the administrator and recorded in the audit trail
- Public claim flow now explains three simple steps: identify yourself, show your ID, and prove ownership
- Returned items no longer offer a new claim action
- `python test.py` runs the complete discovered test suite and returns a failure code if any check fails

## Version 6 public background and motion update

- Added the supplied St. Francis Xavier College campus building photos as subtle white/grayscale watermark backgrounds
- Added a softer celebration-photo watermark on public pages
- Added staggered page, card, and panel entrance transitions
- Added gentle crest and notice-card motion
- Added hover transitions and a reduced-motion accessibility fallback

## Version 7 school portal polish

- Increased campus-building background visibility on public pages
- Added a school-branded header kicker: “SFXC Official Campus Portal”
- Rebuilt the footer into school information sections with portal links, office information, and safe-claiming guidance
- Added a separated footer copyright bar and responsive footer columns

## Quick start

1. Install Python 3.11 or newer.
2. Open a terminal inside this folder.
3. Create and activate a virtual environment:
   - Windows: `python -m venv .venv` then `.venv\\Scripts\\activate`
   - macOS/Linux: `python -m venv .venv` then `source .venv/bin/activate`
4. Install packages: `pip install -r requirements.txt`
5. Initialize the database: `python app.py --init-db`
6. Start locally: `python app.py`
7. Open `http://127.0.0.1:5000`.

For other phones/tablets on the same Wi-Fi, use the laptop's local IP followed by `:5000`. For public internet access, deploy with HTTPS and set `SECRET_KEY`, `ADMIN_PASSWORD`, and `STAFF_PASSWORD` environment variables.

## Initial accounts

The database initializer creates these development accounts:

- Administrator: `admin` / `ChangeMe!2026`
- Staff: `staff` / `StaffChange!2026`

Both accounts must change their password at first login. Override the initial passwords using the `ADMIN_PASSWORD` and `STAFF_PASSWORD` environment variables before initialization.

## Tests

Run `python -m unittest -v` from the project folder.
