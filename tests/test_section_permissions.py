from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from unittest import mock

import test_regression_foundation as foundation
import httpx

from app.permissions import SECTIONS, effective_permissions, route_allowed
from app.schemas import ChatCreate, TaskCreate

db, repo, main = foundation.db, foundation.repo, foundation.main


class SectionPermissionsTests(unittest.TestCase):
    def setUp(self):
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        db.init_db()
        main.app.state.security_rate_limits = {}
        self.admin = repo.create_user("permission-admin", "synthetic-password", role="admin")
        self.staff = repo.create_user("permission-staff", "synthetic-password", permissions={})

    def tearDown(self):
        self.assertEqual([], foundation._NETWORK_ATTEMPTS)
        foundation._remove_test_runtime_files()

    def run_async(self, coroutine):
        return foundation._TEST_EVENT_LOOP.run_until_complete(coroutine)

    @asynccontextmanager
    async def client(self, user):
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="https://testserver")
        client.cookies.set(main.AUTH_COOKIE_NAME, repo.create_session(user["id"]))
        response = await client.get("/api/security/csrf")
        self.assertEqual(200, response.status_code)
        client.headers[main.CSRF_HEADER_NAME] = response.json()["csrf_token"]
        try:
            yield client
        finally:
            await client.aclose()

    def test_existing_users_preserve_read_defaults_and_migration_is_idempotent(self):
        legacy = repo.create_user("legacy-user", "synthetic-password")
        # Recreate the previous schema with an existing account and active session.
        token = repo.create_session(legacy["id"])
        with db.get_connection() as conn:
            conn.execute("ALTER TABLE users DROP COLUMN section_permissions")
        snapshot = foundation._REAL_SQLITE_CONNECT(":memory:")
        self.addCleanup(snapshot.close)
        with db.get_connection() as conn:
            conn.backup(snapshot)
        db.init_db()
        legacy = repo.get_user_by_session(token)
        self.assertEqual("read", legacy["permissions"]["chats"])
        self.assertEqual("none", legacy["permissions"]["techSettings"])
        repo.update_user(legacy["id"], permissions={"tasks": "write"})
        db.init_db()
        db.init_db()
        self.assertEqual("write", repo.get_user_by_id(legacy["id"])["permissions"]["tasks"])
        self.assertEqual(dict.fromkeys(SECTIONS, "none"), effective_permissions("manager", "bad json"))
        with db.get_connection() as conn:
            snapshot.backup(conn)
            self.assertNotIn("section_permissions", {row["name"] for row in conn.execute("PRAGMA table_info(users)")})
        db.init_db()
        self.assertEqual("read", repo.get_user_by_session(token)["permissions"]["tasks"])

    def test_permission_matrix_denies_direct_routes_and_cross_section_requests(self):
        cases = {"chats": "/api/chats", "tasks": "/api/tasks", "reviews": "/api/reviews",
                 "questions": "/api/questions", "knowledge": "/api/knowledge/categories",
                 "analytics": "/api/analytics/chats", "supplyPlanning": "/api/supply-planning/status",
                 "ozonFbo": "/api/ozon-fbo/runs", "techSettings": "/api/chat-settings/statuses"}
        for section, path in cases.items():
            for level in ("none", "read", "write"):
                user = {"role": "manager", "permissions": {section: level}}
                self.assertEqual(level != "none", route_allowed(user, "GET", path), (section, level))
                if section != "analytics":
                    self.assertEqual(level == "write", route_allowed(user, "POST", path), (section, level))
        full = {"role": "manager", "permissions": dict.fromkeys(SECTIONS, "write")}
        for path in ("/api/users", "/api/admin/yandex-oauth-links", "/api/debug/local",
                     "/api/sync/operator", "/api/assets/image", "/api/unknown",
                     "/api/ozon-fbo/settings", "/api/ozon-fbo/runs/1/api-log"):
            self.assertFalse(route_allowed(full, "GET", path), path)
        self.assertFalse(route_allowed({"role": "manager", "permissions": {"analytics": "read"}},
                                       "GET", "/api/analytics/chats/drilldown"))
        self.assertFalse(route_allowed({"role": "manager", "permissions": {"reviews": "write"}},
                                       "POST", "/api/reviews/1/start-chat"))

    def test_live_session_observes_admin_changes_and_cannot_elevate_itself(self):
        async def exercise():
            async with self.client(self.staff) as staff, self.client(self.admin) as admin:
                self.assertEqual(403, (await staff.get("/api/chats")).status_code)
                self.assertEqual(403, (await staff.patch(f'/api/users/{self.staff["id"]}', json={"role": "admin"})).status_code)
                await staff.patch('/api/auth/profile', json={"permissions": {"chats": "write"}})
                self.assertEqual(403, (await staff.get("/api/chats")).status_code)
                changed = await admin.patch(f'/api/users/{self.staff["id"]}', json={"permissions": {"chats": "read"}})
                self.assertEqual(200, changed.status_code, changed.text)
                self.assertEqual(200, (await staff.get("/api/chats")).status_code)
                self.assertEqual(403, (await staff.patch("/api/chats/1", json={"customer_name": "test"})).status_code)
                self.assertEqual(403, (await staff.get("/api/tasks")).status_code)
                self.assertEqual(422, (await admin.patch(f'/api/users/{self.staff["id"]}', json={"permissions": {"users": "write"}})).status_code)
                await admin.patch(f'/api/users/{self.staff["id"]}', json={"permissions": {}})
                self.assertEqual(403, (await staff.get("/api/chats")).status_code)
                self.assertEqual(200, (await staff.get("/api/auth/me")).status_code)
        self.run_async(exercise())

    def test_password_and_oauth_return_same_current_permissions(self):
        repo.update_user(self.staff["id"], permissions={"tasks": "write"})
        password_user, _ = repo.authenticate_user_and_create_session("permission-staff", "synthetic-password")
        oauth_user, _ = repo.create_session_for_yandex_identity("synthetic-provider-id", bootstrap_username="permission-staff")
        self.assertEqual(password_user["permissions"], oauth_user["permissions"])
        self.assertEqual("write", password_user["permissions"]["tasks"])
        for user in (password_user, oauth_user, repo.get_user_by_id(self.staff["id"])):
            self.assertNotIn("section_permissions", user)
            self.assertNotIn("password_hash", user)

    def test_linked_tasks_and_embedded_tasks_do_not_bypass_sections(self):
        chat = {"id": repo.upsert_chat(ChatCreate(marketplace="ozon", external_chat_id="permission-chat"))}
        task_id = repo.create_task(TaskCreate(chat_id=chat["id"], title="private linked task"))
        standalone_id = repo.create_standalone_task(title="standalone visible task")
        repo.update_user(self.staff["id"], permissions={"tasks": "write"})
        async def exercise():
            async with self.client(self.staff) as staff:
                tasks = await staff.get("/api/tasks")
                self.assertEqual(200, tasks.status_code, tasks.text)
                self.assertEqual([standalone_id], [t["id"] for t in tasks.json()])
                self.assertEqual(403, (await staff.patch(f"/api/tasks/{task_id}", json={"title": "changed"})).status_code)
                self.assertEqual(403, (await staff.delete(f"/api/tasks/{task_id}")).status_code)
                self.assertEqual(200, (await staff.patch(f"/api/tasks/{standalone_id}", json={"title": "changed"})).status_code)
                repo.update_user(self.staff["id"], permissions={"chats": "read"})
                response = await staff.get(f'/api/chats/{chat["id"]}')
                self.assertEqual(200, response.status_code)
                self.assertEqual([], response.json()["tasks"])
        self.run_async(exercise())

    def test_chat_operations_notes_and_attachment_scope(self):
        chat_id = repo.upsert_chat(ChatCreate(marketplace="ozon", external_chat_id="permitted-chat"))
        repo.create_task(TaskCreate(chat_id=chat_id, title="another section"))
        repo.update_user(self.staff["id"], permissions={"chats": "write"})
        base = f"/api/chats/{chat_id}"
        image_url = "https://example.invalid/file.png"
        async def exercise():
            async with self.client(self.staff) as staff:
                self.assertEqual(200, (await staff.get(base + "/message-send-operations")).status_code)
                self.assertEqual(404, (await staff.get(base + "/message-send-operations/missing")).status_code)
                note = await staff.post(base + "/notes", json={"text": image_url})
                self.assertEqual(200, note.status_code, note.text)
                self.assertEqual([], note.json()["chat"]["tasks"])
                item = base + f'/notes/{note.json()["message_id"]}'
                params = {"section": "chats", "entity_id": chat_id, "url": image_url}
                self.assertEqual(404, (await staff.get("/api/assets/entity-image", params=params)).status_code)
                for response in (await staff.patch(item, json={"text": "updated"}), await staff.delete(item)):
                    self.assertEqual(200, response.status_code, response.text)
                    self.assertEqual([], response.json()["chat"]["tasks"])
                repo.add_message(chat_id=chat_id, direction="inbound", text=image_url, author="buyer",
                                 raw={"text": image_url})
                self.assertFalse(repo.entity_contains_image_url("chats", chat_id, image_url))
                repo.add_message(chat_id=chat_id, direction="inbound", text="photo", author="buyer",
                                 raw={"attachments": [{"url": image_url}]})
                self.assertTrue(repo.entity_contains_image_url("chats", chat_id, image_url))
                repo.update_user(self.staff["id"], permissions={})
                self.assertEqual(403, (await staff.get("/api/assets/entity-image", params=params)).status_code)
        self.run_async(exercise())

    def test_notifications_filter_before_limit_and_push_rechecks_revocation(self):
        repo.update_user(self.staff["id"], permissions={"tasks": "read", "questions": "read"})
        allowed = repo.create_notification(user_id=self.staff["id"], type="new_task", title="visible", entity_type="task")
        denied = repo.create_notification(user_id=self.staff["id"], type="new_question", title="hidden after revoke", entity_type="question")
        repo.update_user(self.staff["id"], permissions={"tasks": "read"})
        result = repo.list_notifications(self.staff["id"], limit=1)
        self.assertEqual([allowed], [n["id"] for n in result["items"]])
        self.assertEqual(1, result["unread_count"])
        self.assertFalse(repo.mark_notification_read(denied, self.staff["id"]))
        self.assertEqual(1, repo.mark_all_notifications_read(self.staff["id"]))
        repo.update_user(self.staff["id"], permissions={})
        with mock.patch.object(main, "_web_push_configured", return_value=True), mock.patch.object(main, "_send_web_push_to_user", new_callable=mock.AsyncMock) as send:
            self.run_async(main._drain_push_outbox_once())
            send.assert_not_called()

    def test_admin_cannot_remove_last_admin_and_csrf_still_required(self):
        async def exercise():
            async with self.client(self.admin) as admin:
                response = await admin.patch(f'/api/users/{self.admin["id"]}', json={"role": "manager"})
                self.assertEqual(400, response.status_code)
                admin.headers.pop(main.CSRF_HEADER_NAME)
                response = await admin.patch(f'/api/users/{self.staff["id"]}', json={"permissions": {"chats": "write"}})
                self.assertEqual(403, response.status_code)
        self.run_async(exercise())


if __name__ == "__main__":
    unittest.main()
