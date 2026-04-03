# Knowledge Base

> Last verified: v2.3 (Phase 101)

MEHO's knowledge system lets operators upload documents that become searchable by the agent during investigations. Documents are processed through a structure-aware pipeline using [IBM Docling](https://github.com/DS4SD/docling) for intelligent chunking that preserves document hierarchy.

## Document Processing Pipeline

When a document is uploaded, MEHO processes it through a multi-stage pipeline:

```
Upload --> Docling Conversion --> TOC Filtering --> HybridChunker
    --> Heading Path Enrichment --> Summary Generation --> Embedding --> Storage
```

### Stage 1: Document Conversion

Docling converts uploaded files into a structured `DoclingDocument` representation that preserves headings, sections, tables, lists, and other document elements with their semantic labels.

### Stage 2: TOC and Noise Filtering

Before chunking, the pipeline filters out noise elements that would pollute search results. The following element types are excluded:

- **Document Index** (table of contents entries)
- **Page Headers** (repeated header text)
- **Page Footers** (repeated footer text)

A chunk is excluded only if *all* of its source elements have excluded labels. Mixed chunks (some content + some noise) are preserved.

### Stage 3: Structure-Aware Chunking

The `HybridChunker` creates semantic chunks that respect document structure. Unlike naive text splitting, HybridChunker:

- Keeps related paragraphs together within heading sections.
- Merges small peer elements (adjacent paragraphs under the same heading).
- Respects a configurable token limit per chunk (default: 512 tokens).

### Stage 4: Heading Path Enrichment

Each chunk is enriched with its heading path -- the hierarchical chain of headings leading to the chunk's location in the document. For example, a chunk under "Chapter 3 > Networking > DNS Configuration" carries that full path as context.

This heading path is prepended to the chunk text before embedding, ensuring the embedding vector captures the chunk's position in the document hierarchy -- not just its content.

### Stage 5: Document Summary Generation

An LLM generates a 1-2 sentence summary of each document, focusing on what systems, technologies, or procedures it covers. This summary is prepended to every chunk as additional context for the embedding model.

The summary generation uses the first ~16,000 characters of the document (covering the title, table of contents, and introduction) and runs with a 15-second timeout. If generation fails, ingestion continues without a summary.

### Stage 6: Embedding and Storage

Enriched chunks (heading path + summary prefix + chunk content) are embedded using the configured embedding provider and stored in PostgreSQL with pgvector for semantic search and GIN indexes for BM25 full-text search.

## Lightweight Pipeline (CPU-Only)

> Added in v2.3 (Phase 98)

When `MEHO_FEATURE_USE_DOCLING=false`, MEHO uses a lightweight document processing pipeline that requires no PyTorch, GPU, or ML models. This pipeline is ideal for:

- **Open-source deployments** where GPU resources are unavailable
- **Slim Docker images** (~500 MB vs ~4 GB with Docling)
- **Development environments** where fast startup matters more than maximum extraction quality

### Pipeline Architecture

```
Upload --> Format Detection --> Handler Selection --> Markdown Extraction
    --> TextChunker (heading-aware) --> Embedding --> Storage
```

### Format Handlers

| Format | Library | Capabilities |
|--------|---------|-------------|
| PDF | pymupdf4llm + pdfplumber | Text extraction with layout preservation, table detection and markdown formatting, OCR fallback via RapidOCR for scanned pages |
| DOCX | python-docx | Heading structure, paragraphs, tables |
| HTML | BeautifulSoup | Semantic structure extraction |

### Quality Comparison

| Aspect | Docling (default) | Lightweight |
|--------|-------------------|-------------|
| PDF text extraction | ML-powered layout detection | pymupdf4llm heuristic layout |
| Table extraction | Deep learning table structure | pdfplumber rule-based detection |
| OCR (scanned PDFs) | Built-in ML OCR | RapidOCR (CPU-only) |
| Heading detection | Element classification model | Regex/font-size heuristic |
| Memory usage | 6-22 GB peak (per page accumulation) | ~250 MB total |
| Docker image size | ~4 GB (with PyTorch) | ~500 MB |
| GPU required | Optional (recommended) | No |

The lightweight pipeline produces the same output shape (markdown text + chunks) as Docling, so downstream processing (embedding, search, agent retrieval) works identically regardless of which pipeline is active.

## Supported Formats

| Format | Docling Engine | Lightweight Engine | Notes |
|--------|--------|---------|-------|
| PDF | Docling (ML layout) | pymupdf4llm + pdfplumber + RapidOCR | Lightweight: rule-based layout, adequate for most docs |
| DOCX | Docling | python-docx | Both preserve heading structure |
| HTML | Docling | BeautifulSoup | Both extract semantic structure |
| Plain text | TextChunker (fallback) | TextChunker (fallback) | Same path for both |
| URL | Docling (HTML/PDF) or TextChunker | Lightweight (HTML/PDF) or TextChunker | Routed by content type |

PDF is the primary optimized path. Docling's PDF processing provides the richest structural information, including element-type classification that enables TOC filtering and accurate heading path extraction. The lightweight pipeline provides adequate quality for most documents without requiring PyTorch or GPU resources.

## Search

Knowledge search uses a hybrid approach combining two retrieval methods:

- **Semantic search** -- pgvector cosine similarity on chunk embeddings finds conceptually related content even when exact terms differ.
- **Keyword search** -- PostgreSQL full-text search (BM25 via GIN index) finds exact term matches, important for technical identifiers, error codes, and configuration keys.

Results from both methods are merged, deduplicated, and optionally reranked using Voyage AI rerank-2.5 for 15-30% precision improvement. Each result includes the chunk content with its heading path context, helping the agent understand where in the document the information comes from.

### Three-Tier Scoping

Knowledge is scoped at three levels:

- **Global** -- Available to all connectors and investigations.
- **Connector type** -- Available to all connectors of a specific type (e.g., all Kubernetes connectors share Kubernetes documentation).
- **Connector instance** -- Available only to a specific connector instance.

This scoping provides day-one value: upload a Kubernetes troubleshooting guide once, and it is immediately available to every Kubernetes connector.

## Configuration

| Setting | Description |
|---------|-------------|
| `MEHO_FEATURE_KNOWLEDGE` | Set to `false` to disable the knowledge module entirely. |
| `EMBEDDING_MODEL` | Embedding model name (default: `voyage-4-large`). |
| `VOYAGE_API_KEY` | API key for Voyage AI embeddings (not needed if using local TEI). |
| `MEHO_FEATURE_USE_DOCLING` | Set to `false` to use the lightweight CPU-only pipeline instead of Docling. Default: `true`. |

## What Changed in v2.3

The v2.3 knowledge pipeline is a significant upgrade from the previous version:

- **Docling replaces pypdf** -- Structure-aware document conversion instead of raw text extraction. PDF processing now understands headings, sections, tables, and document hierarchy.
- **Heading path enrichment** -- Every chunk carries its hierarchical position in the document, improving search relevance for nested content.
- **TOC filtering** -- Table of contents entries, page headers, and page footers are filtered out before chunking, eliminating noise from search results.
- **Document summaries** -- LLM-generated summaries provide document-level context to every chunk embedding, improving cross-document retrieval.
- **HybridChunker** -- Docling's structure-aware chunker replaces fixed-size text splitting, producing more semantically coherent chunks.
- **Chunk prefix enrichment** -- Connector type, connector name, and document summary are prepended to each chunk before embedding for richer context.
- **Lightweight pipeline option** -- Setting `MEHO_FEATURE_USE_DOCLING=false` activates a CPU-only pipeline using pymupdf4llm, pdfplumber, and RapidOCR. No PyTorch or GPU required. Produces the same output shape as Docling for seamless downstream compatibility.
