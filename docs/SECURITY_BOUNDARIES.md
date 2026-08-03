# Security boundaries

## Yandex OAuth login

Yandex OAuth is an optional login path for existing CRM users. The password endpoint
does not load or validate OAuth configuration and remains available when OAuth is
disabled, incomplete, malformed, or unavailable. OAuth configuration is read lazily;
an invalid `YANDEX_OAUTH_USER_MAP` disables only the Yandex status/start/callback flow
and does not fail application startup.

The authorization-code flow uses a random state value and PKCE S256. State and the
PKCE verifier are held only in a signed, short-lived, `HttpOnly`, `Secure`,
`SameSite=Lax` cookie scoped to `/api/auth/yandex`; the browser does not store them in
localStorage or sessionStorage. The provider access token is used only for the
immediate profile request and is not persisted, returned, or logged.

`YANDEX_OAUTH_USER_MAP` is used only to bootstrap a provider identity that has no
stored link yet, in id, login, then email priority. Login and email matching is
trimmed and case-insensitive. The first successful callback stores the immutable
Yandex user ID against `users.id` in `yandex_oauth_links`; subsequent callbacks use
that numeric CRM identity and never rebind it from a changed or reused username.
Link creation and CRM session creation share one `BEGIN IMMEDIATE` transaction.
OAuth never creates CRM users and creates a session only while the linked user still
exists and is active.

OAuth failures redirect with one short allowlisted code: `cancelled`,
`account_not_allowed`, `account_inactive`, `flow_expired`, `provider_unavailable`,
`oauth_rate_limited`, or `failed`. Start/callback rate-limit rejection also returns
to the login screen with `oauth_rate_limited`, rather than exposing an API error
page. The frontend maps these codes to safe Russian messages. Provider responses,
access tokens, profile email/login, filesystem or SQL details, and raw exception
messages are never included in the redirect.

Callback diagnostics emit only one fixed failure stage: `token_exchange`,
`profile_request`, `profile_validation`, `user_mapping`, `database_link`, or
`session_creation`. Logs never include the authorization code, provider token or
payload, OAuth client secret, cookies, profile email/login, or exception text.

## User deactivation and sessions

User management remains admin-only. An active-to-inactive user transition and
revocation of every still-active session for that user are committed in one SQLite
transaction. A database error rolls both changes back. Reactivation also revokes any
non-revoked legacy sessions for that user in the same transaction and never clears
`sessions.revoked_at`, so previously issued tokens cannot become valid again.
Repeated deactivation is safe and does not affect other users' sessions.

## Knowledge article images

Knowledge articles and their images require a normal authenticated CRM session.
Images are read only through `GET /api/knowledge/articles/{article_id}/image`, with
the same viewer-and-admin permission semantics as article reads. The endpoint first
loads the article and uses its server-stored image reference; anonymous requests
return `401`, while unknown articles, invalid references, and missing files return a
generic `404` without filesystem details.

Article API responses never expose a legacy static URL, private storage reference, or
private basename. When an article has an internal image reference, serialization
derives `/api/knowledge/articles/{article_id}/image`. Text create/update DTOs reject
raw `image_url` and `clear_image` fields. Text updates do not write `image_url`, so a
stale client cannot restore or replace an image reference.

Knowledge mutation endpoints require the admin role. The shared frontend
`admin-only hidden` mechanism hides article creation/editing, image upload/removal,
and category management controls from viewers, but backend RBAC remains the source
of truth. Viewers retain article and image read access.

New files are attached with
`POST /api/knowledge/articles/{article_id}/image`. The server chooses the filename,
writes the file in `CRM_KNOWLEDGE_IMAGES_DIR`, then stores an internal reference in
the exact rollback-compatible form
`/api/knowledge/images/<uuid>.<extension>`. Article serialization still exposes only
the article-id endpoint. This directory defaults to `knowledge_images/`
outside `app/static/` and is never mounted as static content. The corresponding
`DELETE` clears only the database reference; it intentionally retains the physical
file to favor recovery over data loss. Orphan cleanup is deferred.

The original filename and multipart `Content-Type` are metadata, not proof that an
upload is an image. Before creating the final private file or changing the article,
the endpoint reads the upload in 1 MiB chunks, rejects empty content and content over
8 MiB, then uses Pillow for two decoder passes: `Image.open(...).verify()`, followed
by reopen and full `load()`. Only decoder-confirmed JPEG, PNG, WebP, and GIF are
accepted. The decoder format must strictly match both the original extension and
MIME type: JPEG uses `.jpg` or `.jpeg` with `image/jpeg`; PNG uses `.png` with
`image/png`; WebP uses `.webp` with `image/webp`; GIF uses `.gif` with `image/gif`.
The persisted UUID filename uses the canonical decoder-derived extension (`.jpg` for
JPEG). Empty, unsupported, corrupt, truncated, and metadata-mismatched uploads return
a generic `400`; oversize uploads return `413`. Validation failures create no private
file and do not change the existing article reference.

Existing database references under `/static/uploads/knowledge/` remain unchanged.
The article-id endpoint strictly resolves an exact legacy reference beneath
`app/static/uploads/knowledge` with lexical and resolved containment. Existing
private references resolve beneath the configured private root. The server chooses a
safe image content type from the validated extension and returns `nosniff` and
private/no-store response headers.

This slice has not been deployed with the temporary `knowledge-private:` write
format. New writes deliberately retain the `/api/knowledge/images/<uuid>` internal
format, so rolling back to the previous application keeps newly attached images
readable. The current resolver accepts both formats for compatibility, but performs
no database conversion and does not change existing `updated_at` values.

There is no startup or request-time physical legacy migration in this slice. Startup
does not rewrite article rows, change `updated_at`, copy files, or delete legacy
sources. Old files are intentionally retained. A physical migration, deduplication,
and orphan lifecycle require a separate rollout and recovery plan.

The mounted FastAPI static application blocks the complete legacy namespace. It
normalizes repeated and encoded separators and dot segments, then refuses a lookup
when its lexical path is inside `app/static/uploads/knowledge` or its resolved target
is inside that directory. Windows-safe `commonpath` containment is used instead of
string-prefix checks. Symlink/reparse aliases into the legacy tree and links located
inside the tree are denied; unrelated static assets remain available.

FastAPI cannot enforce this boundary if a reverse proxy, CDN, or web server serves
`app/static` before the ASGI application. Every deployment must explicitly deny or
exclude `app/static/uploads/knowledge` from its static alias/root. Release checks
must cover direct, encoded, dot-segment, mixed-separator, and reparse/junction paths
through the public proxy.

This decoder boundary applies only to knowledge article images. Chat uploads retain
their existing storage, routes, validation, and RBAC behavior. Custom dimension,
pixel-count, frame-count, animation, and decompression-bomb policies remain future
work; Pillow's built-in warning/error behavior is not disabled. Decoder validation
without re-encoding does not guarantee removal of all trailing bytes or polyglot
content. Re-encoding, metadata stripping, aggregate quotas, physical legacy
migration, and generalized orphan cleanup are intentionally outside this change.

Regression coverage is in `tests/test_knowledge_image_security.py` and uses only a
temporary SQLite database and synthetic files under the test temporary directory.
