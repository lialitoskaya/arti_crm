# V30 mobile extra actions fix

Problem:
After switcher/menu changes, tapping the extra menu functions in the mobile open-chat view could fail to open panels.

Root cause:
The menu action relied on a conversation-level delegated pointerdown handler, while the same controls also had a mobile click handler that prevented default and returned. On iOS Safari this is less reliable than binding the action directly to the button.

Fix:
- Bind [data-extra] buttons directly on pointerdown.
- Stop propagation before conversation/document handlers can interfere.
- Keep click fallback for browsers where pointerdown does not run.
- Ignore synthetic click within 700ms after pointerdown.
- Treat [data-extra] as inside menu for the outside-close pointerdown listener.

Version: v30-mobile-extra-actions-fix-20260629
