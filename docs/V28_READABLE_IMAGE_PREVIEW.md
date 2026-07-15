# V28 readable image preview

Problem:
Image previews were shown as small cropped thumbnails because `.image-card` used a fixed 96x96 box and `object-fit: cover`.

Fix:
- Add `message-has-images` class in renderMessages when a message contains images.
- Increase image message width.
- Override `.image-card` to a larger responsive preview.
- Use `object-fit: contain` and `height: auto` to avoid cropping.

Version: v28-readable-image-preview-20260629
