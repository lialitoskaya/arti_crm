# V41 Ozon return message links

## Rule

Use the same structured number extraction as Ozon posting links:

1. Prefer full number from nested `data` arrays.
2. Fallback to explicit `context.order_number` fields.
3. Do not infer numbers from arbitrary message text.

## Link routing

- `*-R<digits>` => Ozon returns URL.
- everything else from the structured Ozon number => Ozon posting URL.

## Preserved user changes

The package uses the currently uploaded:

- `index(27).html`
- `styles(32).css`

Only cache-bust in index was updated.

Version: v41-ozon-return-message-links-20260629
