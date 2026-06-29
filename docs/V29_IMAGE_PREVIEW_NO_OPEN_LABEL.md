# V29 image preview no open label

Problem:
The image preview card showed an extra visible label "Открыть в полном размере".
The image is already inside a link, so the label is redundant and visually breaks the chat.

Fix:
- Removed fallback span creation from renderMessages().
- Added CSS guard to hide `.image-fallback` if old DOM remains from cache.
- Preserved click-to-open behavior on the image link.

Version: v29-image-preview-no-open-label-20260629
