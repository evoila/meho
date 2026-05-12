# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Retrieval substrate - the shared write + read path G4 (#215, knowledge)
and G5 (#216, memory) both consume.

Initiative #225 (G0.4 Retrieval substrate) lands this package over six
tasks. The substrate is one embedding pipeline + one ``documents`` table
+ one retrieval implementation, so the model-choice decision is made
once and both consuming Goals get the same bug fixes / recall numbers /
operator-visible cost graph.

Module map (tasks land in order):

* :mod:`meho_backplane.retrieval.embedding` (G0.4-T2, #259) - the
  fastembed-backed :class:`EmbeddingService` plus its singleton
  factory. Loaded eagerly by ``main.py``'s lifespan so first-request
  latency doesn't absorb the ~1-2 s ONNX model load cost.
* :mod:`meho_backplane.retrieval.indexer` (G0.4-T3, #260) - the
  ``index_document`` helper with hash-based skip-re-embedding. Lands
  in T3.
* :mod:`meho_backplane.retrieval.retriever` (G0.4-T4, #261) - the
  ``retrieve`` helper with hybrid BM25 + cosine + RRF fusion. Lands
  in T4.

The HTTP surface (``POST /api/v1/retrieve``, G0.4-T5 #262) lives in
:mod:`meho_backplane.api.v1.retrieve` rather than here - the API
package is the public-route convention; the retrieval helpers stay
internal-import.
"""
