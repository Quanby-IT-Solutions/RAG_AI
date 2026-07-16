"""Multimodal ingestion (CE-7) — vision-worthy PDF pages as images.

For corpora where meaning lives in charts/figures, text extraction destroys
the signal. At upload time, PDF pages whose extractable text is sparse AND
which carry embedded graphics are rendered to PNG (pypdfium2 — permissive
license, no system deps) and stored in `page_images` (bytea; cascades with
the document). At indexing time each stored page gets a vision-model caption
(cache-first on the image hash — an unchanged image is never re-captioned)
that becomes a retrievable chunk carrying `modality: "image"` metadata and a
pointer to the image, flowing through the standard IN-2 index path.

Everything is fail-soft: a failed render or caption never fails the
document; the vision cost is accounted separately (the ~5x multiplier must
be observable, never folded into totals).
"""

import base64
import hashlib
import json
import logging
from typing import Any, Protocol

import asyncpg
from prometheus_client import Counter

from app.config import Settings
from app.costs import compute_cost_usd
from app.timeouts import with_hard_timeout

logger = logging.getLogger(__name__)

# Caption-cache key component: bump when the caption prompt changes.
CAPTION_PROMPT_VERSION = "v1"

_CAPTION_SYSTEM_PROMPT = (
    "You describe a document page image for a search index. State what the "
    "page shows — chart types, axes, labels, table columns, key figures and "
    "trends — in 3-6 factual sentences. Include every number that is legible "
    "and significant. Reply with the description only."
)

MULTIMODAL_EVENTS = Counter(
    "rag_multimodal_events_total",
    "Multimodal ingestion events (CE-7)",
    ["event"],  # page_rendered | render_failed | caption_hit | caption_miss | caption_fallback
)

_STORE_IMAGE_SQL = """
INSERT INTO page_images (document_id, page, content_hash, mime_type, image)
VALUES ($1, $2, $3, $4, $5)
ON CONFLICT (document_id, page) DO UPDATE SET
    content_hash = EXCLUDED.content_hash,
    mime_type = EXCLUDED.mime_type,
    image = EXCLUDED.image
RETURNING id
"""

_FETCH_IMAGES_SQL = """
SELECT id, page, content_hash, mime_type FROM page_images
WHERE document_id = $1 ORDER BY page
"""

_FETCH_IMAGE_BYTES_SQL = """
SELECT id, document_id, page, content_hash, mime_type, image FROM page_images
WHERE id = ANY($1::uuid[])
"""

_CAPTION_LOOKUP_SQL = """
SELECT image_hash, caption FROM caption_cache
WHERE model = $1 AND prompt_version = $2 AND image_hash = ANY($3::text[])
"""

_CAPTION_UPSERT_SQL = """
INSERT INTO caption_cache (image_hash, model, prompt_version, caption)
VALUES ($1, $2, $3, $4)
ON CONFLICT (image_hash, model, prompt_version) DO NOTHING
"""


class SupportsComplete(Protocol):
    model: str

    async def complete(self, messages: list[Any]) -> tuple[str, dict[str, int] | None]: ...


# ------------------------------------------------------------- detection
def detect_vision_pages(
    raw_pdf: bytes,
    *,
    min_text_chars: int,
    render_scale: float,
    max_pages: int,
) -> list[dict[str, Any]]:
    """Vision-worthy pages rendered to PNG (sync — run in a thread).

    Worthy = extractable text below `min_text_chars` AND the page carries at
    least one embedded image object (raster graphics; a purely vector-drawn
    chart with rich text around it is served by the text path). Capped at
    `max_pages` per document (cost bound, logged when hit)."""
    import pypdfium2 as pdfium

    document = pdfium.PdfDocument(raw_pdf)
    try:
        rendered: list[dict[str, Any]] = []
        for index in range(len(document)):
            page = document[index]
            try:
                textpage = page.get_textpage()
                text_chars = len(textpage.get_text_bounded() or "")
                has_image = any(
                    obj.type == pdfium.raw.FPDF_PAGEOBJ_IMAGE
                    for obj in page.get_objects()
                )
                if text_chars >= min_text_chars or not has_image:
                    continue
                if len(rendered) >= max_pages:
                    logger.info(
                        "multimodal.page_cap_hit %s",
                        json.dumps({"maxPages": max_pages, "page": index}),
                    )
                    break
                bitmap = page.render(scale=render_scale)
                buffer = _png_bytes(bitmap)
                rendered.append(
                    {
                        "page": index,
                        "image": buffer,
                        "content_hash": hashlib.sha256(buffer).hexdigest(),
                    }
                )
                MULTIMODAL_EVENTS.labels(event="page_rendered").inc()
            except Exception:
                logger.warning("multimodal.render_failed page=%s", index, exc_info=True)
                MULTIMODAL_EVENTS.labels(event="render_failed").inc()
            finally:
                page.close()
        return rendered
    finally:
        document.close()


def _png_bytes(bitmap: Any) -> bytes:
    import io

    image = bitmap.to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------- storage
class PageImagesRepository:
    """asyncpg persistence over the Drizzle-owned `page_images` table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def store(self, *, document_id: str, pages: list[dict[str, Any]]) -> int:
        if not pages:
            return 0
        conn = await self._connect()
        try:
            for entry in pages:
                await conn.execute(
                    _STORE_IMAGE_SQL,
                    document_id,
                    entry["page"],
                    entry["content_hash"],
                    "image/png",
                    entry["image"],
                )
        finally:
            await conn.close()
        return len(pages)

    async def list_for_document(self, document_id: str) -> list[dict[str, Any]]:
        """Image identities (no bytes) for the caption/index step."""
        conn = await self._connect()
        try:
            rows = await conn.fetch(_FETCH_IMAGES_SQL, document_id)
        finally:
            await conn.close()
        return [
            {
                "id": str(row["id"]),
                "page": row["page"],
                "content_hash": row["content_hash"],
                "mime_type": row["mime_type"],
            }
            for row in rows
        ]

    async def fetch_bytes(self, image_ids: list[str]) -> dict[str, dict[str, Any]]:
        """Full rows including bytes (CE-8 vision path + captioning)."""
        if not image_ids:
            return {}
        conn = await self._connect()
        try:
            rows = await conn.fetch(_FETCH_IMAGE_BYTES_SQL, image_ids)
        finally:
            await conn.close()
        return {
            str(row["id"]): {
                "id": str(row["id"]),
                "document_id": str(row["document_id"]),
                "page": row["page"],
                "content_hash": row["content_hash"],
                "mime_type": row["mime_type"],
                "image": bytes(row["image"]),
            }
            for row in rows
        }


class CaptionCacheRepository:
    """asyncpg persistence over the Drizzle-owned `caption_cache` table."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _connect(self) -> asyncpg.Connection:
        return await with_hard_timeout(
            asyncpg.connect(dsn=self._settings.database_url),
            seconds=self._settings.db_timeout_seconds,
            operation="postgres.connect",
        )

    async def fetch(
        self, *, model: str, prompt_version: str, hashes: list[str]
    ) -> dict[str, str]:
        if not hashes:
            return {}
        conn = await self._connect()
        try:
            rows = await conn.fetch(_CAPTION_LOOKUP_SQL, model, prompt_version, hashes)
        finally:
            await conn.close()
        return {row["image_hash"]: row["caption"] for row in rows}

    async def store(
        self, *, model: str, prompt_version: str, entries: list[tuple[str, str]]
    ) -> None:
        if not entries:
            return
        conn = await self._connect()
        try:
            for image_hash, caption in entries:
                await conn.execute(
                    _CAPTION_UPSERT_SQL, image_hash, model, prompt_version, caption
                )
        finally:
            await conn.close()


# -------------------------------------------------------------- captioning
def image_message(mime_type: str, image: bytes, prompt: str) -> list[dict[str, Any]]:
    """OpenAI vision content parts for one image + instruction."""
    encoded = base64.b64encode(image).decode()
    return [
        {"type": "text", "text": prompt},
        {
            "type": "image_url",
            "image_url": {"url": f"data:{mime_type};base64,{encoded}"},
        },
    ]


async def caption_images(
    *,
    images: list[dict[str, Any]],
    image_bytes: dict[str, dict[str, Any]],
    client: SupportsComplete,
    cache: CaptionCacheRepository,
    settings: Settings,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Captions per image id, cache-first on the image hash (fail-soft).

    Returns ({image_id: caption}, stats {hits, misses, fallbacks, costUsd})."""
    if not images:
        return {}, {"hits": 0, "misses": 0, "fallbacks": 0, "costUsd": 0.0}
    hashes = [entry["content_hash"] for entry in images]
    cached = await cache.fetch(
        model=client.model,
        prompt_version=CAPTION_PROMPT_VERSION,
        hashes=list(dict.fromkeys(hashes)),
    )
    cost_total = 0.0
    captions: dict[str, str] = {}
    fresh: list[tuple[str, str]] = []
    misses = 0
    fallbacks = 0
    for entry in images:
        image_hash = entry["content_hash"]
        if image_hash in cached:
            captions[entry["id"]] = cached[image_hash]
            continue
        payload = image_bytes.get(entry["id"])
        if payload is None:
            fallbacks += 1
            continue
        misses += 1
        try:
            text, usage = await client.complete(
                [
                    {"role": "system", "content": _CAPTION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": image_message(
                            payload["mime_type"],
                            payload["image"],
                            "Describe this document page for retrieval.",
                        ),
                    },
                ]
            )
        except Exception:
            logger.warning(
                "multimodal.caption_failed image=%s", entry["id"], exc_info=True
            )
            fallbacks += 1
            continue
        cost = compute_cost_usd(settings, model=client.model, token_usage=usage)
        if cost is not None:
            cost_total += cost
        caption = text.strip()
        if caption:
            captions[entry["id"]] = caption
            cached[image_hash] = caption
            fresh.append((image_hash, caption))
        else:
            fallbacks += 1
    await cache.store(
        model=client.model, prompt_version=CAPTION_PROMPT_VERSION, entries=fresh
    )
    stats = {
        "hits": len([h for h in hashes if h in cached]) - len(fresh),
        "misses": misses,
        "fallbacks": fallbacks,
        "costUsd": round(cost_total, 8),
    }
    MULTIMODAL_EVENTS.labels(event="caption_hit").inc(stats["hits"])
    MULTIMODAL_EVENTS.labels(event="caption_miss").inc(stats["misses"])
    MULTIMODAL_EVENTS.labels(event="caption_fallback").inc(stats["fallbacks"])
    logger.info("multimodal.caption_stats %s", json.dumps(stats))
    return captions, stats


def build_vision_client(settings: Settings, bucket: Any = None) -> SupportsComplete:
    """Vision-capable chat client (VISION_MODEL), CP-4 bucket-gated."""
    from app.providers import OpenAIChatClient

    client: Any = OpenAIChatClient(settings, model=settings.vision_model)
    if bucket is not None:
        from app.provider_bucket import ThrottledChatClient

        client = ThrottledChatClient(client, bucket)
    return client


def caption_chunk_rows(
    *,
    document: dict[str, Any],
    images: list[dict[str, Any]],
    captions: dict[str, str],
) -> list[dict[str, Any]]:
    """Synthetic caption chunks for the standard index path (IN-2 upsert).

    Chunk index space 200000+ keeps them clear of text chunks and RT-6
    parents; the chunk hash binds (document, page, image hash, caption) so an
    unchanged caption is upsert-idempotent and a changed one replaces cleanly
    via the stale-hash cleanup."""
    rows: list[dict[str, Any]] = []
    for entry in images:
        caption = captions.get(entry["id"])
        if not caption:
            continue
        chunk_hash = hashlib.sha256(
            f"caption:{document['content_hash']}:{entry['page']}:"
            f"{entry['content_hash']}:{CAPTION_PROMPT_VERSION}:{caption}".encode()
        ).hexdigest()
        rows.append(
            {
                "content": caption,
                "metadata": {
                    "modality": "image",
                    "page": entry["page"],
                    "imageId": entry["id"],
                    "imageHash": entry["content_hash"],
                },
                "content_hash": chunk_hash,
                "chunk_index": 200_000 + entry["page"],
            }
        )
    return rows
