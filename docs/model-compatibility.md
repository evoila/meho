# Model Compatibility Matrix

MEHO supports three LLM providers. This document describes the quality and capability differences between them.

## Provider Tiers

| Tier | Provider | Model | Quality | Use Case |
|------|----------|-------|---------|----------|
| 1 (Optimal) | Anthropic Claude | claude-opus-4-6, claude-sonnet-4-6 | Reference quality | Production deployments, complex cross-system investigations |
| 2 (Good) | OpenAI | gpt-4o, gpt-4o-mini | Good for most scenarios | Teams already using OpenAI, cost-sensitive deployments |
| 3 (Experimental) | Ollama | qwen2.5:32b | Experimental | Air-gapped environments, local development, privacy-first |
| 3-lite | Ollama | qwen2.5:7b | Minimal | Low-resource machines (8GB RAM), quick testing |

## Quality Details

### Tier 1: Anthropic Claude (Reference)

Claude is the reference model. MEHO was designed, tested, and optimized for Claude's reasoning capabilities.

- **Adaptive thinking**: Automatically scales reasoning depth per query complexity
- **Prompt caching**: Reduces cost by ~50% on repeated system prompts
- **Structured output**: Reliable JSON schema adherence for connector classification and data extraction
- **Cross-system correlation**: Strongest at connecting findings across Kubernetes, Prometheus, Loki, and other connectors

**Configuration:**
```env
MEHO_LLM_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...
```

### Tier 2: OpenAI GPT-4o (Good Alternative)

GPT-4o handles most investigation scenarios well but may miss subtle cross-system correlations that Claude catches.

- **Strengths**: Fast responses, good tool use, reliable structured output
- **Limitations**: May not follow multi-hop investigation chains as deeply, less nuanced in ambiguous diagnostic scenarios
- **Cost**: Comparable to Claude for most workloads

**Configuration:**
```env
MEHO_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
```

### Tier 3: Ollama Local Models (Experimental)

Ollama enables fully local, air-gapped operation with no API keys or cloud dependencies. Investigation quality degrades significantly compared to Claude.

**Recommended model: qwen2.5:32b** (D-08)

- Strong reasoning and tool use capabilities
- Requires 24GB GPU or runs on CPU (slower)
- ~20GB model download on first use

**Lite option: qwen2.5:7b** (D-09)

- Fits on machines with 8GB RAM
- Significant quality degradation -- expect incomplete investigations and missed correlations
- Suitable for quick testing, not production investigations

**Configuration:**
```env
MEHO_LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://localhost:11434/v1  # Local development
# or
OLLAMA_BASE_URL=http://ollama:11434/v1    # Docker Compose with --profile ollama
```

**Starting Ollama with Docker Compose:**
```bash
docker compose --profile ollama up -d
docker exec -it meho-ollama-1 ollama pull qwen2.5:32b  # ~20GB download
```

## Known Limitations

### Structured Output Quality

MEHO uses structured output (JSON schemas) for connector classification and data extraction. Smaller models may produce malformed JSON. PydanticAI includes retry logic for parse failures, but repeated failures will degrade the investigation experience.

### Context Window Differences

| Provider | Context Window | Impact |
|----------|---------------|--------|
| Claude Opus 4.6 | 200K tokens | Handles large investigations with full context |
| GPT-4o | 128K tokens | Sufficient for most investigations |
| qwen2.5:32b | 32K tokens (default) | May truncate long investigation histories |

### Adaptive Thinking

Claude's adaptive thinking (automatic reasoning depth scaling) is Anthropic-specific. OpenAI and Ollama models use fixed reasoning -- they do not automatically think harder on complex queries.

## Model Role Mapping

MEHO assigns different models to different roles based on task complexity:

| Role | Anthropic | OpenAI | Ollama |
|------|-----------|--------|--------|
| Main reasoning (heavy) | claude-opus-4-6 | gpt-4o | qwen2.5:32b |
| Streaming agent | claude-opus-4-6 | gpt-4o | qwen2.5:32b |
| Interpreter | claude-opus-4-6 | gpt-4o | qwen2.5:32b |
| Classifier (utility) | claude-sonnet-4-6 | gpt-4o-mini | qwen2.5:32b |
| Data extractor (utility) | claude-sonnet-4-6 | gpt-4o-mini | qwen2.5:32b |

## Changing Providers

Set `MEHO_LLM_PROVIDER` and restart the application. Provider changes require a restart -- they do not take effect at runtime.

```bash
# In .env file:
MEHO_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...

# Restart:
docker compose restart meho meho-celery
```

Individual model roles can be overridden independently:
```env
# Use GPT-4o for everything except classification (use mini for that)
MEHO_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
CLASSIFIER_MODEL=openai:gpt-4o-mini
```
