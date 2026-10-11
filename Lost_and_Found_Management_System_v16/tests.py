import io
import os
import tempfile
import unittest
from pathlib import Path

from app import create_app, init_db
from PIL import Image


class LostFoundSystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app({
            "TESTING": True,
            "SECRET_KEY": "test-secret",
            "DATABASE": str(Path(self.temp.name) / "test.sqlite3"),
            "UPLOAD_FOLDER": str(Path(self.temp.name) / "uploads"),
        })
        init_db(self.app)
        self.client = self.app.test_client()

    def tearDown(self):
        self.temp.cleanup()

    def token(self):
        with self.client.session_transaction() as sess:
            return sess.setdefault("csrf_token", "test-token")

    def login(self, username="admin", password="ChangeMe!2026"):
        return self.client.post("/login", data={"username":username,"password":password,"csrf_token":self.token()})

    def change_password(self):
        return self.client.post("/change-password", data={"password":"NewSecure!2026","confirm_password":"NewSecure!2026","csrf_token":self.token()})

    def test_public_pages_load(self):
        for path in ["/", "/items", "/report", "/check-status", "/login"]:
            self.assertEqual(self.client.get(path).status_code, 200)

    def test_qr_scanner_is_staff_only_and_loads(self):
        self.assertEqual(self.client.get("/manage/scanner").status_code, 302)
        self.login(); self.change_password()
        response = self.client.get("/manage/scanner")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Start Camera Scanner", response.data)
        self.assertIn(b"scanner.js", response.data)

    def test_in_memory_database_survives_request_connections(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "memory-test", "DATABASE": ":memory:", "UPLOAD_FOLDER": str(Path(self.temp.name) / "memory-uploads")})
        init_db(app)
        self.assertEqual(app.test_client().get("/").status_code, 200)

    def test_full_school_name_is_visible(self):
        response = self.client.get("/")
        self.assertIn(b"St. Francis Xavier College", response.data)
        self.assertNotIn(b">SFCollege<", response.data)
        self.assertIn(b"Portal links", response.data)
        self.assertIn(b"Office information", response.data)
        self.assertIn(b"SFXC OFFICIAL CAMPUS PORTAL", response.data)

    def test_csrf_blocks_unprotected_post(self):
        self.assertEqual(self.client.post("/report", data={}).status_code, 400)

    def test_public_report_is_pending_and_private(self):
        response = self.client.post("/report", data={
            "csrf_token": self.token(), "report_type":"found", "title":"Blue Umbrella",
            "public_description":"Blue folding umbrella found near a building.",
            "private_details":"Small white scratch on its wooden handle", "event_date":"2026-09-16",
            "reporter_name":"Public Reporter", "reporter_contact":"09123456789",
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Report received", response.data)
        browse = self.client.get("/items")
        self.assertNotIn(b"Blue Umbrella", browse.data)
        self.assertNotIn(b"09123456789", browse.data)

    def test_admin_login_requires_password_change(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/change-password", response.location)
        self.assertEqual(self.change_password().status_code, 302)
        self.assertEqual(self.client.get("/dashboard").status_code, 200)

    def test_staff_cannot_access_admin_pages(self):
        self.login("staff", "StaffChange!2026")
        self.client.post("/change-password", data={"password":"StaffSecure!2026","confirm_password":"StaffSecure!2026","csrf_token":self.token()})
        self.assertEqual(self.client.get("/manage/users").status_code, 403)
        self.assertEqual(self.client.get("/manage/audit").status_code, 403)

    def test_report_approval_claim_and_release_workflow(self):
        self.client.post("/report", data={
            "csrf_token":self.token(),"report_type":"found","title":"Calculator",
            "public_description":"Black scientific calculator found in a classroom.",
            "private_details":"Owner name written under the battery cover","event_date":"2026-09-16",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        self.login(); self.change_password()
        with self.app.app_context():
            import sqlite3
            db=sqlite3.connect(self.app.config["DATABASE"]); db.row_factory=sqlite3.Row
            item=db.execute("SELECT * FROM items").fetchone(); item_id=item["id"]; ref=item["public_reference"]; db.close()
        self.client.post(f"/manage/items/{item_id}",data={"csrf_token":self.token(),"action":"approve"})
        self.assertIn(b"Calculator",self.client.get("/items").data)
        self.client.post("/logout",data={"csrf_token":self.token()})
        response=self.client.post(f"/items/{ref}/claim",data={"csrf_token":self.token(),"claimant_name":"Owner","contact":"owner@example.com","identification":"Student ID 123","proof_description":"My name is under the battery cover"})
        self.assertEqual(response.status_code,302)
        self.login(password="NewSecure!2026")
        with self.app.app_context():
            db=sqlite3.connect(self.app.config["DATABASE"]); claim_id=db.execute("SELECT id FROM claims").fetchone()[0]; db.close()
        self.client.post(f"/manage/claims/{claim_id}",data={"csrf_token":self.token(),"action":"approve","staff_notes":"Details match"})
        self.client.post(f"/manage/claims/{claim_id}",data={"csrf_token":self.token(),"action":"release","staff_notes":"ID verified"})
        self.assertEqual(self.client.get(f"/manage/claims/{claim_id}/receipt").status_code,200)

    def test_security_headers_are_present(self):
        response=self.client.get("/")
        self.assertEqual(response.headers["X-Content-Type-Options"],"nosniff")
        self.assertIn("frame-ancestors",response.headers["Content-Security-Policy"])

    def test_reference_lookup_redirects_to_correct_status(self):
        self.client.post("/report", data={
            "csrf_token": self.token(), "report_type":"lost", "title":"Notebook",
            "public_description":"Green notebook missing near the library entrance.",
            "private_details":"Initials written on the final page", "event_date":"2026-09-17",
            "reporter_name":"Reporter", "reporter_contact":"reporter@example.com",
        })
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"])
        reference=db.execute("SELECT public_reference FROM items").fetchone()[0]
        db.close()
        response=self.client.post("/check-status",data={"csrf_token":self.token(),"reference":reference.lower()})
        self.assertEqual(response.status_code,302)
        self.assertIn(f"/submission/{reference}",response.location)

    def test_admin_can_add_category_and_edit_item(self):
        self.client.post("/report", data={
            "csrf_token":self.token(),"report_type":"found","title":"Water Bottle",
            "public_description":"Reusable blue water bottle found near the gym.",
            "private_details":"There is a name sticker below the base","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        self.login(); self.change_password()
        response=self.client.post("/manage/taxonomy",data={"csrf_token":self.token(),"kind":"category","name":"Sports Equipment"})
        self.assertEqual(response.status_code,302)
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"])
        item_id=db.execute("SELECT id FROM items").fetchone()[0]
        category_id=db.execute("SELECT id FROM categories WHERE name='Sports Equipment'").fetchone()[0]
        db.close()
        response=self.client.post(f"/manage/items/{item_id}",data={
            "csrf_token":self.token(),"action":"save","title":"Blue Water Bottle",
            "public_description":"Reusable blue water bottle found beside the gym entrance.",
            "private_details":"There is a name sticker below the base","event_date":"2026-09-17",
            "category_id":category_id,"location_id":""})
        self.assertEqual(response.status_code,302)
        db=sqlite3.connect(self.app.config["DATABASE"])
        updated=db.execute("SELECT title,category_id FROM items WHERE id=?",(item_id,)).fetchone()
        db.close()
        self.assertEqual(updated,("Blue Water Bottle",category_id))

    def test_fake_image_is_rejected_and_not_saved(self):
        response=self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"Flash Drive",
            "public_description":"A black flash drive found near the computer laboratory.",
            "private_details":"Small colored marking on one side","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com",
            "image":(io.BytesIO(b"this is not an image"),"fake.jpg")
        },content_type="multipart/form-data")
        self.assertEqual(response.status_code,200)
        self.assertIn(b"not a real image",response.data)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).glob("*")),[])

    def test_invalid_form_does_not_leave_orphan_image(self):
        image_data=io.BytesIO()
        Image.new("RGB",(20,20),"green").save(image_data,format="PNG")
        image_data.seek(0)
        response=self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"x",
            "public_description":"short","private_details":"mark","event_date":"2099-01-01",
            "reporter_name":"A","reporter_contact":"1",
            "image":(image_data,"valid.png")
        },content_type="multipart/form-data")
        self.assertEqual(response.status_code,200)
        self.assertEqual(list(Path(self.app.config["UPLOAD_FOLDER"]).glob("*")),[])

    def test_real_image_is_sanitized_and_saved_by_detected_format(self):
        image_data=io.BytesIO()
        Image.new("RGB",(30,30),"gold").save(image_data,format="PNG")
        image_data.seek(0)
        response=self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"Keychain",
            "public_description":"A small keychain found near the school entrance.",
            "private_details":"A hidden initial is engraved on the back","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com",
            "image":(image_data,"misnamed.jpg")
        },content_type="multipart/form-data")
        self.assertEqual(response.status_code,302)
        saved=list(Path(self.app.config["UPLOAD_FOLDER"]).glob("*"))
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0].suffix,".png")
        with Image.open(saved[0]) as verified:
            self.assertEqual(verified.format,"PNG")

    def test_claim_cannot_skip_approval(self):
        self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"Phone Case",
            "public_description":"A clear phone case found outside the registrar office.",
            "private_details":"A small sticker is hidden behind the insert","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        self.login(); self.change_password()
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); db.row_factory=sqlite3.Row
        item=db.execute("SELECT * FROM items").fetchone(); db.close()
        self.client.post(f"/manage/items/{item['id']}",data={"csrf_token":self.token(),"action":"approve"})
        self.client.post("/logout",data={"csrf_token":self.token()})
        self.client.post(f"/items/{item['public_reference']}/claim",data={
            "csrf_token":self.token(),"claimant_name":"Owner","contact":"owner@example.com",
            "identification":"Student ID 100","proof_description":"I know the hidden sticker"})
        self.login(password="NewSecure!2026")
        db=sqlite3.connect(self.app.config["DATABASE"]); claim_id=db.execute("SELECT id FROM claims").fetchone()[0]; db.close()
        response=self.client.post(f"/manage/claims/{claim_id}",data={"csrf_token":self.token(),"action":"release","staff_notes":"Attempted skip"})
        self.assertEqual(response.status_code,409)

    def test_releasing_one_claim_closes_competing_claims(self):
        self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"ID Holder",
            "public_description":"A dark ID holder found beside the campus library.",
            "private_details":"The hidden card has a specific department name","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        self.login(); self.change_password()
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); db.row_factory=sqlite3.Row
        item=db.execute("SELECT * FROM items").fetchone(); db.close()
        self.client.post(f"/manage/items/{item['id']}",data={"csrf_token":self.token(),"action":"approve"})
        self.client.post("/logout",data={"csrf_token":self.token()})
        for number in (1,2):
            self.client.post(f"/items/{item['public_reference']}/claim",data={
                "csrf_token":self.token(),"claimant_name":f"Claimant {number}","contact":f"owner{number}@example.com",
                "identification":f"Student ID {number}","proof_description":"Ownership detail supplied"})
        self.login(password="NewSecure!2026")
        db=sqlite3.connect(self.app.config["DATABASE"]); ids=[row[0] for row in db.execute("SELECT id FROM claims ORDER BY id")]; db.close()
        self.client.post(f"/manage/claims/{ids[0]}",data={"csrf_token":self.token(),"action":"approve","staff_notes":"Verified"})
        self.client.post(f"/manage/claims/{ids[0]}",data={"csrf_token":self.token(),"action":"release","staff_notes":"Released"})
        db=sqlite3.connect(self.app.config["DATABASE"]); statuses=[row[0] for row in db.execute("SELECT status FROM claims ORDER BY id")]; db.close()
        self.assertEqual(statuses,["released","rejected"])
        response=self.client.post(f"/manage/claims/{ids[1]}",data={"csrf_token":self.token(),"action":"release","staff_notes":"Duplicate"})
        self.assertEqual(response.status_code,409)

    def test_item_status_cannot_repeat_invalid_transition(self):
        self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"lost","title":"School Lanyard",
            "public_description":"A green school lanyard missing near the main gate.",
            "private_details":"A unique charm is attached near the clip","event_date":"2026-09-17",
            "reporter_name":"Reporter","reporter_contact":"reporter@example.com"})
        self.login(); self.change_password()
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); item_id=db.execute("SELECT id FROM items").fetchone()[0]; db.close()
        first=self.client.post(f"/manage/items/{item_id}",data={"csrf_token":self.token(),"action":"approve"})
        second=self.client.post(f"/manage/items/{item_id}",data={"csrf_token":self.token(),"action":"approve"})
        self.assertEqual(first.status_code,302)
        self.assertEqual(second.status_code,409)

    def test_admin_can_directly_post_a_published_item(self):
        self.login(); self.change_password()
        self.assertEqual(self.client.get("/manage/items/new").status_code,200)
        response=self.client.post("/manage/items/new",data={
            "csrf_token":self.token(),"report_type":"found","title":"Official Office Umbrella",
            "public_description":"An official umbrella held by the Lost and Found Office.",
            "private_details":"A blue mark on the inside handle","event_date":"2026-09-17",
            "category_id":"","location_id":""})
        self.assertEqual(response.status_code,302)
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); db.row_factory=sqlite3.Row
        item=db.execute("SELECT * FROM items").fetchone()
        audit=db.execute("SELECT action FROM audit_logs WHERE entity_type='item' ORDER BY id DESC LIMIT 1").fetchone()
        db.close()
        self.assertEqual(item["status"],"published")
        self.assertEqual(item["reporter_name"],"System Administrator")
        self.assertEqual(audit["action"],"direct_post")
        self.assertIn(b"Official Office Umbrella",self.client.get("/items").data)

    def test_staff_cannot_directly_post(self):
        self.login("staff","StaffChange!2026")
        self.change_password()
        self.assertEqual(self.client.get("/manage/items/new").status_code,403)

    def test_claim_page_explains_three_steps(self):
        self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"Blue Folder",
            "public_description":"A blue folder found in the library reading area.",
            "private_details":"A handwritten note is inside the back pocket","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); reference=db.execute("SELECT public_reference FROM items").fetchone()[0]; db.close()
        self.login(); self.change_password()
        db=sqlite3.connect(self.app.config["DATABASE"]); item_id=db.execute("SELECT id FROM items").fetchone()[0]; db.close()
        self.client.post(f"/manage/items/{item_id}",data={"csrf_token":self.token(),"action":"approve"})
        page=self.client.get(f"/items/{reference}/claim")
        self.assertEqual(page.status_code,200)
        self.assertIn(b"Simple 3-step claim",page.data)
        self.assertIn(b"Step 1",page.data)
        self.assertIn(b"Step 2",page.data)
        self.assertIn(b"Step 3",page.data)

    def test_returned_item_does_not_offer_new_claim(self):
        self.client.post("/report",data={
            "csrf_token":self.token(),"report_type":"found","title":"Returned Backpack",
            "public_description":"A returned backpack record for testing the public status page.",
            "private_details":"A stitched mark on the inner pocket","event_date":"2026-09-17",
            "reporter_name":"Finder","reporter_contact":"finder@example.com"})
        import sqlite3
        db=sqlite3.connect(self.app.config["DATABASE"]); reference=db.execute("SELECT public_reference FROM items").fetchone()[0]; db.close()
        self.login(); self.change_password()
        db=sqlite3.connect(self.app.config["DATABASE"]); item_id=db.execute("SELECT id FROM items").fetchone()[0]; db.close()
        self.client.post(f"/manage/items/{item_id}",data={"csrf_token":self.token(),"action":"approve"})
        self.client.post("/logout",data={"csrf_token":self.token()})
        self.client.post(f"/items/{reference}/claim",data={
            "csrf_token":self.token(),"claimant_name":"Owner","contact":"owner@example.com",
            "identification":"Student ID 7","proof_description":"Private detail"})
        self.login(password="NewSecure!2026")
        db=sqlite3.connect(self.app.config["DATABASE"]); claim_id=db.execute("SELECT id FROM claims").fetchone()[0]; db.close()
        self.client.post(f"/manage/claims/{claim_id}",data={"csrf_token":self.token(),"action":"approve","staff_notes":"Matched"})
        self.client.post(f"/manage/claims/{claim_id}",data={"csrf_token":self.token(),"action":"release","staff_notes":"Released"})
        page=self.client.get(f"/items/{reference}")
        self.assertEqual(page.status_code,200)
        self.assertNotIn(b"See the claim steps",page.data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
