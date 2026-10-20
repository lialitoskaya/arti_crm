-- Static DDL extracted from the user-supplied 2026-10-13 db.py code archive.
-- Synthetic test schema only; no hosting data or executable startup code.
CREATE TABLE IF NOT EXISTS chats (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL,
                external_chat_id TEXT NOT NULL,
                customer_name TEXT,
                customer_public_id TEXT,
                order_id TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                assigned_to TEXT,
                assigned_user_id INTEGER,
                last_message_at TEXT,
                last_message_preview TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(marketplace, external_chat_id)
            );

CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                external_message_id TEXT,
                direction TEXT NOT NULL CHECK(direction IN ('inbound', 'outbound', 'internal')),
                author TEXT,
                text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                raw_json TEXT NOT NULL DEFAULT '{}',
                is_crm_sent INTEGER NOT NULL DEFAULT 0 CHECK(is_crm_sent IN (0, 1)),
                crm_author_user_id INTEGER,
                crm_author_label TEXT,
                client_operation_id TEXT,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(crm_author_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

CREATE TABLE IF NOT EXISTS task_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                comment_label TEXT NOT NULL DEFAULT 'Комментарий',
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                task_type_id INTEGER,
                title TEXT NOT NULL,
                description TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                assignee TEXT,
                assigned_user_id INTEGER,
                due_at TEXT,
                completed_at TEXT,
                archived_at TEXT,
                chat_status_effect_key TEXT,
                chat_status_effect_applied_at TEXT,
                chat_status_effect_cycle TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(task_type_id) REFERENCES task_types(id) ON DELETE SET NULL
            );

CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                comment TEXT NOT NULL,
                author TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                body TEXT,
                chat_id INTEGER,
                task_id INTEGER,
                entity_type TEXT,
                entity_id TEXT,
                dedupe_key TEXT,
                is_read INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                read_at TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE,
                FOREIGN KEY(task_id) REFERENCES tasks(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS push_subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                endpoint TEXT NOT NULL UNIQUE,
                subscription_json TEXT NOT NULL,
                user_agent TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                failure_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_success_at TEXT,
                last_error_at TEXT,
                last_error TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS push_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                notification_id INTEGER,
                user_id INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT,
                sent_at TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                role TEXT NOT NULL DEFAULT 'manager',
                password_hash TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_token TEXT NOT NULL UNIQUE,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                expires_at TEXT NOT NULL,
                revoked_at TEXT,
                user_agent TEXT,
                ip TEXT,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS chat_user_states (
                user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL,
                last_read_message_id INTEGER,
                last_read_at TEXT,
                is_marked_unread INTEGER NOT NULL DEFAULT 0 CHECK(is_marked_unread IN (0, 1)),
                is_pinned INTEGER NOT NULL DEFAULT 0 CHECK(is_pinned IN (0, 1)),
                pinned_at TEXT,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(user_id, chat_id),
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY(chat_id) REFERENCES chats(id) ON DELETE CASCADE
            );

CREATE TABLE IF NOT EXISTS yandex_oauth_links (
                yandex_user_id TEXT PRIMARY KEY,
                crm_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(crm_user_id) REFERENCES users(id) ON DELETE RESTRICT
            );

CREATE INDEX IF NOT EXISTS idx_yandex_oauth_links_crm_user_id
            ON yandex_oauth_links(crm_user_id);

CREATE TABLE IF NOT EXISTS yandex_oauth_managed_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                identifier_type TEXT NOT NULL CHECK(identifier_type IN ('login', 'email')),
                display_identifier TEXT NOT NULL,
                normalized_identifier TEXT NOT NULL,
                crm_user_id INTEGER NOT NULL,
                yandex_user_id TEXT UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK(is_active IN (0, 1)),
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(identifier_type, normalized_identifier),
                FOREIGN KEY(crm_user_id) REFERENCES users(id) ON DELETE RESTRICT
            );

CREATE INDEX IF NOT EXISTS idx_yandex_oauth_managed_links_crm_user_id
            ON yandex_oauth_managed_links(crm_user_id);

CREATE TABLE IF NOT EXISTS webhook_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                event_type TEXT,
                external_id TEXT,
                payload_json TEXT NOT NULL,
                received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                marketplace TEXT NOT NULL DEFAULT 'ozon',
                external_review_id TEXT NOT NULL,
                sku TEXT,
                product_name TEXT,
                rating INTEGER,
                status TEXT,
                author_name TEXT,
                text TEXT,
                published_at TEXT,
                comments_amount INTEGER DEFAULT 0,
                photos_amount INTEGER DEFAULT 0,
                videos_amount INTEGER DEFAULT 0,
                reply_text TEXT,
                reply_created_at TEXT,
                posting_number TEXT,
                linked_chat_id INTEGER,
                media_json TEXT NOT NULL DEFAULT '[]',
                comments_json TEXT NOT NULL DEFAULT '[]',
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(marketplace, external_review_id),
                FOREIGN KEY(linked_chat_id) REFERENCES chats(id) ON DELETE SET NULL
            );

CREATE TABLE IF NOT EXISTS ozon_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                external_question_id TEXT NOT NULL UNIQUE,
                sku TEXT,
                product_name TEXT,
                product_url TEXT,
                status TEXT,
                author_name TEXT,
                text TEXT,
                published_at TEXT,
                answer_text TEXT,
                answer_created_at TEXT,
                raw_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS reply_templates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_by_user_id INTEGER,
                updated_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

CREATE TABLE IF NOT EXISTS knowledge_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS knowledge_articles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category_id INTEGER,
                title TEXT NOT NULL,
                content TEXT NOT NULL DEFAULT '',
                tags TEXT,
                image_url TEXT,
                is_published INTEGER NOT NULL DEFAULT 1,
                created_by_user_id INTEGER,
                updated_by_user_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(category_id) REFERENCES knowledge_categories(id) ON DELETE SET NULL,
                FOREIGN KEY(created_by_user_id) REFERENCES users(id) ON DELETE SET NULL,
                FOREIGN KEY(updated_by_user_id) REFERENCES users(id) ON DELETE SET NULL
            );

CREATE TABLE IF NOT EXISTS chat_funnels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_default INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

CREATE TABLE IF NOT EXISTS chat_statuses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT NOT NULL UNIQUE,
                title TEXT NOT NULL,
                funnel_id INTEGER,
                color TEXT,
                sort_order INTEGER NOT NULL DEFAULT 0,
                is_system INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(funnel_id) REFERENCES chat_funnels(id) ON DELETE SET NULL
            );

CREATE INDEX IF NOT EXISTS idx_messages_direction_created_at
                ON messages(direction, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_chat_direction_created_at
                ON messages(chat_id, direction, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_chat_created_id
                ON messages(chat_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_messages_chat_direction_id
                ON messages(chat_id, direction, id);

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_external_unique
                ON messages(chat_id, external_message_id)
                WHERE external_message_id IS NOT NULL AND TRIM(external_message_id)<>'';

CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_chat_operation_unique
                ON messages(chat_id, client_operation_id)
                WHERE client_operation_id IS NOT NULL AND TRIM(client_operation_id)<>'';

CREATE INDEX IF NOT EXISTS idx_chat_user_states_chat_user
                ON chat_user_states(chat_id, user_id);

CREATE INDEX IF NOT EXISTS idx_chat_user_states_user_pinned
                ON chat_user_states(user_id, is_pinned, chat_id);

CREATE INDEX IF NOT EXISTS idx_tasks_chat_created_id
                ON tasks(chat_id, created_at DESC, id DESC);

CREATE INDEX IF NOT EXISTS idx_chats_marketplace_status_last_message
                ON chats(marketplace, status, last_message_at);

CREATE INDEX IF NOT EXISTS idx_chats_marketplace_last_message
                ON chats(marketplace, last_message_at);

CREATE INDEX IF NOT EXISTS idx_notifications_user_read_created
                ON notifications(user_id, is_read, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_notifications_chat
                ON notifications(chat_id);

CREATE INDEX IF NOT EXISTS idx_notifications_task
                ON notifications(task_id);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_user_active
                ON push_subscriptions(user_id, is_active);

CREATE INDEX IF NOT EXISTS idx_push_subscriptions_endpoint
                ON push_subscriptions(endpoint);

CREATE INDEX IF NOT EXISTS idx_push_outbox_pending
                ON push_outbox(sent_at, next_attempt_at, attempts, created_at);

CREATE INDEX IF NOT EXISTS idx_push_outbox_user
                ON push_outbox(user_id);

CREATE INDEX IF NOT EXISTS idx_tasks_status_type
                ON tasks(status, task_type_id);

CREATE INDEX IF NOT EXISTS idx_tasks_due_at_id
                ON tasks(due_at, id);

CREATE INDEX IF NOT EXISTS idx_tasks_assigned_due_id
                ON tasks(assigned_user_id, due_at, id);

CREATE INDEX IF NOT EXISTS idx_task_types_active_sort
                ON task_types(is_active, sort_order, title);

CREATE INDEX IF NOT EXISTS idx_task_status_effect_chat_applied
                ON tasks(chat_id, chat_status_effect_applied_at DESC, id DESC)
                WHERE chat_status_effect_key IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_dedupe
                ON notifications(dedupe_key) WHERE dedupe_key IS NOT NULL AND dedupe_key != '';

CREATE INDEX IF NOT EXISTS idx_reply_templates_active_sort
                ON reply_templates(is_active, sort_order, updated_at DESC);
