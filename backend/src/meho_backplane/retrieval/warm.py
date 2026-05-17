# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Build-time embedding-model warm/bake entrypoint (evoila/meho#574).

Run by ``backend/Dockerfile``'s runtime stage:

    python -m meho_backplane.retrieval.warm

It pre-downloads the shipped default fastembed model
(:data:`~meho_backplane.retrieval.embedding.DEFAULT_EMBEDDING_MODEL`)
into an **image layer** at
:data:`~meho_backplane.retrieval.embedding.BAKED_MODEL_CACHE_DIR`. Two
problems are solved at once:

* **Out-of-the-box startup (#574).** The chart used to rely on a
  runtime HuggingFace download into a *persistent RWO PVC*. fastembed
  treats an existing snapshot directory as "model present" and never
  re-fetches, so a single interrupted/partial first download (or a CSI
  volume that doesn't preserve the HF cache's ``snapshots/<sha>/*.onnx
  -> ../../blobs/<hash>`` symlinks) poisons that PVC permanently and
  *every* subsequent fresh pod CrashLoops identically with
  ``onnxruntime ... NO_SUCHFILE ... model_optimized.onnx``. Baking the
  model into an image layer removes the PVC from the correctness path
  for the shipped default entirely.

* **Air-gap (the #572 half).** First boot no longer needs HuggingFace
  egress.

This module is *also* the regression guard the reporter asked for:
the build ``RUN`` fails (non-zero exit, image build aborts) if
fastembed and the pinned model repo ever drift such that the default
model cannot actually be instantiated and produce a correctly-shaped
vector. Green ``ci.yml`` on ``main`` does not catch a runtime-fetched
model; this does, at build time, deterministically.

Exit codes: ``0`` on a successful load that yields a non-empty
:data:`~meho_backplane.retrieval.embedding.EMBEDDING_DIMENSION`-wide
vector; ``1`` (with a legible single-line reason on stderr) otherwise.
"""

from __future__ import annotations

import os
import sys

from meho_backplane.retrieval.embedding import (
    BAKED_MODEL_CACHE_DIR,
    DEFAULT_EMBEDDING_MODEL,
    EMBEDDING_DIMENSION,
)

#: Env override for the bake target. The Dockerfile build does NOT set
#: this, so the image bakes at :data:`BAKED_MODEL_CACHE_DIR` (the
#: read-only path the runtime defaults to). CI runs this same module as
#: the per-PR drift guard but the gha-runner-scale-set sandbox can't
#: write ``/opt`` — it sets ``MEHO_WARM_CACHE_DIR`` to a writable temp
#: dir so the *assertion* still runs without building the image.
_WARM_CACHE_DIR_ENV = "MEHO_WARM_CACHE_DIR"


def main() -> int:
    """Bake + assert the default embedding model. Return a process exit code."""
    # Local import: fastembed transitively pulls onnxruntime, which does
    # its own import-time probing. Keeping it in the function body mirrors
    # EmbeddingService._ensure_loaded and keeps `python -m ... --help`-style
    # introspection cheap.
    from fastembed import TextEmbedding

    cache_dir = os.environ.get(_WARM_CACHE_DIR_ENV, BAKED_MODEL_CACHE_DIR)
    print(
        f"[warm] baking {DEFAULT_EMBEDDING_MODEL!r} -> {cache_dir!r}",
        flush=True,
    )
    try:
        model = TextEmbedding(
            model_name=DEFAULT_EMBEDDING_MODEL,
            cache_dir=cache_dir,
        )
        # One real embed: proves the onnxruntime session is loadable AND
        # the artifact is functionally intact, not merely present on disk.
        vector = list(next(iter(model.embed(["meho embedding warm check"]))))
    except Exception as exc:  # build-time guard: surface anything
        print(
            f"[warm] FAILED to load embedding model "
            f"{DEFAULT_EMBEDDING_MODEL!r} into {cache_dir!r}: "
            f"{type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )
        return 1

    dim = len(vector)
    if dim != EMBEDDING_DIMENSION:
        print(
            f"[warm] FAILED: {DEFAULT_EMBEDDING_MODEL!r} produced a "
            f"{dim}-dim vector, expected {EMBEDDING_DIMENSION} "
            f"(documents.embedding is hard-pinned to vector"
            f"({EMBEDDING_DIMENSION}) by migration 0003)",
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        f"[warm] OK {DEFAULT_EMBEDDING_MODEL!r} dim={dim} baked at {cache_dir!r}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
