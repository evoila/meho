# arm64 / Apple Silicon — first-run notes

Operational reference for MEHO on arm64 hosts. Covers which images are native,
which run under Rosetta 2 emulation, measured baselines for the Rosetta path,
and when to prefer Voyage over the local TEI fallback.

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
| **tei-embeddings** | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9` | **no — x86_64 only** | runs under Rosetta 2; `platform: linux/amd64` declared in compose |
| **tei-reranker** | same | **no — x86_64 only** | runs under Rosetta 2; `platform: linux/amd64` declared in compose |
| meho (backend) | built locally via `docker/Dockerfile.meho` | yes (BuildKit-native) | published multi-arch to `ghcr.io/evoila/meho-backend` (`linux/amd64`, `linux/arm64`) |
| meho-frontend | built locally via `docker/Dockerfile.meho-frontend` | yes (BuildKit-native) | — |

## Prerequisite: enable Rosetta 2

The TEI sidecars cannot start on arm64 without Rosetta 2 emulation. Enable it once:

1. Open **Docker Desktop > Settings > General**.
2. Set **Choose virtual machine manager** to **Apple Virtualization framework**. Rosetta is only available under this VMM.
3. Enable **Use Rosetta for x86_64/amd64 emulation on Apple Silicon**. This option is disabled by default.
4. Click **Apply & restart**.

With Rosetta enabled and the compose-level `platform: linux/amd64` directive in place (see `docker-compose.yml`), `docker compose --profile tei up` pulls the amd64 image and runs it under Rosetta transparently.

## Rosetta 2 baseline (measured)

Host: Apple M3 Pro, macOS 26.2, Docker Desktop 29.3.1, Rosetta 2 emulation
enabled, 36 GB RAM, baseline clean (no prior `meho_tei_models` volume, no
cached TEI image).

| Metric | Value | Notes |
|---|---|---|
| TEI embeddings image pull (amd64 manifest) | ~22 s | `ghcr.io/huggingface/text-embeddings-inference:cpu-1.9`, ~1.2 GB compressed |
| TEI embeddings first-boot (compose up → `/health` returns 200) | ~297 s | includes `BAAI/bge-m3` model download (~2 GB) and load into Rosetta-emulated process |
| **Total TEI embeddings cold-start on clean arm64** | **~320 s (~5m 20s)** | pull + model download + load + ready |
| TEI reranker first-boot (image cached, compose up → `/health`) | ~428 s (~7m 8s) | `BAAI/bge-reranker-v2-m3` download + load; image pull step skipped because embeddings reused the same image |
| TEI embeddings observed idle memory (post-load) | **~12.0 GiB** | `docker stats --no-stream meho-tei-embeddings-1` — point-in-time sample, not a true peak |
| TEI reranker observed idle memory (post-load) | ~4.6 GiB | same |
| Steady-state embed latency (median of 10 sequential calls, single short query) | **~190 ms** | `POST /embed` with `{"inputs":"...","normalize":true,"truncate":true}` |
| Cold-cache first embed latency | ~1550 ms | first call after model load; subsequent calls reach steady state within ~3 requests |

amd64 comparison is not provided — no x86_64 host was available at measurement time. Published numbers should be taken as the arm64-under-Rosetta operating envelope, not a relative comparison.

### Memory implications

The ~12 GiB observed idle memory for TEI embeddings under Rosetta is substantially higher
than the ~4 GiB Docker Desktop memory allocation suggested in the top-level
troubleshooting doc. On arm64 / Rosetta, plan for at least **16 GB** of Docker
Desktop memory allocation if running the full stack plus both TEI sidecars
simultaneously. The Rosetta translation layer and the amd64 model weights both
contribute to the inflated footprint.

### Reproducibility

The baseline above was captured on a clean machine with the following sequence:

```bash
# 0. Clean state — remove prior TEI volume/image if present
docker compose -p meho --profile tei stop tei-embeddings tei-reranker
docker compose -p meho --profile tei rm -f tei-embeddings tei-reranker
docker volume rm meho_tei_models || true
docker image rm ghcr.io/huggingface/text-embeddings-inference:cpu-1.9 || true

# 1. First-boot, embeddings
START=$(date +%s)
docker compose -p meho --profile tei up -d tei-embeddings
until curl -sf http://localhost:8090/health >/dev/null; do sleep 5; done
echo "embeddings ready in $(( $(date +%s) - START ))s"

# 2. First-boot, reranker (image now cached)
START=$(date +%s)
docker compose -p meho --profile tei up -d tei-reranker
until curl -sf http://localhost:8091/health >/dev/null; do sleep 5; done
echo "reranker ready in $(( $(date +%s) - START ))s"

# 3. Peak memory
docker stats --no-stream meho-tei-embeddings-1 meho-tei-reranker-1

# 4. Steady-state latency
for i in $(seq 1 10); do
  /usr/bin/time -p curl -sS -X POST http://localhost:8090/embed \
    -H 'content-type: application/json' \
    -d '{"inputs":"how much ram does the checkout-service use","normalize":true,"truncate":true}' \
    -o /dev/null 2>&1 | awk '/real/ {print $2}'
done | sort -n
# Median is the 5th value.

# 5. Cleanup
docker compose -p meho --profile tei stop tei-embeddings tei-reranker
docker compose -p meho --profile tei rm -f tei-embeddings tei-reranker
```

## CI coverage

The first-run TEI path is guarded in CI by the `arm64-first-run` workflow
([`.github/workflows/arm64-first-run.yml`](../../.github/workflows/arm64-first-run.yml)).
It boots the full compose stack on `ubuntu-24.04-arm`, asserts backend and
frontend reachability, and detects the silent-crash failure mode where amd64
containers fail under broken QEMU emulation. Runs on PRs that touch the
compose stack, Dockerfiles, or these notes; on a weekly schedule (catches
upstream image drift); and on manual dispatch.

The test source is
[`tests/integration/test_first_run_arm64.py`](../../tests/integration/test_first_run_arm64.py).

## When to pick Voyage vs. TEI on arm64

| Situation | Recommended path |
|---|---|
| Have or can create a Voyage AI account | **Voyage** — fastest, architecture-agnostic, recommended |
| No Voyage account, okay with ~5 min first boot | TEI under Rosetta — works, fully local, plan for ≥16 GB Docker memory |
| Air-gapped or policy-restricted environment | TEI under Rosetta — accept the emulation overhead |
| Running on x86_64 (Intel Mac, Linux, WSL) | Either — TEI is native, no Rosetta penalty |

## Known limitations

- **Rosetta overhead is not negligible.** Steady-state embedding latency is acceptable for evaluator workloads, but sustained high-throughput ingestion against the local TEI sidecar on arm64 will be slower than the same work on native amd64 hardware or against Voyage.
- **Memory ceiling.** Under Rosetta, the emulated TEI process reserves significantly more memory than the native image would. Docker Desktop's default memory allocation may be insufficient.
- **Native arm64 TEI is deferred.** A native arm64 embedding backend (for example switching to `infinity` or in-process sentence-transformers) is tracked as a post-launch follow-up, not part of this initiative.

## References

- [docs/codebase/first-run-experience.md](../codebase/first-run-experience.md) — first-run experience overview
- [docs/troubleshooting.md](../troubleshooting.md) — `arm64 / Apple Silicon first-run issues` section
- [Docker Desktop Mac settings](https://docs.docker.com/desktop/settings/mac/) — Rosetta 2 location
- [Docker Compose `platform:` directive](https://docs.docker.com/reference/compose-file/services/#platform)
- [HuggingFace text-embeddings-inference](https://github.com/huggingface/text-embeddings-inference)
