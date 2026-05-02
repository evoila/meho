# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""Entrypoint for ephemeral ingestion worker: python -m meho_app.worker

Runs as a stateless container that processes a single document and exits.
All backends (K8s Job, Cloud Run Job, Docker, local subprocess) invoke
this same entrypoint. Process termination reclaims all Docling/PyTorch memory.
"""

import sys

from meho_app.worker.ingest import run_worker

if __name__ == "__main__":
    sys.exit(run_worker())
