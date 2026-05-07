# Knowledge Base

> Last verified: 0.1.0

MEHO's knowledge system lets operators upload documents that become searchable by the agent during investigations. Documents go through a CPU-only pipeline (no PyTorch, no GPU, no sidecars) that extracts markdown, splits it into heading-aware chunks, embeds the chunks via [fastembed](https://qdrant.github.io/fastembed/) (ONNX in-process), and stores them in PostgreSQL with pgvector.

This is the preview retrieval stack. A future opt-in to MEHO.Knowledge will move embedding + cross-encoder reranking out-of-process to a dedicated service; until then, MEHO.X stays single-container.

## Document Processing Pipeline

```
Upload → Format Detection → Lightweight Conversion → Heading-Aware Chunking
       → Summary Generation → fastembed (ONNX) → Storage
```

### Stage 1: Format Detection

The uploader detects the MIME type from the URL extension and the file's magic bytes. Supported formats are listed in [Supported Formats](#supported-formats).

### Stage 2: Conversion

The lightweight converter (`meho_app.modules.knowledge.lightweight_converter.LightweightDocumentConverter`) extracts markdown text + per-page metadata using:

| Format | Library | Capabilities |
|--------|---------|-------------|
| PDF | pymupdf4llm + pdfplumber | Text extraction with layout preservation, table detection and markdown formatting, OCR fallback via RapidOCR for scanned pages |
| DOCX | python-docx | Heading structure, paragraphs, tables |
| HTML | BeautifulSoup | Semantic structure extraction |

The whole pipeline fits in ~250 MB of dependencies, no PyTorch.

### Stage 3: Heading-Aware Chunking

`meho_app.modules.knowledge.chunking.TextChunker` splits the markdown into chunks bounded by **512 words** with **64-word tail overlap** for context continuity. Chunks are paragraph-aware (split on blank lines) and the **ATX heading hierarchy** (`#` through `######`) is tracked, so each chunk carries its hierarchical heading path (e.g., `["Chapter 3", "Networking", "DNS Configuration"]`) as metadata.

### Stage 4: Document Summary Generation

An LLM generates a 1–2 sentence summary of each document. The summary is prepended to every chunk as additional context for the embedding model. Generation runs with a 15-second timeout; on failure, ingestion continues without a summary.

### Stage 5: Embedding and Storage

Each chunk's "retrieval text" (heading path + summary prefix + chunk content) is embedded by **fastembed** running `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-D, multilingual, ~220 MB ONNX). The model is downloaded from Hugging Face Hub on first use and cached on disk under `FASTEMBED_CACHE_DIR` (default `/var/cache/fastembed`, persisted via the `fastembed_cache` Docker volume). Embedding runs in the API process — no HTTP hop, no separate container.

Vectors land in PostgreSQL with `pgvector` at `Vector(384)`. BM25 lexical retrieval runs on top of the same chunk text via a Redis-backed index ([`bm25_service.py`](https://github.com/evoila-bosnia/MEHO.X/blob/main/meho_app/modules/knowledge/bm25_service.py)).

## Supported Formats

| Format | Engine | Notes |
|--------|--------|-------|
| PDF | pymupdf4llm + pdfplumber + RapidOCR | Rule-based layout, adequate for most docs; OCR for scanned pages |
| DOCX | python-docx | Heading structure preserved |
| HTML | BeautifulSoup | Semantic structure extraction |
| Plain text | TextChunker | Heading-aware Markdown chunking |
| URL | Lightweight (HTML/PDF) or TextChunker | Routed by Content-Type |

## Search

Knowledge search runs **hybrid retrieval** that fuses two ranked candidate lists:

- **BM25** — Redis-cached lexical search via `rank_bm25` + Porter stemmer. Excels at exact-term queries (model numbers, error codes, configuration keys).
- **Vector** — pgvector cosine similarity over the fastembed MiniLM-L12 embeddings. Good baseline for paraphrased / semantic queries.

The two lists are fused with **reciprocal rank fusion** (`k=60`, weighted by `bm25_weight` / `semantic_weight`).

There is **no cross-encoder reranker in the preview path** — it returns when MEHO.Knowledge takes over remote retrieval and brings back a heavier (multilingual) cross-encoder. The `KnowledgeService.search_with_rerank` and `adaptive_search` entry points still exist; they delegate to the plain hybrid retrieval flow so callers stay backwards-compatible.

### Three-Tier Scoping

Knowledge is scoped at three levels:

- **Global** — Available to all connectors and investigations.
- **Connector type** — Available to all connectors of a specific type (e.g., all Kubernetes connectors share Kubernetes documentation).
- **Connector instance** — Available only to a specific connector instance.

This scoping provides day-one value: upload a Kubernetes troubleshooting guide once, and it is immediately available to every Kubernetes connector.

## Configuration

| Setting | Description |
|---------|-------------|
| `MEHO_FEATURE_KNOWLEDGE` | Set to `false` to disable the knowledge module entirely. |
| `FASTEMBED_EMBEDDING_MODEL` | fastembed model name. Default: `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Must be one of the models in fastembed's catalog. Changing this typically requires a new Alembic migration to match the embedding dimension. |
| `FASTEMBED_CACHE_DIR` | Directory where fastembed caches downloaded ONNX weights. Default: `/var/cache/fastembed`. Persist via a Docker volume to skip the first-boot download on subsequent runs. |
