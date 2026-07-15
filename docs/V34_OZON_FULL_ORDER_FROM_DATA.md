# V34 Ozon full order from data

## Problem

`raw.context.order_number` can be incomplete. Ozon posting/order numbers can have suffixes such as `-1`, `-2`, etc.

When a message is sent from an order section, Ozon sends a `data` array with two elements; the first element is the full order/posting number.

## Fix

Priority changed:

1. Search nested `data` arrays.
2. If a `data` array has at least two elements and the first element looks like an order number, use that first element.
3. Fall back to explicit `context.order_number` / `context.orderNumber`.
4. Do not infer order numbers from arbitrary digits in the message text.

Version: v34-ozon-full-order-from-data-20260629
