from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import unquote


LEGACY_KNOWLEDGE_IMAGE_URL_PREFIX = "/static/uploads/knowledge/"
PRIVATE_KNOWLEDGE_IMAGE_REFERENCE_PREFIX = "knowledge-private:"
LEGACY_PRIVATE_KNOWLEDGE_IMAGE_REFERENCE_PREFIX = "/api/knowledge/images/"
_ALLOWED_EXTENSIONS = frozenset({".jpg", ".png", ".webp", ".gif"})
_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


def private_storage_root(storage_root: Path, public_static_root: Path) -> Path:
    resolved_storage = storage_root.resolve()
    resolved_public = public_static_root.resolve()
    if resolved_path_is_within(resolved_storage, resolved_public):
        raise ValueError("Knowledge image storage must be outside the public static directory")
    return resolved_storage


def resolved_path_is_within(candidate: Path | str, root: Path | str) -> bool:
    resolved_candidate = os.path.normcase(os.path.realpath(os.fspath(candidate)))
    resolved_root = os.path.normcase(os.path.realpath(os.fspath(root)))
    try:
        return os.path.commonpath([resolved_candidate, resolved_root]) == resolved_root
    except ValueError:
        return False


def lexical_path_is_within(candidate: Path | str, root: Path | str) -> bool:
    normalized_candidate = os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(candidate))))
    normalized_root = os.path.normcase(os.path.abspath(os.path.normpath(os.fspath(root))))
    try:
        return os.path.commonpath([normalized_candidate, normalized_root]) == normalized_root
    except ValueError:
        return False


def normalized_static_lookup_path(path: str) -> str:
    normalized = str(path or "")
    for _ in range(3):
        decoded = unquote(normalized)
        if decoded == normalized:
            break
        normalized = decoded
    normalized = normalized.replace("\\", os.sep).replace("/", os.sep)
    return normalized.lstrip("/\\")


def _valid_filename(filename: str) -> bool:
    candidate = str(filename or "")
    stem, extension = os.path.splitext(candidate)
    return (
        len(stem) == 32
        and all(character in "0123456789abcdef" for character in stem)
        and extension in _ALLOWED_EXTENSIONS
    )


def _filename_from_reference(reference: str, prefix: str) -> str | None:
    if not reference.startswith(prefix):
        return None
    filename = reference[len(prefix):]
    return filename if _valid_filename(filename) else None


def private_knowledge_image_reference(filename: str) -> str | None:
    if not _valid_filename(filename):
        return None
    return f"{LEGACY_PRIVATE_KNOWLEDGE_IMAGE_REFERENCE_PREFIX}{filename}"


def article_knowledge_image_url(article_id: int) -> str:
    return f"/api/knowledge/articles/{int(article_id)}/image"


def resolve_knowledge_image_path(storage_root: Path, filename: str) -> Path | None:
    if not _valid_filename(filename):
        return None
    resolved_root = storage_root.resolve()
    lexical_candidate = resolved_root / filename
    if not lexical_path_is_within(lexical_candidate, resolved_root):
        return None
    resolved_candidate = lexical_candidate.resolve()
    if not resolved_path_is_within(resolved_candidate, resolved_root):
        return None
    return resolved_candidate


def resolve_article_image_reference(
    image_reference: str | None,
    *,
    legacy_root: Path,
    private_root: Path,
    public_static_root: Path,
) -> Path | None:
    reference = str(image_reference or "")
    filename = _filename_from_reference(reference, LEGACY_KNOWLEDGE_IMAGE_URL_PREFIX)
    if filename is not None:
        storage_root = legacy_root.resolve()
    else:
        filename = _filename_from_reference(reference, PRIVATE_KNOWLEDGE_IMAGE_REFERENCE_PREFIX)
        if filename is None:
            filename = _filename_from_reference(
                reference,
                LEGACY_PRIVATE_KNOWLEDGE_IMAGE_REFERENCE_PREFIX,
            )
        if filename is None:
            return None
        storage_root = private_storage_root(private_root, public_static_root)
    path = resolve_knowledge_image_path(storage_root, filename)
    return path if path is not None and path.is_file() else None


def knowledge_image_media_type(path: Path) -> str | None:
    return _MEDIA_TYPES.get(path.suffix.lower())
