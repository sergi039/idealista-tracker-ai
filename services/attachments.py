"""Documents and photos attached to a listing (#430).

The worked example is the one the ticket opens with: the agency sent the *ficha
catastral* by WhatsApp, and the PDF had nowhere to go. A cadastral plan, a
photo of the frontage, a scan of the condiciones -- evidence that is not a
measurement and cannot be recomputed, which is what makes losing it different
from losing a drive time.

**The bytes live on disk and the row holds metadata.** `./data` is the app's
one bind mount, so a file written under it survives the `COPY . .` rebuild that
takes the image; Postgres would carry the same bytes through every `pg_dump`
instead, and this database is 17 MB today against photos that are megabytes
each. What that costs is the two systems not sharing a transaction, and §
*writing* below is how that is paid.

**Content-addressed, so the name is never the user's.** The on-disk name is the
sha256 of the content, sharded two levels (`ab/cd/abcd....jpg`), which makes
path traversal structurally impossible on the write side rather than filtered
on it, gives deduplication for nothing, and lets a later job re-hash a file to
find a truncated write. The name the browser sent is metadata: it is shown, it
goes in `Content-Disposition`, and it never touches a path.

**Write, then commit -- never the other way round.** An orphan *file* is inert
disk the sweeper reclaims; an orphan *row* is a download that 404s and is
indistinguishable at read time from "the sweeper has not run yet". So: stream
into a temporary file **in the destination directory** (`os.replace` is atomic
only within one filesystem), hash and count bytes as they arrive, fsync, rename
into place, and only then insert the row.

**The type is what the bytes say.** `puremagic` reads the signature and the
result is checked against a short allowlist; the browser's `Content-Type` and
the filename's extension are hints for humans and are never trusted for the
decision, the stored type, or the served one. **SVG is not an accepted format
at all** -- it is XML, it can carry `<script>`, and no amount of `nosniff`
helps a document the browser is right to render. HEIC is accepted (an iPhone
sends it) and always served as a download, because most browsers cannot draw
it and a broken inline image reads as a broken feature.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# What may be stored, keyed by the MIME type `puremagic` reports for the real
# bytes. The extension here is the one written on disk -- derived from the
# sniffed type and never from the uploaded name.
#
# `inline` says whether the download route may let a browser draw it in place.
# Only formats a browser renders as an image are allowed to; everything else is
# a download, which is what stops a document from being *executed* rather than
# merely mis-drawn.
ALLOWED_TYPES: Dict[str, Dict[str, Any]] = {
    "application/pdf": {"ext": "pdf", "inline": False, "kind": "document"},
    "image/jpeg": {"ext": "jpg", "inline": True, "kind": "photo"},
    "image/png": {"ext": "png", "inline": True, "kind": "photo"},
    "image/gif": {"ext": "gif", "inline": True, "kind": "photo"},
    "image/webp": {"ext": "webp", "inline": True, "kind": "photo"},
    # An iPhone photo, unless something converted it on the way. Stored, and
    # always served as a download: Chrome and Firefox do not draw it.
    "image/heif": {"ext": "heic", "inline": False, "kind": "photo"},
    "image/heic": {"ext": "heic", "inline": False, "kind": "photo"},
}

# 25 MB a file. `MAX_CONTENT_LENGTH` bounds the whole request body and cannot
# bound one part of it, so this is counted while the bytes stream past.
MAX_FILE_BYTES = 25 * 1024 * 1024

# What a temporary file is called while it is being written. The sweeper skips
# these by name: a partial upload is not an orphan, it is an upload.
TEMP_PREFIX = ".incoming-"


class AttachmentError(ValueError):
    """A refused upload. The route turns it into a flash, never a 500."""


def attachments_dir() -> str:
    """Where the bytes live. Under DATA_DIR, which is the one bind mount."""
    from config import Config

    return getattr(
        Config, "ATTACHMENTS_DIR", os.path.join(Config.DATA_DIR, "attachments")
    )


def storage_path(content_sha256: str, extension: str) -> str:
    """The relative path a hash maps to: `ab/cd/abcd….ext`.

    Two levels of sharding, the git and CDN convention. A personal archive of
    documents and phone photos crosses the "too many files in one directory"
    threshold eventually, and the hash is already in hand, so it costs nothing
    to do now and a migration to do later.
    """
    return os.path.join(
        content_sha256[0:2], content_sha256[2:4], f"{content_sha256}.{extension}"
    )


def sniff(head: bytes) -> Tuple[str, Dict[str, Any]]:
    """The MIME type of these bytes, or a refusal naming what was seen.

    `puremagic` reads signatures; this narrows its answer to the allowlist
    above. The narrowing is the security boundary -- puremagic knows hundreds
    of formats, and "it identified something" is not "this is a thing we
    accept".
    """
    import puremagic

    try:
        matches = puremagic.magic_string(head)
    except Exception:  # puremagic raises on bytes it cannot place at all
        matches = []

    for match in matches:
        mime = (match.mime_type or "").lower()
        if mime in ALLOWED_TYPES:
            return mime, ALLOWED_TYPES[mime]

    seen = matches[0].mime_type if matches else "nothing recognisable"
    raise AttachmentError(
        f"that file is a {seen}; this page takes PDFs and photos "
        "(JPEG, PNG, GIF, WebP, HEIC)"
    )


def store(stream, *, original_filename: str = "") -> Dict[str, Any]:
    """Write an uploaded stream to its content-addressed path.

    Returns the metadata a row needs. Raises `AttachmentError` for anything
    refused, having written nothing that survives.

    The order is deliberate and asymmetric: a file that exists with no row is
    inert, while a row pointing at a missing file is a user-visible 404 nobody
    can tell from a sweep that has not run. So nothing here commits anything --
    the caller inserts the row *after* this returns.
    """
    root = attachments_dir()
    os.makedirs(root, exist_ok=True)

    # The first chunk decides the type, before the rest is worth writing.
    head = stream.read(4096)
    if not head:
        raise AttachmentError("that file is empty")
    mime, rules = sniff(head)

    destination_dir = os.path.join(root, "tmp")
    os.makedirs(destination_dir, exist_ok=True)

    digest = hashlib.sha256()
    size = 0
    handle = None
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix=TEMP_PREFIX, dir=destination_dir)
        handle = os.fdopen(fd, "wb")
        chunk = head
        while chunk:
            size += len(chunk)
            if size > MAX_FILE_BYTES:
                raise AttachmentError(
                    f"that file is over {MAX_FILE_BYTES // (1024 * 1024)} MB"
                )
            digest.update(chunk)
            handle.write(chunk)
            chunk = stream.read(65536)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None

        content_sha256 = digest.hexdigest()
        relative = storage_path(content_sha256, rules["ext"])
        final_path = os.path.join(root, relative)
        os.makedirs(os.path.dirname(final_path), exist_ok=True)

        # Atomic, and a duplicate takes this path too rather than skipping it:
        # replacing the existing file refreshes its mtime, which is what keeps
        # the sweeper's age check from collecting bytes a row is about to
        # reference. Same filesystem by construction -- the temporary file was
        # made under the same root.
        os.replace(temp_path, final_path)
        temp_path = None
    finally:
        if handle is not None:
            handle.close()
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)

    return {
        "content_sha256": content_sha256,
        "storage_path": relative,
        "content_type": mime,
        "size_bytes": size,
        "kind": rules["kind"],
        "inline": rules["inline"],
        "original_filename": (original_filename or "")[:255],
    }


def absolute_path(record: Any) -> str:
    """Where one attachment's bytes are. Never built from anything a client sent."""
    return os.path.join(attachments_dir(), record.storage_path)


def may_render_inline(content_type: Optional[str]) -> bool:
    """Whether a browser may draw this in place rather than download it.

    Read off the *stored, sniffed* type. A file whose bytes are HTML cannot
    reach this dictionary at all, so the question never becomes "is this
    payload safe" -- it is "is this one of five raster formats".
    """
    rules = ALLOWED_TYPES.get((content_type or "").lower())
    return bool(rules and rules["inline"])


def attach(prop: Any, upload: Any, *, activity: Any = None) -> Any:
    """Store an uploaded file and record it against a property.

    The order is the whole contract: `store()` writes and fsyncs the bytes and
    only then is a row inserted and committed. A crash between the two leaves
    an orphan *file*, which is inert and which `utils/sweep_attachments.py`
    reclaims; the reverse order would leave a row whose download 404s, and no
    reader could tell that from a sweep that has not run yet.

    `activity` is the timeline entry the file arrived with, when there is one.
    It is passed as the object rather than as an id so the caller cannot hand
    in an entry belonging to another property -- and if one somehow does, the
    composite foreign key refuses the insert (migration 023).
    """
    from app import db
    from models import PropertyAttachment

    stored = store(upload.stream, original_filename=upload.filename or "")

    record = PropertyAttachment(
        property_id=prop.id,
        activity_id=activity.id if activity is not None else None,
        content_sha256=stored["content_sha256"],
        storage_path=stored["storage_path"],
        original_filename=stored["original_filename"] or None,
        content_type=stored["content_type"],
        size_bytes=stored["size_bytes"],
        kind=stored["kind"],
    )
    db.session.add(record)
    db.session.commit()
    return record


def for_property(prop: Any) -> Dict[Optional[int], list]:
    """Live attachments, grouped by the entry they arrived with.

    Keyed by `activity_id`, with `None` collecting the ones filed against the
    listing itself, so the timeline can draw each entry's files beside it in
    one pass rather than a query per row.
    """
    from models import PropertyAttachment

    grouped: Dict[Optional[int], list] = {}
    rows = (
        PropertyAttachment.query.filter(
            PropertyAttachment.property_id == prop.id,
            PropertyAttachment.deleted_at.is_(None),
        )
        .order_by(PropertyAttachment.uploaded_at.asc(), PropertyAttachment.id.asc())
        .all()
    )
    for row in rows:
        grouped.setdefault(row.activity_id, []).append(row)
    return grouped


def human_size(size_bytes: Optional[int]) -> str:
    """A size a person reads, for the chip under an entry."""
    if not size_bytes:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"
