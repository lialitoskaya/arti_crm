# V33 Ozon order links inside messages

## Problem

Ozon messages can contain:

```json
{
  "context": {
    "sku": "",
    "order_number": ""
  }
}
```

When `order_number` is filled and the same number is shown in message text, the order number should be clickable.

## Fix

- Added `extractOzonOrderNumber(message)`.
- Added `ozonPostingUrl(orderNumber)`.
- Replaced message text rendering with `renderMessageTextWithLinks(container, message, text)`.
- Exact occurrences of the order number in the message text become links to:
  `https://seller.ozon.ru/app/postings/{order_number}`

Version: v33-ozon-order-message-link-20260629
