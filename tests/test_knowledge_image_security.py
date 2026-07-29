from __future__ import annotations

import os
import shutil
import unittest
import uuid
from pathlib import Path
from unittest import mock

import httpx
from starlette.routing import Mount

import test_regression_foundation as foundation  # noqa: E402
from app.services import knowledge_images  # noqa: E402


db = foundation.db
main = foundation.main
repo = foundation.repo
_TEST_EVENT_LOOP = foundation._TEST_EVENT_LOOP

_PASSWORD = "knowledge-image-test-password-2026"


async def _login(client: httpx.AsyncClient, username: str) -> None:
    response = await client.post(
        "/api/auth/login",
        json={"username": username, "password": _PASSWORD},
        headers={"user-agent": "knowledge-image-security-test"},
    )
    if response.status_code != 200:
        raise AssertionError(f"test login failed: {response.status_code}")


async def _csrf_headers(client: httpx.AsyncClient) -> dict[str, str]:
    response = await client.get("/api/security/csrf")
    if response.status_code != 200:
        raise AssertionError(f"failed to obtain test CSRF token: {response.status_code}")
    return {main.CSRF_HEADER_NAME: response.json()["csrf_token"]}


def _run(coroutine):
    return _TEST_EVENT_LOOP.run_until_complete(coroutine)


class KnowledgeImageSecurityTests(unittest.TestCase):
    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        foundation._remove_test_runtime_files()
        self.test_root = (foundation._TEMP_ROOT / "knowledge-image-security").resolve()
        self.public_static_root = (self.test_root / "public-static").resolve()
        self.private_image_root = (self.test_root / "private-knowledge-images").resolve()
        shutil.rmtree(self.test_root, ignore_errors=True)
        self.public_static_root.mkdir(parents=True)
        db.init_db()
        self.admin = repo.create_user("knowledge-admin", _PASSWORD, "Admin", "admin")
        repo.create_user("knowledge-viewer", _PASSWORD, "Viewer", "viewer")
        self.static_dir_patch = mock.patch.object(main, "STATIC_DIR", self.public_static_root)
        self.image_dir_patch = mock.patch.object(main, "KNOWLEDGE_IMAGES_DIR", self.private_image_root)
        self.static_dir_patch.start()
        self.image_dir_patch.start()

    def tearDown(self) -> None:
        try:
            self.assertEqual([], foundation._NETWORK_ATTEMPTS, "a test attempted network access")
        finally:
            self.image_dir_patch.stop()
            self.static_dir_patch.stop()
            shutil.rmtree(self.test_root, ignore_errors=True)
            foundation._remove_test_runtime_files()

    def _create_article(self, image_reference: str | None, title: str = "Knowledge article") -> dict[str, object]:
        return repo.create_knowledge_article(
            category_id=None,
            title=title,
            content="Synthetic knowledge content",
            tags=None,
            image_url=image_reference,
            user_id=int(self.admin["id"]),
        )

    def _create_legacy_article(self, *, source_bytes: bytes | None = b"legacy-image") -> tuple[dict[str, object], str, Path]:
        filename = f"{uuid.uuid4().hex}.jpg"
        legacy_url = f"/static/uploads/knowledge/{filename}"
        legacy_path = self.public_static_root / "uploads" / "knowledge" / filename
        if source_bytes is not None:
            legacy_path.parent.mkdir(parents=True, exist_ok=True)
            legacy_path.write_bytes(source_bytes)
        return self._create_article(legacy_url, "Legacy image article"), legacy_url, legacy_path

    def _static_app(self):
        static_mount = next(
            route
            for route in main.app.routes
            if isinstance(route, Mount) and route.path == "/static"
        )
        return static_mount.app

    def test_article_image_endpoint_requires_auth_and_serves_legacy_and_private_references(self) -> None:
        legacy_article, legacy_reference, legacy_path = self._create_legacy_article()
        private_filename = f"{uuid.uuid4().hex}.png"
        private_reference = f"knowledge-private:{private_filename}"
        private_path = self.private_image_root / private_filename
        private_path.parent.mkdir(parents=True)
        private_path.write_bytes(b"private-image")
        private_article = self._create_article(private_reference, "Private image article")
        legacy_url = f"/api/knowledge/articles/{legacy_article['id']}/image"
        private_url = f"/api/knowledge/articles/{private_article['id']}/image"

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with (
                httpx.AsyncClient(transport=transport, base_url="https://testserver") as viewer,
                httpx.AsyncClient(transport=transport, base_url="https://testserver") as anonymous,
            ):
                await _login(viewer, "knowledge-viewer")
                legacy_image = await viewer.get(legacy_url)
                private_image = await viewer.get(private_url)
                legacy_api = await viewer.get(f"/api/knowledge/articles/{legacy_article['id']}")
                private_api = await viewer.get(f"/api/knowledge/articles/{private_article['id']}")
                listing = await viewer.get("/api/knowledge/articles")
                old_filename_route = await viewer.get(f"/api/knowledge/images/{private_filename}")
                anonymous_image = await anonymous.get(legacy_url)
            return legacy_image, private_image, legacy_api, private_api, listing, old_filename_route, anonymous_image

        legacy_image, private_image, legacy_api, private_api, listing, old_filename_route, anonymous_image = _run(exercise())

        self.assertEqual(200, legacy_image.status_code)
        self.assertEqual(b"legacy-image", legacy_image.content)
        self.assertEqual("image/jpeg", legacy_image.headers["content-type"])
        self.assertEqual(200, private_image.status_code)
        self.assertEqual(b"private-image", private_image.content)
        self.assertEqual("image/png", private_image.headers["content-type"])
        self.assertEqual(legacy_url, legacy_api.json()["image_url"])
        self.assertEqual(private_url, private_api.json()["image_url"])
        listed_urls = {item["id"]: item["image_url"] for item in listing.json()}
        self.assertEqual(legacy_url, listed_urls[legacy_article["id"]])
        self.assertEqual(private_url, listed_urls[private_article["id"]])
        self.assertNotIn(Path(legacy_reference).name, legacy_api.text)
        self.assertNotIn(private_filename, private_api.text)
        self.assertEqual(404, old_filename_route.status_code)
        self.assertEqual(401, anonymous_image.status_code)
        self.assertTrue(legacy_path.is_file())
        self.assertTrue(private_path.is_file())

    def test_unknown_missing_invalid_and_orphan_images_return_generic_404(self) -> None:
        missing_article, _, _ = self._create_legacy_article(source_bytes=None)
        invalid_article = self._create_article("/static/uploads/knowledge/../secret.jpg", "Invalid reference")
        orphan_filename = f"{uuid.uuid4().hex}.png"
        self.private_image_root.mkdir(parents=True)
        (self.private_image_root / orphan_filename).write_bytes(b"orphan-private-image")

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as viewer:
                await _login(viewer, "knowledge-viewer")
                missing = await viewer.get(f"/api/knowledge/articles/{missing_article['id']}/image")
                invalid = await viewer.get(f"/api/knowledge/articles/{invalid_article['id']}/image")
                unknown = await viewer.get("/api/knowledge/articles/999999/image")
                old_orphan_route = await viewer.get(f"/api/knowledge/images/{orphan_filename}")
            return missing, invalid, unknown, old_orphan_route

        responses = _run(exercise())
        for response in responses:
            self.assertEqual(404, response.status_code)
            self.assertNotIn(str(self.private_image_root), response.text)
            self.assertNotIn(str(self.public_static_root), response.text)

    def test_article_scoped_upload_is_private_and_delete_only_clears_database_reference(self) -> None:
        image_bytes = b"synthetic-private-upload"

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as admin:
                await _login(admin, "knowledge-admin")
                headers = await _csrf_headers(admin)
                created = await admin.post(
                    "/api/knowledge/articles",
                    json={"title": "Upload target", "content": "Created before image"},
                    headers=headers,
                )
                article_id = created.json()["id"]
                uploaded = await admin.post(
                    f"/api/knowledge/articles/{article_id}/image",
                    files={"file": ("example.png", image_bytes, "image/png")},
                    headers=headers,
                )
                internal_reference = repo.get_knowledge_article(int(article_id))["image_url"]
                image = await admin.get(f"/api/knowledge/articles/{article_id}/image")
                removed = await admin.delete(
                    f"/api/knowledge/articles/{article_id}/image",
                    headers=headers,
                )
                after_remove = await admin.get(f"/api/knowledge/articles/{article_id}/image")
            return created, uploaded, internal_reference, image, removed, after_remove

        created, uploaded, internal_reference, image, removed, after_remove = _run(exercise())
        article_id = int(created.json()["id"])
        raw_article = repo.get_knowledge_article(article_id)
        self.assertEqual(200, uploaded.status_code)
        self.assertEqual(f"/api/knowledge/articles/{article_id}/image", uploaded.json()["image_url"])
        self.assertNotIn("filename", uploaded.json())
        self.assertRegex(
            str(internal_reference),
            r"^/api/knowledge/images/[0-9a-f]{32}\.png$",
        )
        self.assertNotIn(str(internal_reference), uploaded.text)
        self.assertEqual(200, image.status_code)
        self.assertEqual(image_bytes, image.content)
        self.assertEqual(200, removed.status_code)
        self.assertIsNone(removed.json()["image_url"])
        self.assertEqual(404, after_remove.status_code)
        self.assertIsNone(raw_article["image_url"])
        private_files = list(self.private_image_root.iterdir())
        self.assertEqual(1, len(private_files))
        self.assertEqual(image_bytes, private_files[0].read_bytes())
        self.assertFalse((self.public_static_root / "uploads" / "knowledge").exists())

    def test_viewer_cannot_upload_or_delete_article_images(self) -> None:
        upload_target = self._create_article(None, "Viewer upload target")
        private_filename = f"{uuid.uuid4().hex}.png"
        private_reference = f"/api/knowledge/images/{private_filename}"
        private_path = self.private_image_root / private_filename
        private_path.parent.mkdir(parents=True)
        private_path.write_bytes(b"existing-private-image")
        delete_target = self._create_article(private_reference, "Viewer delete target")

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as viewer:
                await _login(viewer, "knowledge-viewer")
                headers = await _csrf_headers(viewer)
                upload = await viewer.post(
                    f"/api/knowledge/articles/{upload_target['id']}/image",
                    files={"file": ("forbidden.png", b"must-not-be-written", "image/png")},
                    headers=headers,
                )
                image_read = await viewer.get(f"/api/knowledge/articles/{delete_target['id']}/image")
                delete = await viewer.delete(
                    f"/api/knowledge/articles/{delete_target['id']}/image",
                    headers=headers,
                )
            return upload, image_read, delete

        upload, image_read, delete = _run(exercise())
        self.assertEqual(403, upload.status_code)
        self.assertIsNone(repo.get_knowledge_article(int(upload_target["id"]))["image_url"])
        self.assertEqual([private_path], list(self.private_image_root.iterdir()))
        self.assertEqual(200, image_read.status_code)
        self.assertEqual(b"existing-private-image", image_read.content)
        self.assertEqual(403, delete.status_code)
        self.assertEqual(
            private_reference,
            repo.get_knowledge_article(int(delete_target["id"]))["image_url"],
        )
        self.assertEqual(b"existing-private-image", private_path.read_bytes())

    def test_knowledge_ui_has_no_raw_url_and_uses_shared_admin_only_controls(self) -> None:
        static_root = Path(main.__file__).resolve().parent / "static"
        index_html = (static_root / "index.html").read_text(encoding="utf-8")
        app_js = (static_root / "app.js").read_text(encoding="utf-8")

        self.assertNotIn("knowledgeArticleImageUrl", index_html)
        self.assertNotIn("knowledgeArticleImageUrl", app_js)
        self.assertIn("document.querySelectorAll('.admin-only')", app_js)
        for element_id in (
            "knowledgeNewArticleBtn",
            "knowledgeCategoryForm",
            "knowledgeEditArticleBtn",
            "knowledgeArticleForm",
            "knowledgeModalEditBtn",
        ):
            self.assertRegex(
                index_html,
                rf'id="{element_id}"[^>]*class="[^"]*admin-only[^"]*hidden[^"]*"',
            )

    def test_text_update_preserves_internal_reference_and_raw_image_fields_are_rejected(self) -> None:
        legacy_article, legacy_reference, legacy_path = self._create_legacy_article()
        article_id = int(legacy_article["id"])
        derived_url = f"/api/knowledge/articles/{article_id}/image"

        async def exercise():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as admin:
                await _login(admin, "knowledge-admin")
                headers = await _csrf_headers(admin)
                edited = await admin.patch(
                    f"/api/knowledge/articles/{article_id}",
                    json={"title": "Edited text only", "content": "No image mutation"},
                    headers=headers,
                )
                stale_patch = await admin.patch(
                    f"/api/knowledge/articles/{article_id}",
                    json={"title": "Must not apply", "image_url": "/api/knowledge/images/stale.png"},
                    headers=headers,
                )
                stale_clear = await admin.patch(
                    f"/api/knowledge/articles/{article_id}",
                    json={"clear_image": True},
                    headers=headers,
                )
                raw_create = await admin.post(
                    "/api/knowledge/articles",
                    json={"title": "Must reject raw image", "image_url": legacy_reference},
                    headers=headers,
                )
            return edited, stale_patch, stale_clear, raw_create

        edited, stale_patch, stale_clear, raw_create = _run(exercise())
        stored = repo.get_knowledge_article(article_id)
        self.assertEqual(200, edited.status_code)
        self.assertEqual(derived_url, edited.json()["image_url"])
        self.assertEqual(422, stale_patch.status_code)
        self.assertEqual(422, stale_clear.status_code)
        self.assertEqual(422, raw_create.status_code)
        self.assertEqual("Edited text only", stored["title"])
        self.assertEqual(legacy_reference, stored["image_url"])
        self.assertTrue(legacy_path.is_file())

    def test_startup_does_not_rewrite_database_or_move_legacy_file(self) -> None:
        article, legacy_reference, legacy_path = self._create_legacy_article()
        article_id = int(article["id"])
        with db.get_connection() as connection:
            connection.execute(
                "UPDATE knowledge_articles SET updated_at=? WHERE id=?",
                ("2025-01-02 03:04:05", article_id),
            )

        async def exercise_startup() -> None:
            with mock.patch.object(main, "_ensure_wb_events_auto_plan_from_env"):
                await main.on_startup()
                await main.on_shutdown()

        _run(exercise_startup())
        stored = repo.get_knowledge_article(article_id)
        self.assertEqual(legacy_reference, stored["image_url"])
        self.assertEqual("2025-01-02 03:04:05", stored["updated_at"])
        self.assertEqual(b"legacy-image", legacy_path.read_bytes())
        self.assertFalse(self.private_image_root.exists())
        self.assertFalse(hasattr(main, "_migrate_legacy_knowledge_images"))

    def test_legacy_static_namespace_is_blocked_for_normalized_path_variants(self) -> None:
        filename = f"{uuid.uuid4().hex}.png"
        legacy_path = self.public_static_root / "uploads" / "knowledge" / filename
        legacy_path.parent.mkdir(parents=True)
        legacy_path.write_bytes(b"must-not-be-public")
        (self.public_static_root / "allowed.txt").write_bytes(b"public-static-ok")
        static_app = self._static_app()
        self.assertIsInstance(static_app, main.KnowledgeSafeStaticFiles)
        original_directory = static_app.directory
        original_directories = static_app.all_directories
        static_app.directory = str(self.public_static_root)
        static_app.all_directories = [str(self.public_static_root)]
        try:
            async def exercise():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                    allowed = await client.get("/static/allowed.txt")
                    blocked = [
                        await client.get(f"/static/uploads/knowledge/{filename}"),
                        await client.get(f"/static/uploads/./knowledge/{filename}"),
                        await client.get(f"/static//uploads///knowledge//{filename}"),
                        await client.get(f"/static/other/%2e%2e/uploads/knowledge/{filename}"),
                        await client.get(f"/static/uploads/x/%252e%252e/knowledge/{filename}"),
                        await client.get(f"/static/uploads%5Cknowledge%5C{filename}"),
                        await client.get(f"/static/uploads%255Cknowledge%255C{filename}"),
                    ]
                return allowed, blocked

            allowed_response, blocked_responses = _run(exercise())
        finally:
            static_app.directory = original_directory
            static_app.all_directories = original_directories

        self.assertEqual(200, allowed_response.status_code)
        self.assertEqual(b"public-static-ok", allowed_response.content)
        for response in blocked_responses:
            self.assertEqual(404, response.status_code)
            self.assertNotIn("must-not-be-public", response.text)

    def test_lexical_and_resolved_containment_are_windows_safe(self) -> None:
        legacy_root = self.public_static_root / "uploads" / "knowledge"
        sibling = self.public_static_root / "uploads" / "knowledge-other" / "image.png"
        self.assertTrue(knowledge_images.lexical_path_is_within(legacy_root / "image.png", legacy_root))
        self.assertTrue(knowledge_images.lexical_path_is_within(legacy_root / "nested" / ".." / "image.png", legacy_root))
        self.assertFalse(knowledge_images.lexical_path_is_within(sibling, legacy_root))
        self.assertFalse(knowledge_images.resolved_path_is_within(sibling, legacy_root))

    def test_static_boundary_blocks_symlinks_both_into_and_out_of_legacy_tree(self) -> None:
        legacy_root = self.public_static_root / "uploads" / "knowledge"
        legacy_root.mkdir(parents=True)
        legacy_target = legacy_root / "legacy-target.txt"
        outside_target = self.public_static_root / "outside-target.txt"
        legacy_target.write_bytes(b"legacy-target-secret")
        outside_target.write_bytes(b"outside-target-public")
        inside_link = legacy_root / "link-out.txt"
        outside_link = self.public_static_root / "link-in.txt"
        try:
            os.symlink(outside_target, inside_link, target_is_directory=False)
            os.symlink(legacy_target, outside_link, target_is_directory=False)
        except (NotImplementedError, OSError) as exc:
            self.skipTest(f"deployment reparse/symlink check unavailable: {exc.__class__.__name__}")

        static_app = self._static_app()
        original_directory = static_app.directory
        original_directories = static_app.all_directories
        static_app.directory = str(self.public_static_root)
        static_app.all_directories = [str(self.public_static_root)]
        try:
            async def exercise():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as client:
                    normal = await client.get("/static/outside-target.txt")
                    lexical_legacy = await client.get("/static/uploads/knowledge/link-out.txt")
                    resolved_legacy = await client.get("/static/link-in.txt")
                return normal, lexical_legacy, resolved_legacy

            normal, lexical_legacy, resolved_legacy = _run(exercise())
        finally:
            static_app.directory = original_directory
            static_app.all_directories = original_directories

        self.assertEqual(200, normal.status_code)
        self.assertEqual(404, lexical_legacy.status_code)
        self.assertEqual(404, resolved_legacy.status_code)


if __name__ == "__main__":
    unittest.main()
