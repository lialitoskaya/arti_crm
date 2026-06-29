# Arti CRM v22 — corrected mobile interaction fix

## What was wrong in v21

v21 added CSS `contain: layout paint style` to `.chat-header` and related mobile layers. That was the wrong place to optimize.

On iOS Safari, paint containment creates a separate painting/stacking context and can clip absolutely positioned descendants. Because the chat menu lives inside the header row, this made the menu render below/behind the chat body and visually changed the upper panel.

## What v22 does

1. Restores CSS base to v20/v19 instead of layering the risky v21 containment block.
2. Keeps only safe mobile interaction CSS:
   - `touch-action: manipulation`;
   - high z-index for header/menu/panels;
   - `overflow: visible` for header/header row.
3. Reduces UI work during basic mobile actions:
   - status/assignee changes no longer trigger heavy stats/list refresh while the mobile chat is open;
   - background list refresh is deferred to idle;
   - message refresh skips while header controls, panels or composer are active.

## Remaining performance boundary

If a native select still opens slowly on iPhone Safari, the next structural step is to replace the two native `<select>` controls in the mobile chat header with custom lightweight popover buttons. Native iOS selects are browser-controlled and can feel delayed under heavy pages.
