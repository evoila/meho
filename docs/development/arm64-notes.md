# arm64 / Apple Silicon — first-run notes

Operational reference for MEHO on arm64 hosts. The previous TEI sidecar
deployment forced an x86_64-only image to run under Rosetta 2, which was
slow and memory-hungry on Apple Silicon. The retrieval stack now embeds
in-process via [fastembed](https://qdrant.github.io/fastembed/) (ONNX),
so every image in the stack is native arm64.

## Architecture support

| Service | Image | Native arm64? | Notes |
|---|---|---|---|
| postgres | `pgvector/pgvector:pg15` | yes | — |
| minio | `minio/minio:latest` | yes | — |
| redis | `redis/redis-stack-server:latest` | yes | — |
| keycloak | `quay.io/keycloak/keycloak:24.0` | yes | — |
| seq | `datalust/seq:latest` | yes | — |
| pgadmin | `dpage/pgadmin4:latest` | yes | `tools` profile |
| ollama | `ollama/ollama:latest` | yes | `ollama` profile |
| meho (backend) | built locally via `docker/Dockerfile.meho` | yes (BuildKit-native) | published multi-arch to `ghcr.io/evoila/meho-backend` (`linux/amd64`, `linux/arm64`) |
| meho-frontend | built locally via `docker/Dockerfile.meho-frontend` | yes (BuildKit-native) | — |

No Rosetta 2 enablement, no `platform: linux/amd64` directives, no
amd64-only images. `docker compose up` runs the full stack natively on
arm64.

## fastembed model download

On first request after a fresh start, fastembed lazy-loads its ONNX
model. The default
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` weighs
~220 MB and is fetched from Hugging Face Hub into the
`fastembed_cache` Docker volume. Subsequent boots reuse the cached
weights and the warm-up cost drops to a few hundred milliseconds.

To pre-warm the cache before exercising the API:

```bash
docker compose exec meho python -c "
from meho_app.modules.knowledge.embeddings import get_embedding_provider
import asyncio
asyncio.run(get_embedding_provider().embed_text('warmup'))
"
```

## CI coverage

The first-run path is guarded in CI by the `arm64-first-run` workflow
([`.github/workflows/arm64-first-run.yml`](../../.github/workflows/arm64-first-run.yml)).
It boots the full compose stack on `ubuntu-24.04-arm`, asserts backend
and frontend reachability, and detects the silent-crash failure modes
that previously appeared with broken cross-arch emulation. Runs on PRs
that touch the compose stack, Dockerfiles, or these notes; on a weekly
schedule (catches upstream image drift); and on manual dispatch.

The test source is
[`tests/integration/test_first_run_arm64.py`](../../tests/integration/test_first_run_arm64.py).

## References

- [docs/codebase/first-run-experience.md](../codebase/first-run-experience.md) — first-run experience overview
- [docs/troubleshooting.md](../troubleshooting.md) — `arm64 / Apple Silicon first-run notes` section
- [fastembed docs](https://qdrant.github.io/fastembed/) — ONNX-based embedding library
