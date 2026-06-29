# V39 mobile search zoom fix

## Problem

`chatSearchInput` is outside the mobile open-chat composer area, so the v36 mobile zoom fix did not cover it.
On iOS Safari, focused fields with font-size below 16px trigger viewport zoom.

## Fix

- Added mobile 16px font-size for `#chatSearchInput`.
- Disabled native WebKit search cancel button to remove the duplicate cross.

Version: v39-mobile-search-zoom-fix-20260629
