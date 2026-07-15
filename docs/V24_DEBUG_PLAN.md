# Arti CRM v24-debug-20260629 diagnostic plan

## Why this build exists

Previous patches were not enough because the exact delay source has not been measured inside the user's real browser/device.

This build adds instrumentation around:
- native selects `#chatStatus` and `#assignedUserSelect`;
- fetch requests;
- long tasks;
- render and refresh functions;
- DOM mutations of select options;
- loaded script/css version.

## Expected next step

After the user reproduces the delay and sends the copied debug JSON, inspect:
- whether the tap event is immediate;
- whether a long task happens around the tap;
- whether the select is rehydrated after pointerdown;
- whether fetch/sync blocks the main thread;
- whether the loaded version is actually `v24-debug-20260629`.

## This build intentionally does not attempt a new blind fix.
