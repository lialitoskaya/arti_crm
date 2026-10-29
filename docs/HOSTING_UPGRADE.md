# Coherent hosting release upgrade

## Purpose and boundary

The reported import failure came from mixing historical `repository.py`/outbox
contracts with a modern task automation module. Replacing one file or inventing
missing constant aliases does not repair the different schemas and send semantics.
Deploy one coherent release, including its complete `app/`, static assets, entry
points and pinned requirements. Do not overlay arbitrary files from the old ZIPs.

The known historical schema comes from supplied **source code**, not a production
database inspection. The startup bridge supports that exact schema, a normal new
database, and the already-current schema. A different or partially mixed historical
schema must stop with a safe error; do not disable its validators to force startup.

## Data policy

- All old send rows are copied into immutable private audit with a count/hash
  manifest. Missing text is not guessed. No old row is eligible for automatic send.
- The existing operation API returns a review row with `uncertain` and the fixed
  explanation “Старая отправка требует ручной сверки; текст не сохранён”. A
  sufficiently proven existing confirmed message suppresses that transient row.
- Same-ID replay preserves the old operation; changed content and new IDs with an
  unresolved old hash return 409. This is a conservative duplicate guard, not proof
  of exactly-once delivery. Different rewritten text cannot be recognized by hash.
- Manual review/resolution tools are not included. Do not force a new ID, modify
  audit hashes, or enqueue placeholder text to work around a review block.
- Old task effects/state are archived without retroactive acquisition. Existing chat
  workflow status and manual metadata remain; valid dormant mappings are retained.
  Modern message-identity repair remains enabled and can update activity caches
  when it merges proven historical duplicates.
- Audit contains historical sensitive fields already present in the database. Keep
  DB/backups private, never web-served, exported to diagnostics, or attached to a
  support request. There is no API for raw audit data.

## Rollout (operator action; not performed by development tests)

1. Stop the old web process, cron/background ticks and marketplace workers. Prevent
   requests during replacement. SQLite locking is not a mixed-version deployment
   strategy.
2. Take a consistent backup of the complete DB (including WAL state through the
   SQLite backup mechanism or a fully stopped checkpointed database), current code,
   private attachments and configuration. Verify restoration in a restricted
   operator-owned environment. Never send secrets or a production dump to the agent.
3. Install one complete release and its `requirements.txt` in a fresh environment.
   Python 3.11+ is required by the existing dispatcher (`asyncio.timeout`).
   `a2wsgi==1.10.10` is included for the existing WSGI entry points; Uvicorn remains
   the ASGI server option. Keep deployment configuration/private data outside the
   release and use absolute writable storage paths.
4. For an empty user table, privately configure the one-time `BOOTSTRAP_ADMIN_*`
   settings described below before starting one maintenance instance. Both ASGI
   startup and hosted WSGI use the same `init_db()` and `ensure_initial_admin()`.
   The known legacy conversion uses one `BEGIN IMMEDIATE` transaction
   before normal initialization. Unsupported schema or conversion errors roll it
   back; do not ignore the error or run an old dispatcher against it.
5. Validate migration markers, preserved audit counts and the modern schema in the
   operator environment without printing raw rows. Verify login, read-only history,
   uncertain review rows and one **synthetic/mock** new command before enabling
   real traffic. No production send is needed for a release smoke check.
6. Resume only current-version processes. ASGI lifecycle starts the existing loops.
   The WSGI adapter does not promise ASGI lifespan/background-loop execution; hosting
   must continue to call the existing authenticated `/api/background/tick` job for
   deferred drain/sync. Do not add a second sender or share incompatible releases.

## Startup configuration and database selection

All entry points load configuration through `app.runtime_config` before importing
modules that read environment settings. Only the installation root's dotenv file
is considered, with `override=False`: values provided by the hosting panel or
process manager take precedence. The process working directory is not the source
of database identity.

`DATABASE_PATH` is made absolute relative to the project root. Without an explicit
value, the single default is `<project root>/crm.sqlite3`. The loader does not open,
create, move or copy a database. A fresh installation with no known existing DB may
initialize this path normally. An existing root DB is reused. If the former
`data/crm.sqlite3` exists and no path is specified, startup refuses to guess, even
if the root DB also exists. Select the intended existing file explicitly. An
explicit missing file is also rejected while either known default file exists,
to prevent a typo from silently creating a second DB. Existing explicitly selected
files are honored. Empty values, directories, SQLite memory/URI selectors and
unresolvable paths are rejected with a value-free configuration error.

Keep an absolute `DATABASE_PATH` configured for every deployment, including code
rollback: older releases had different defaults. This change neither alters schema
migrations nor relocates data. Do not copy a database over newer live data or deploy
individual modules from this release over an unrelated historical release.

WSGI startup failures return only a fixed, non-cacheable HTTP 500 message. Details
go to the `arti_crm.startup` logger (normally the process manager's stderr): exception
type and frame file/line/function, without raw exception arguments, source lines,
locals or configuration values. Restrict these process logs to operators, outside
the web document root. Fixed diagnostic hints identify bootstrap or database-path
configuration errors without exposing their values. No new `startup_error.log` is
written in the site root. Existing copies are not deleted automatically; the
operator must restrict public access and archive/remove old diagnostic files
according to the deployment's retention policy.

## First administrator on an empty database

An empty `users` table requires explicit `BOOTSTRAP_ADMIN_USERNAME` and
`BOOTSTRAP_ADMIN_PASSWORD`. The deployment owner supplies these values privately
through the hosting configuration; do not send a password to the agent, put it in
source code, commit it, or paste it into diagnostics. There are no default login
credentials and `CRM_AUTH_DISABLED` is not a bootstrap or recovery mechanism.

Choose a unique username of 2–120 printable characters without whitespace; the
former default `admin` is rejected. Use a strong, unique password of 12–500
characters; former default passwords are rejected. Optional
`BOOTSTRAP_ADMIN_DISPLAY_NAME` is limited to 160 characters and otherwise defaults
to the username. Missing, partial, or invalid settings stop an empty-database
startup with a fixed error naming the required settings, never their values.

Both `server.py` and `passenger_wsgi.py` bootstrap after schema initialization and
before constructing the WSGI adapter. They do not invoke the full ASGI startup,
cleanup, repairs, or background loops. The shared repository operation takes
`BEGIN IMMEDIATE`, checks the user table again inside that transaction, and inserts
only the first administrator. Concurrent startup workers cannot create additional
initial administrators, even with different configured usernames.

Once any user exists (including an inactive or non-admin user), bootstrap is a
no-op: settings are no longer required and cannot reset a password, rename,
reactivate, promote, or replace an existing account. After the owner verifies
password login, remove the one-time settings from the private deployment
configuration and restart. Forgotten credentials or an inactive existing account
require the normal owner-controlled account recovery process, not deleting users
or changing bootstrap settings.

This fix changes no schema and needs no data migration. If the application is
rolled back, the created user and password hash remain usable; older WSGI code
still cannot bootstrap a new empty user table. Do not delete existing users as a
rollback step.

## Recovery

Conversion failures roll back the bridge. Later `init_db()` stages are not one
global transaction: if a later stage fails after the bridge committed, retain the
audit/marker and fix forward. Repeated bridge startup validates and reuses them.
Before any new-version writes, restoring the **whole matching code + DB backup**
is an operator rollback option. After new writes, restoring that snapshot loses
those writes and requires a separate reconciliation decision. Never deploy just an
old module against the converted schema; do not drop audit tables or markers.

## Verification

`tests/test_legacy_hosting_upgrade.py` uses only `tmp_path` SQLite files, synthetic
records, code-derived DDL, blocked external networking and disabled dotenv. It
covers conversion/repeat init, rollback faults, invalid schema, ID reservation,
reference remapping, immutable audit, concurrent registration/conversion, HTTP
replay/polling, task lifecycle and real a2wsgi entry-point requests. It does not
claim a live hosting deployment, provider-send verification or support for every
unknown database variant.

`tests/test_auth_bootstrap_security.py` additionally exercises real WSGI password
login on an empty temporary database, invalid/missing bootstrap settings,
unchanged existing users and repeated startup, plus concurrent first-admin writes
on separate SQLite connections. Dotenv and external networking are disabled;
these tests never inspect or modify a deployed database or configuration.

`tests/test_runtime_startup.py` covers canonical path resolution independent of
working directory, panel-over-dotenv priority, repeat loading, ambiguous and invalid
paths, lazy DB initialization, ASGI configuration ordering, both WSGI entry points,
safe HTTP failures and value-free diagnostics. Dotenv is simulated, never read.
