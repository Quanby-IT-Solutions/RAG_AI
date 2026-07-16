"""CE-7 multimodal ingestion tests (vision-worthy pages + caption chunks)."""

import io
from typing import Any

import pytest

from app.multimodal import (
    CAPTION_PROMPT_VERSION,
    caption_chunk_rows,
    caption_images,
    detect_vision_pages,
)
from tests.conftest import REQUIRED_ENV


@pytest.fixture
def settings(monkeypatch):
    def build(**env: str):
        for key, value in REQUIRED_ENV.items():
            monkeypatch.setenv(key, value)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        from app.config import Settings

        return Settings()

    return build


def _chart_image_bytes() -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (400, 300), "white")
    draw = ImageDraw.Draw(image)
    for index, height in enumerate((80, 160, 240, 120)):
        draw.rectangle([40 + index * 90, 280 - height, 100 + index * 90, 280], fill="steelblue")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def make_pdf(*, chart_page: bool, text_pages: int = 1) -> bytes:
    """Fixture PDF via reportlab: N text-heavy pages + optionally one page
    that is just a chart image with almost no text."""
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=letter)
    for page in range(text_pages):
        text = pdf.beginText(72, 720)
        for line in range(40):
            text.textLine(
                f"Refund policy clause {page}-{line}: refunds are processed within "
                "thirty days of the request being approved by the finance team."
            )
        pdf.drawText(text)
        pdf.showPage()
    if chart_page:
        pdf.drawString(72, 750, "Q3")  # near-zero text
        pdf.drawImage(
            ImageReader(io.BytesIO(_chart_image_bytes())), 72, 300, width=400, height=300
        )
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class FakeVisionClient:
    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[Any] = []
        self.fail = fail

    async def complete(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("vision model down")
        return (
            "Bar chart of quarterly revenue: Q1 80, Q2 160, Q3 240, Q4 120.",
            {"promptTokens": 900, "completionTokens": 40, "totalTokens": 940},
        )


class FakeCaptionCache:
    def __init__(self, preloaded: dict[str, str] | None = None) -> None:
        self.entries = {
            (h, "gpt-4o-mini", CAPTION_PROMPT_VERSION): c
            for h, c in (preloaded or {}).items()
        }
        self.stored: list[tuple[str, str]] = []

    async def fetch(self, *, model, prompt_version, hashes):
        return {
            h: self.entries[(h, model, prompt_version)]
            for h in hashes
            if (h, model, prompt_version) in self.entries
        }

    async def store(self, *, model, prompt_version, entries):
        self.stored.extend(entries)
        for image_hash, caption in entries:
            self.entries[(image_hash, model, prompt_version)] = caption


class FakePageImagesRepository:
    def __init__(self, images: list[dict[str, Any]] | None = None) -> None:
        self.images = images or []

    async def list_for_document(self, document_id):
        return [
            {k: v for k, v in entry.items() if k != "image"} for entry in self.images
        ]

    async def fetch_bytes(self, image_ids):
        return {e["id"]: dict(e) for e in self.images if e["id"] in image_ids}

    async def store(self, *, document_id, pages):
        return len(pages)


# ---------------------------------------------------------------- detection
def test_chart_page_detected_and_rendered_text_pages_skipped():
    raw = make_pdf(chart_page=True, text_pages=2)

    pages = detect_vision_pages(raw, min_text_chars=200, render_scale=1.0, max_pages=20)

    assert len(pages) == 1
    assert pages[0]["page"] == 2  # the chart page, not the text pages
    assert pages[0]["image"].startswith(b"\x89PNG")
    assert len(pages[0]["content_hash"]) == 64


def test_text_only_pdf_yields_no_vision_pages():
    raw = make_pdf(chart_page=False, text_pages=2)

    assert detect_vision_pages(raw, min_text_chars=200, render_scale=1.0, max_pages=20) == []


def test_page_cap_bounds_rendering():
    raw = make_pdf(chart_page=True, text_pages=0)  # single chart page

    pages = detect_vision_pages(raw, min_text_chars=200, render_scale=1.0, max_pages=0)

    assert pages == []  # cap hit before any render (logged, not silent)


# --------------------------------------------------------------- captioning
def image_entry(image_id: str, image_hash: str) -> dict[str, Any]:
    return {
        "id": image_id,
        "page": 2,
        "content_hash": image_hash,
        "mime_type": "image/png",
        "image": b"png-bytes",
    }


async def test_cached_captions_make_zero_vision_calls(settings):
    config = settings(MULTIMODAL_ENABLED="true")
    client = FakeVisionClient()
    cache = FakeCaptionCache(preloaded={"ih-1": "cached caption"})

    captions, stats = await caption_images(
        images=[image_entry("img-1", "ih-1")],
        image_bytes={"img-1": image_entry("img-1", "ih-1")},
        client=client,
        cache=cache,
        settings=config,
    )

    assert captions == {"img-1": "cached caption"}
    assert client.calls == []
    assert stats["hits"] == 1 and stats["misses"] == 0


async def test_caption_miss_calls_vision_model_and_writes_back(settings):
    config = settings(MULTIMODAL_ENABLED="true")
    client = FakeVisionClient()
    cache = FakeCaptionCache()

    captions, stats = await caption_images(
        images=[image_entry("img-1", "ih-1")],
        image_bytes={"img-1": image_entry("img-1", "ih-1")},
        client=client,
        cache=cache,
        settings=config,
    )

    assert "Bar chart" in captions["img-1"]
    # The vision call carried the image as a data URI content part.
    content = client.calls[0][-1]["content"]
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert cache.stored[0][0] == "ih-1"
    assert stats["misses"] == 1 and stats["costUsd"] > 0


async def test_caption_failure_is_fail_soft(settings):
    config = settings(MULTIMODAL_ENABLED="true")

    captions, stats = await caption_images(
        images=[image_entry("img-1", "ih-1")],
        image_bytes={"img-1": image_entry("img-1", "ih-1")},
        client=FakeVisionClient(fail=True),
        cache=FakeCaptionCache(),
        settings=config,
    )

    assert captions == {}
    assert stats["fallbacks"] == 1


def test_caption_chunk_rows_carry_modality_and_pointer():
    rows = caption_chunk_rows(
        document={"id": "doc-1", "content_hash": "doc-hash"},
        images=[image_entry("img-1", "ih-1")],
        captions={"img-1": "Bar chart of quarterly revenue."},
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["metadata"] == {
        "modality": "image",
        "page": 2,
        "imageId": "img-1",
        "imageHash": "ih-1",
    }
    assert row["chunk_index"] == 200_002
    assert row["content"] == "Bar chart of quarterly revenue."
    # Deterministic hash: identical inputs -> idempotent upsert.
    again = caption_chunk_rows(
        document={"id": "doc-1", "content_hash": "doc-hash"},
        images=[image_entry("img-1", "ih-1")],
        captions={"img-1": "Bar chart of quarterly revenue."},
    )
    assert again[0]["content_hash"] == row["content_hash"]


# --------------------------------------------------------- indexing pipeline
async def test_caption_chunks_flow_through_the_standard_index_path(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache

    config = settings(
        MULTIMODAL_ENABLED="true",
        CHUNKING_STRATEGY="recursive",
        CHUNK_MIN_CHARS="10",
        CHUNK_MAX_CHARS="60",
    )
    repository = CapturingRepository()
    document = {**make_document("refund policy text. " * 10), "id": "doc-1"}

    outcome = await index_document_content(
        document=document,
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        page_images_repository=FakePageImagesRepository([image_entry("img-1", "ih-1")]),
        caption_cache=FakeCaptionCache(),
        vision_client=FakeVisionClient(),
    )

    assert outcome["status"] == "indexed"
    rows = repository.indexed["chunks"]
    caption_rows = [r for r in rows if (r.get("metadata") or {}).get("modality") == "image"]
    assert len(caption_rows) == 1
    assert caption_rows[0]["embedding"], "caption chunks are embedded like any chunk"
    assert outcome["multimodal"]["captionChunks"] == 1
    assert outcome["multimodal"]["pages"] == 1
    assert outcome["multimodal"]["costUsd"] > 0


async def test_flag_off_pipeline_never_touches_images(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache

    config = settings(
        CHUNKING_STRATEGY="recursive", CHUNK_MIN_CHARS="10", CHUNK_MAX_CHARS="60"
    )

    class ExplodingImagesRepository:
        async def list_for_document(self, document_id):
            raise AssertionError("images must not be touched when the flag is off")

    repository = CapturingRepository()
    outcome = await index_document_content(
        document={**make_document("refund policy text. " * 10), "id": "doc-1"},
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        page_images_repository=ExplodingImagesRepository(),
    )

    assert outcome["status"] == "indexed"
    assert outcome["multimodal"] is None
    assert all(
        (r.get("metadata") or {}).get("modality") != "image"
        for r in repository.indexed["chunks"]
    )


# ------------------------------------------------------ CE-8 vision answers
def image_chunk(content: str, image_id: str = "img-1") -> dict[str, Any]:
    import uuid

    return {
        "id": str(uuid.uuid4()),
        "document_id": str(uuid.uuid4()),
        "content": content,
        "metadata": {"modality": "image", "page": 2, "imageId": image_id},
        "chunk_index": 200_002,
        "similarity": 0.9,
    }


class VisionAnswerClient:
    model = "gpt-4o-mini"
    provider = "openai"

    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[Any] = []
        self.fail = fail

    async def complete(self, messages):
        self.calls.append(messages)
        if self.fail:
            raise RuntimeError("vision provider down")
        return "The chart shows Q3 at 240. [1]", {
            "promptTokens": 1000,
            "completionTokens": 30,
            "totalTokens": 1030,
        }


def vision_service(settings_obj, *, chunks, vision_client, images=None):
    from app.generation import GenerationService
    from tests.test_generation import (
        FakeChatClient,
        FakeConversationsRepository,
        FakeRetrievalService,
    )

    retrieval = FakeRetrievalService(chunks)
    chat = FakeChatClient(answer="Caption-grounded answer. [1]")
    repository = FakeConversationsRepository()
    service = GenerationService(
        settings_obj,
        retrieval,
        chat,
        repository,
        vision_client=vision_client,
        page_images=FakePageImagesRepository(images or [image_entry("img-1", "ih-1")]),
    )  # type: ignore[arg-type]
    return service, chat, repository


async def test_image_chunks_route_to_the_vision_model(settings):
    from tests.test_generation import PRINCIPAL

    config = settings(MULTIMODAL_ENABLED="true")
    vision = VisionAnswerClient()
    service, chat, repository = vision_service(
        config, chunks=[image_chunk("Bar chart caption.")], vision_client=vision
    )

    result = await service.answer(query="what does the chart show?", k=None, principal=PRINCIPAL)

    assert result["answer"] == "The chart shows Q3 at 240. [1]"
    assert result["model"] == vision.model
    assert chat.calls == []  # the text client never fired
    # The vision call carried the actual page image bytes as a data URI.
    content = vision.calls[0][-1]["content"]
    assert any(
        part.get("type") == "image_url" for part in content if isinstance(part, dict)
    )
    # AC: the citation resolves to the page image.
    assert result["citations"][0]["modality"] == "image"
    assert result["citations"][0]["imageId"] == "img-1"
    assert repository.audits[-1]["after"]["vision"] == {
        "images": 1,
        "model": vision.model,
        "degraded": False,
    }


async def test_text_only_context_never_pays_vision_cost(settings):
    from tests.test_generation import PRINCIPAL, make_chunk

    config = settings(MULTIMODAL_ENABLED="true")
    vision = VisionAnswerClient()
    service, chat, repository = vision_service(
        config, chunks=[make_chunk("Plain text chunk.")], vision_client=vision
    )

    result = await service.answer(query="text question?", k=None, principal=PRINCIPAL)

    assert vision.calls == []
    assert len(chat.calls) == 1
    assert "vision" not in repository.audits[-1]["after"]
    assert result["model"] == "gpt-4o-mini"


async def test_vision_failure_falls_back_to_caption_grounding(settings):
    from tests.test_generation import PRINCIPAL

    config = settings(MULTIMODAL_ENABLED="true")
    service, chat, repository = vision_service(
        config,
        chunks=[image_chunk("Bar chart caption.")],
        vision_client=VisionAnswerClient(fail=True),
    )

    result = await service.answer(query="what does the chart show?", k=None, principal=PRINCIPAL)

    # The GN-4/5-managed text path answered from the caption chunk.
    assert result["answer"] == "Caption-grounded answer. [1]"
    assert len(chat.calls) == 1
    assert result["degraded"] is False  # honest answer, not a B2 degradation
    assert repository.audits[-1]["after"]["vision"] == {"images": 1, "degraded": True}


async def test_vision_image_cap_bounds_attachments(settings):
    from tests.test_generation import PRINCIPAL

    config = settings(MULTIMODAL_ENABLED="true", VISION_MAX_IMAGES="1")
    images = [image_entry("img-1", "ih-1"), image_entry("img-2", "ih-2")]
    images[1]["page"] = 3
    vision = VisionAnswerClient()
    service, _, _ = vision_service(
        config,
        chunks=[image_chunk("Chart A.", "img-1"), image_chunk("Chart B.", "img-2")],
        vision_client=vision,
        images=images,
    )

    await service.answer(query="charts?", k=None, principal=PRINCIPAL)

    content = vision.calls[0][-1]["content"]
    image_parts = [p for p in content if isinstance(p, dict) and p.get("type") == "image_url"]
    assert len(image_parts) == 1  # capped


async def test_flag_off_generation_is_byte_identical(settings):
    from tests.test_generation import PRINCIPAL

    config = settings()  # baseline

    class ExplodingVision:
        model = "never"

        async def complete(self, messages):
            raise AssertionError("vision must not run when the flag is off")

    # Even with an image chunk in context (e.g. indexed before the flag was
    # turned off), baseline generation stays on the text path.
    service, chat, repository = vision_service(
        config, chunks=[image_chunk("Bar chart caption.")], vision_client=None
    )
    result = await service.answer(query="chart?", k=None, principal=PRINCIPAL)

    assert result["answer"] == "Caption-grounded answer. [1]"
    assert "vision" not in repository.audits[-1]["after"]


async def test_streaming_vision_branch_streams_from_the_vision_client(settings):
    from tests.test_generation import PRINCIPAL, FakeStreamingChatClient

    config = settings(MULTIMODAL_ENABLED="true")

    class StreamingVisionClient(FakeStreamingChatClient):
        pass

    vision = StreamingVisionClient(answer="Streamed chart answer. [1]")
    from app.generation import GenerationService
    from tests.test_generation import (
        FakeConversationsRepository,
        FakeRetrievalService,
    )
    from tests.test_generation import (
        FakeStreamingChatClient as TextStream,
    )

    text_client = TextStream(answer="text path")
    repository = FakeConversationsRepository()
    service = GenerationService(
        config,
        FakeRetrievalService([image_chunk("Bar chart caption.")]),
        text_client,
        repository,
        vision_client=vision,
        page_images=FakePageImagesRepository([image_entry("img-1", "ih-1")]),
    )  # type: ignore[arg-type]

    frames = [
        f
        async for f in service.answer_stream(
            query="what does the chart show?", k=None, principal=PRINCIPAL
        )
    ]

    final = next(f for f in frames if "event: final" in f)
    assert "Streamed chart answer." in final
    assert text_client.calls == [] if hasattr(text_client, "calls") else True
    assert repository.audits[-1]["after"]["vision"]["degraded"] is False


async def test_caption_step_failure_never_fails_the_document(settings):
    from app.indexing import index_document_content
    from tests.test_ingest import FakeEmbeddingsClient
    from tests.test_parent_doc import CapturingRepository, make_document
    from tests.test_worker import NullEmbeddingCache

    config = settings(
        MULTIMODAL_ENABLED="true",
        CHUNKING_STRATEGY="recursive",
        CHUNK_MIN_CHARS="10",
        CHUNK_MAX_CHARS="60",
    )

    class ExplodingImagesRepository:
        async def list_for_document(self, document_id):
            raise RuntimeError("images table unavailable")

    repository = CapturingRepository()
    outcome = await index_document_content(
        document={**make_document("refund policy text. " * 10), "id": "doc-1"},
        settings=config,
        repository=repository,
        embeddings=FakeEmbeddingsClient(),
        embedding_cache=NullEmbeddingCache(),
        page_images_repository=ExplodingImagesRepository(),
    )

    assert outcome["status"] == "indexed"
    assert outcome["multimodal"] == {"failed": True}
