"""Bounded photo inputs for the optional visual descriptor extractor.

Portal URLs are display links, not blanket permission for a server-side fetch.
This module accepts only the three image CDNs already captured by the portal
readers, refuses redirects, and streams a small number of bytes at a time.
The bridge receives typed bytes and hashes, never a URL or host filesystem
path.  It may therefore attach an image to Codex without acquiring a general
network or file-read capability.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Mapping, Optional, Sequence
from urllib.parse import urljoin, urlsplit

import requests

from services import attachments, portal_photos

SCHEMA_VERSION = 1
MAX_IMAGES = 3
MAX_IMAGE_BYTES = 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
_CHUNK_BYTES = 64 * 1024
_CONNECT_TIMEOUT_S = 3
_READ_TIMEOUT_S = 15

# These are image origins established by the portal import readers, rather
# than an arbitrary host supplied in a row edited through psql.
_PORTAL_IMAGE_HOSTS = frozenset(
    {
        "static.fotocasa.es",
        "media.yaencontre.com",
        "images.milanuncios.com",
        "images-re.milanuncios.com",
    }
)

_CONTENT_TYPES = frozenset({"image/jpeg", "image/png"})
VISUAL_ASPECT_IDS = frozenset(
    {
        "visual_appeal",
        "house_character",
        "house_condition",
        "neighbor_privacy",
        "nearby_buildings",
        "agricultural_context",
        "room_scale",
        "plot_outline",
    }
)
_VISUAL_STATUSES = frozenset({"supported", "claimed", "unknown", "conflicting"})
# Photo observations compare only when they use the same finite vocabulary. The
# words describe what the supplied frame shows; none infer an unseen neighbour,
# a parcel boundary, or a fact outside the frame.
VISUAL_VALUE_VOCABULARY: Dict[str, frozenset[str]] = {
    "visual_appeal": frozenset(
        {
            "traditional_stone",
            "painted_facade",
            "weathered_facade",
            "modern_finish",
            "plain_finish",
        }
    ),
    "house_character": frozenset(
        {"old_farmhouse", "stone_house", "rural_house", "modern_house", "mixed_style"}
    ),
    "house_condition": frozenset(
        {"well_maintained", "weathered", "damp", "major_renovation", "ruined"}
    ),
    "neighbor_privacy": frozenset({"neighbours_visible", "screened_view"}),
    "nearby_buildings": frozenset({"few_visible", "several_visible", "dense_visible"}),
    "agricultural_context": frozenset(
        {"agricultural_structures_visible", "cultivated_land_visible"}
    ),
    "room_scale": frozenset({"small", "medium", "large"}),
    # A photograph never establishes a parcel outline.
    "plot_outline": frozenset(),
}


class _ImageRefs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.sources: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "img":
            source = dict(attrs).get("src")
            if source:
                self.sources.append(source)


class VisualInputError(ValueError):
    """A visual source cannot be used safely or within the declared bounds."""


def _default_https_port(parts: Any) -> bool:
    try:
        port = parts.port
    except ValueError:
        return False
    return port in {None, 443}


def _normalise_photo_url(value: Any) -> str:
    url = portal_photos.normalise_photo_url(value)
    if url is None:
        raise VisualInputError("photo URL is malformed or carries a credential")
    parts = urlsplit(url)
    if (
        parts.scheme.lower() != "https"
        or parts.hostname is None
        or parts.hostname.lower() not in _PORTAL_IMAGE_HOSTS
        or not _default_https_port(parts)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise VisualInputError("photo URL is not an approved direct HTTPS portal image")
    return url


def portal_photo_sources(
    prop: Any, *, max_images: int = MAX_IMAGES
) -> List[Dict[str, str]]:
    """Return authorized portal image references from the canonical reader.

    A stored photo block alone does not authorize an arbitrary hostname: each
    value must survive both ``read_photos`` and the stricter fetch allowlist.
    Refused entries are omitted so a manually injected URL is never dialled.
    """
    if not isinstance(max_images, int) or not 1 <= max_images <= MAX_IMAGES:
        raise VisualInputError(f"max_images must be between 1 and {MAX_IMAGES}")
    reading = portal_photos.read_photos(prop)
    if reading.get("state") != portal_photos.STATE_CAPTURED:
        return []

    sources: List[Dict[str, str]] = []
    for index, item in enumerate(reading.get("photos") or []):
        if not isinstance(item, Mapping):
            continue
        try:
            url = _normalise_photo_url(item.get("url"))
        except VisualInputError:
            continue
        sources.append(
            {
                "source_kind": "portal_photo",
                "source_id": f"property:{getattr(prop, 'id', 'unknown')}:photo:{index}",
                "url": url,
            }
        )
        if len(sources) == max_images:
            break
    return sources


def dossier_photo_sources(
    prop: Any, *, session: Any = None, max_images: int = MAX_IMAGES
) -> List[Dict[str, str]]:
    """Read a small, exact-host image sample from an explicitly stored dossier."""
    from services import dossier

    if not isinstance(max_images, int) or not 1 <= max_images <= MAX_IMAGES:
        raise VisualInputError(f"max_images must be between 1 and {MAX_IMAGES}")
    record = dossier.read_dossier(prop)
    prop_id = getattr(prop, "id", None)
    if not record or not isinstance(prop_id, int):
        return []
    url = record["url"]
    parts = urlsplit(url)
    expected_host = f"{prop_id}.cervantes50.com"
    if (
        parts.scheme != "https"
        or parts.hostname != expected_host
        or not _default_https_port(parts)
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        return []
    http = session or requests.Session()
    response = None
    try:
        response = http.get(
            url,
            stream=True,
            allow_redirects=False,
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
        )
        if response.status_code != 200:
            return []
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            if len(body) + len(chunk) > 128 * 1024:
                return []
            body.extend(chunk)
    except requests.RequestException:
        return []
    finally:
        try:
            if response is not None:
                response.close()
        except Exception:
            pass
    parser = _ImageRefs()
    parser.feed(bytes(body).decode("utf-8", "replace"))
    result = []
    for index, raw in enumerate(parser.sources):
        image_url = urljoin(url, raw)
        image = urlsplit(image_url)
        if (
            image.scheme == "https"
            and image.hostname == expected_host
            and _default_https_port(image)
            and image.username is None
            and image.password is None
            and not image.query
            and not image.fragment
        ):
            result.append(
                {
                    "source_kind": "dossier_photo",
                    "source_id": f"property:{prop_id}:dossier:{index}",
                    "url": image_url,
                }
            )
        if len(result) == max_images:
            break
    return result


def _declared_size(response: Any) -> Optional[int]:
    value = (getattr(response, "headers", {}) or {}).get("Content-Length")
    if value is None:
        return None
    try:
        size = int(value)
    except (TypeError, ValueError):
        return None
    return size if size >= 0 else None


def _sniff_content_type(data: bytes) -> str:
    if data.startswith(b"\xff\xd8\xff"):
        _image_dimensions(data, "image/jpeg")
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        _image_dimensions(data, "image/png")
        return "image/png"
    raise VisualInputError("photo bytes are not an accepted JPEG or PNG")


def _image_dimensions(data: bytes, content_type: str) -> tuple[int, int]:
    """Read dimensions from headers before a model can decompress the image."""
    if content_type == "image/png":
        if len(data) < 24:
            raise VisualInputError("PNG header is truncated")
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    else:
        index = 2
        width = height = 0
        while index + 9 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            while index < len(data) and data[index] == 0xFF:
                index += 1
            if index >= len(data):
                break
            marker = data[index]
            index += 1
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index : index + 2], "big")
            if length < 7 or index + length > len(data):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            }:
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                break
            index += length
    if width < 1 or height < 1:
        raise VisualInputError("image dimensions are unreadable")
    if width * height > MAX_IMAGE_PIXELS:
        raise VisualInputError("image exceeds pixel limit")
    return width, height


def download_photo_source(
    source: Mapping[str, Any], *, session: Any = None
) -> Dict[str, str]:
    """Download one already-authorized source, with no redirect or size escape."""
    if not isinstance(source, Mapping):
        raise VisualInputError("photo source must be an object")
    source_kind = source.get("source_kind")
    if source_kind not in {"portal_photo", "dossier_photo"}:
        raise VisualInputError("photo source kind is not approved for network access")
    source_id = source.get("source_id")
    if not isinstance(source_id, str) or not source_id.strip() or len(source_id) > 200:
        raise VisualInputError("photo source id is invalid")
    if source_kind == "portal_photo":
        url = _normalise_photo_url(source.get("url"))
    else:
        matched = re.fullmatch(r"property:(\d+):dossier:\d+", source_id)
        parts = urlsplit(str(source.get("url") or ""))
        if (
            matched is None
            or parts.scheme != "https"
            or parts.hostname != f"{matched.group(1)}.cervantes50.com"
            or not _default_https_port(parts)
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise VisualInputError(
                "dossier photo URL is not an approved exact-host image"
            )
        url = str(source["url"])

    http = session or requests.Session()
    try:
        response = http.get(
            url,
            stream=True,
            allow_redirects=False,
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            headers={"User-Agent": "IdealistaRank visual descriptor input"},
        )
    except requests.RequestException as exc:
        raise VisualInputError("photo source did not answer") from exc

    try:
        if response.status_code != 200:
            raise VisualInputError(f"photo source returned HTTP {response.status_code}")
        declared = _declared_size(response)
        if declared is not None and declared > MAX_IMAGE_BYTES:
            raise VisualInputError("photo source announced too many bytes")
        body = bytearray()
        for chunk in response.iter_content(chunk_size=_CHUNK_BYTES):
            if not chunk:
                continue
            if len(body) + len(chunk) > MAX_IMAGE_BYTES:
                raise VisualInputError("photo source exceeded byte limit")
            body.extend(chunk)
    except requests.RequestException as exc:
        raise VisualInputError("photo source body could not be read") from exc
    finally:
        try:
            response.close()
        except Exception:
            pass

    data = bytes(body)
    content_type = _sniff_content_type(data)
    return {
        "source_kind": source_kind,
        "source_id": source_id,
        "content_type": content_type,
        "content_sha256": hashlib.sha256(data).hexdigest(),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def download_portal_photo_inputs(
    prop: Any, *, session: Any = None, max_images: int = MAX_IMAGES
) -> List[Dict[str, str]]:
    """Fetch the bounded canonical portal-photo sample for an explicit job."""
    return [
        download_photo_source(source, session=session)
        for source in portal_photo_sources(prop, max_images=max_images)
    ]


def download_dossier_photo_inputs(
    prop: Any, *, session: Any = None, max_images: int = MAX_IMAGES
) -> List[Dict[str, str]]:
    """Fetch the bounded explicit dossier-photo sample for an explicit job."""
    return [
        download_photo_source(source, session=session)
        for source in dossier_photo_sources(
            prop, session=session, max_images=max_images
        )
    ]


def attachment_photo_input(record: Any) -> Dict[str, str]:
    """Read one hash-addressed raster attachment without trusting its path.

    ``PropertyAttachment.storage_path`` is database metadata and supported
    maintenance can edit it directly.  Reconstructing the only valid path from
    the content hash and sniffed MIME means such an edit cannot turn a visual
    job into a reader for an unrelated container file.
    """
    content_type = getattr(record, "content_type", None)
    digest = getattr(record, "content_sha256", None)
    if content_type not in _CONTENT_TYPES or not isinstance(digest, str):
        raise VisualInputError("attachment is not a supported raster image")
    digest = digest.lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise VisualInputError("attachment has an invalid content hash")
    rules = attachments.ALLOWED_TYPES.get(content_type)
    if rules is None:
        raise VisualInputError("attachment content type is not allowed")
    expected_path = attachments.storage_path(digest, rules["ext"])
    if getattr(record, "storage_path", None) != expected_path:
        raise VisualInputError("attachment path does not match its content hash")
    try:
        with open(attachments.absolute_path(record), "rb") as handle:
            data = handle.read(MAX_IMAGE_BYTES + 1)
    except OSError as exc:
        raise VisualInputError("attachment bytes are unavailable") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise VisualInputError("attachment is empty or exceeds byte limit")
    if _sniff_content_type(data) != content_type:
        raise VisualInputError("attachment type does not match its bytes")
    if hashlib.sha256(data).hexdigest() != digest:
        raise VisualInputError("attachment hash does not match its bytes")
    attachment_id = getattr(record, "id", None)
    if not isinstance(attachment_id, int) or attachment_id <= 0:
        raise VisualInputError("attachment id is invalid")
    return {
        "source_kind": "attachment_photo",
        "source_id": f"attachment:{attachment_id}",
        "content_type": content_type,
        "content_sha256": digest,
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


def _canonical_image_identity(image: Mapping[str, Any]) -> Dict[str, str]:
    required = (
        "source_kind",
        "source_id",
        "content_type",
        "content_sha256",
        "content_base64",
    )
    if not isinstance(image, Mapping) or any(
        not isinstance(image.get(key), str) for key in required
    ):
        raise VisualInputError("image input has an invalid shape")
    if (
        image["source_kind"]
        not in {"portal_photo", "dossier_photo", "attachment_photo"}
        or not image["source_id"].strip()
    ):
        raise VisualInputError("image input has an unapproved source")
    if image["content_type"] not in _CONTENT_TYPES:
        raise VisualInputError("image input has an unapproved content type")
    digest = image["content_sha256"].lower()
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
        raise VisualInputError("image input has an invalid content hash")
    try:
        data = base64.b64decode(image["content_base64"], validate=True)
    except (ValueError, TypeError) as exc:
        raise VisualInputError("image input is not valid base64") from exc
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise VisualInputError("image input is empty or exceeds byte limit")
    if _sniff_content_type(data) != image["content_type"]:
        raise VisualInputError("image input content type does not match bytes")
    if hashlib.sha256(data).hexdigest() != digest:
        raise VisualInputError("image input content hash does not match bytes")
    return {
        "source_kind": image["source_kind"],
        "source_id": image["source_id"],
        "content_type": image["content_type"],
        "content_sha256": digest,
    }


def build_visual_input(image_inputs: Sequence[Mapping[str, Any]]) -> Dict[str, Any]:
    """Build the non-persistent descriptor envelope for validated image inputs."""
    if not isinstance(image_inputs, Sequence) or isinstance(image_inputs, (str, bytes)):
        raise VisualInputError("image inputs must be a list")
    if not image_inputs or len(image_inputs) > MAX_IMAGES:
        raise VisualInputError(f"image inputs must contain 1 to {MAX_IMAGES} images")
    identities = [_canonical_image_identity(image) for image in image_inputs]
    fingerprint = hashlib.sha256(
        json.dumps(identities, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": fingerprint,
        "visual_observations": [],
    }


def visual_output_schema() -> Dict[str, Any]:
    """JSON Schema used for the one explicit visual extraction call."""
    return {
        "type": "object",
        "properties": {
            "visual_observations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "aspect_id": {
                            "type": "string",
                            "enum": sorted(VISUAL_ASPECT_IDS),
                        },
                        "value": {"type": ["string", "null"]},
                        "status": {"type": "string", "enum": sorted(_VISUAL_STATUSES)},
                        "evidence": {
                            "type": "object",
                            "properties": {
                                "source_kind": {"type": "string"},
                                "source_id": {"type": "string"},
                                "image_sha256": {"type": "string"},
                                "image_index": {"type": "integer", "minimum": 0},
                            },
                            "required": [
                                "source_kind",
                                "source_id",
                                "image_sha256",
                                "image_index",
                            ],
                            "additionalProperties": False,
                        },
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "limitation": {"type": "string"},
                    },
                    "required": [
                        "aspect_id",
                        "value",
                        "status",
                        "evidence",
                        "confidence",
                        "limitation",
                    ],
                    "additionalProperties": False,
                    "allOf": [
                        {
                            "if": {
                                "properties": {"status": {"enum": ["unknown"]}},
                                "required": ["status"],
                            },
                            "then": {"properties": {"value": {"enum": [None]}}},
                        }
                    ]
                    + [
                        {
                            "if": {
                                "properties": {"aspect_id": {"enum": [aspect_id]}},
                                "required": ["aspect_id"],
                            },
                            "then": {
                                "properties": {
                                    "value": {
                                        "enum": [
                                            *sorted(VISUAL_VALUE_VOCABULARY[aspect_id]),
                                            None,
                                        ]
                                    }
                                }
                            },
                        }
                        for aspect_id in sorted(VISUAL_ASPECT_IDS)
                    ]
                    + [
                        {
                            "if": {
                                "properties": {"aspect_id": {"enum": ["plot_outline"]}},
                                "required": ["aspect_id"],
                            },
                            "then": {
                                "properties": {
                                    "status": {"enum": ["unknown"]},
                                    "value": {"enum": [None]},
                                }
                            },
                        }
                    ],
                },
            }
        },
        "required": ["visual_observations"],
        "additionalProperties": False,
    }


def validate_visual_observations(
    payload: Mapping[str, Any], image_inputs: Sequence[Mapping[str, Any]]
) -> List[Dict[str, Any]]:
    """Accept only observations tied to the exact hashed input image.

    A model can describe pixels, not parcel geometry, dimensions, boundaries,
    legal facts, or an absence outside the frame.  ``plot_outline`` is therefore
    always unknown, while every observation keeps a stated limitation.
    """
    build_visual_input(image_inputs)  # validates the sources and byte hashes first
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("visual_observations"), list
    ):
        raise VisualInputError("visual result has no observation list")
    observations: List[Dict[str, Any]] = []
    for item in payload["visual_observations"]:
        if not isinstance(item, Mapping):
            raise VisualInputError("visual observation must be an object")
        aspect_id = item.get("aspect_id")
        status = item.get("status")
        value = item.get("value")
        evidence = item.get("evidence")
        confidence = item.get("confidence")
        limitation = item.get("limitation")
        if (
            aspect_id not in VISUAL_ASPECT_IDS
            or status not in _VISUAL_STATUSES
            or not (isinstance(value, str) or value is None)
            or not isinstance(evidence, Mapping)
            or isinstance(confidence, bool)
            or not isinstance(confidence, (int, float))
            or not 0 <= confidence <= 1
            or not isinstance(limitation, str)
            or not limitation.strip()
        ):
            raise VisualInputError("visual observation has an invalid shape")
        if status == "unknown" and value is not None:
            raise VisualInputError("an unknown visual observation must have no value")
        if value is not None and value not in VISUAL_VALUE_VOCABULARY[aspect_id]:
            raise VisualInputError(
                "visual observation value is outside its canonical vocabulary"
            )
        index = evidence.get("image_index")
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index < len(image_inputs)
        ):
            raise VisualInputError("visual observation points at an unknown image")
        image = _canonical_image_identity(image_inputs[index])
        if (
            evidence.get("source_kind") != "photo"
            or evidence.get("source_id") != image["source_id"]
            or evidence.get("image_sha256") != image["content_sha256"]
        ):
            raise VisualInputError(
                "visual observation evidence does not match its image"
            )
        if aspect_id == "plot_outline" and (status != "unknown" or value is not None):
            raise VisualInputError("a photo cannot establish a parcel outline")
        observations.append(
            {
                "aspect_id": aspect_id,
                "value": value,
                "status": status,
                "evidence": {
                    "source_kind": "photo",
                    "source_id": image["source_id"],
                    "image_sha256": image["content_sha256"],
                    "image_index": index,
                },
                "confidence": float(confidence),
                "limitation": limitation.strip(),
            }
        )
    return observations


def extract_visual_observations(
    prompt: str,
    image_inputs: Sequence[Mapping[str, Any]],
    *,
    model: str = "",
    timeout: int = 300,
) -> Dict[str, Any]:
    """Run one opt-in Codex extraction and return a non-persistent envelope."""
    if not isinstance(prompt, str) or not prompt.strip():
        raise VisualInputError("visual extraction prompt is required")
    envelope = build_visual_input(image_inputs)
    from services import subscription_transport

    result = subscription_transport.complete_with_images(
        prompt,
        images=list(image_inputs),
        provider="codex",
        model=model,
        timeout=timeout,
        schema=visual_output_schema(),
    )
    try:
        payload = json.loads(str(result.get("text") or ""))
    except ValueError as exc:
        raise VisualInputError("visual extractor returned non-JSON output") from exc
    envelope["visual_observations"] = validate_visual_observations(
        payload, image_inputs
    )
    return envelope
