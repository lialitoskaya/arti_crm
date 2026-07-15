# V26 extra menu switcher fix

## Problem

The extra chat menu button did not behave as a true toggle. Repeated taps could
reopen the menu because mobile Safari fires pointerdown and then a synthetic click.

Also, when an extra panel was open, tapping the menu button could close the panel
and immediately open the menu again.

## Fix

- Added `extraMenuPointerHandledAt`.
- Mobile pointerdown handles the action and stores timestamp.
- The following synthetic click is ignored for 700ms.
- `toggleExtraMenu()` now detects `activeExtraPanel && !isMenuOpen` and closes
  everything instead of reopening the menu.
- Outside pointerdown closes both menu and active panel.

## Version

v26-extra-menu-switcher-20260629
