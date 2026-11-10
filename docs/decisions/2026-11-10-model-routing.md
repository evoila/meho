# Model routing: MEHO picks the model server for its own agents (decision)

**Status:** Proposed, waits for the owner's confirmation. This record becomes the decision only when the owner approves its pull request in his own words.
**Date:** 2026-11-10
**Initiative:** [#4068](https://github.com/evoila/meho/issues/4068)
**Task:** [#6093](https://github.com/evoila/meho/issues/6093)
**Built in:** [#5861](https://github.com/evoila/meho/pull/5861) (the code already works this way)

## The decision in short

- MEHO's own agents use **one model server**. MEHO picks it itself, with one setting: `AGENT_MODEL_BACKEND`.
- MEHO does **not** put a gateway between itself and the model servers. Not now.
- A run **never falls back** to another model server. A fallback to Claude is off, and only a separate decision can turn it on.

## Words used

- **Model server:** the service that runs the AI model and answers the agent. For example Anthropic, AWS Bedrock, an OpenAI-compatible server (vLLM, Ollama, OpenRouter) or VCF Private AI Foundation.
- **Gateway:** one extra service that sits in front of the model servers and passes every request on.
- **Fallback:** when the chosen model server fails, the run tries another one.
- **Backend:** the name MEHO uses for one kind of model server.
- **Egress:** data that leaves the customer's network for a cloud service.

## Which agents this covers

Every agent run that MEHO starts itself:

- hosted runs (REST, MCP, console, the event stream)
- scheduled runs and event-triggered runs
- agents started by another agent
- the investigator

It does not cover the agents of the people who use MEHO (for example their own Claude Code). Those pick their own model.

## How it works

- `AGENT_MODEL_BACKEND` names the backend: `anthropic` (the default), `openai`, `bedrock` or `vcf-paif`. The setting is documented in `backend/src/meho_backplane/settings.py:1156-1168`. An unknown value stops the start.
- Every risk level of an agent (`fast`, `standard`, `deep`) and every tenant uses this one backend. `_SettingsResolver.resolve` in `backend/src/meho_backplane/agent/models.py:1648` reads the setting on each run.
- **No fallback.** When the chosen backend is not set up (for example no key), the run fails and sends no model request. The run record says why. The same rule is written in `settings.py:1166-1167` and in the docstring of `_SettingsResolver` (`agent/models.py:1639`).
- **The egress rule.** The resolver can refuse a cloud backend for a tenant whose policy has `allow_egress=False`. The field is in `agent/models.py:296`, and the check is in `agent/models.py:434-436`. Today the live runs build their policy without a per-tenant setting for it, so the choice of `AGENT_MODEL_BACKEND` is the control: an instance that must keep data inside sets a backend that stays inside, such as `vcf-paif` or an `openai` server on its own network. When the tenant setting is connected later, the check applies at once.
- When the setting says "our own model server", no model call goes to Anthropic.

## Why MEHO picks the server itself

- Only MEHO knows the tenant, the session and the egress rule for each run. A gateway would have to be told all of this, or it would decide without it.
- A gateway is one more part that sees every prompt and holds every key. That is a bigger target, and one more thing to run and to secure.
- One setting is easy to explain, to test and to audit. The tests are `backend/tests/test_agent_model_backend_setting.py` and `backend/tests/test_agent_model_resolver.py`.
- Applications of the customer that use an AI model on their own talk to the model server directly. They do not go through MEHO.

## Why there is no fallback

- A silent switch to another server can send data to a place the customer did not choose.
- A silent switch makes costs and answers hard to compare, and hides a broken setup.
- A failed run is a clear signal. The run record names the backend and the reason.

## What stays open

| Question | State | Who decides |
|---|---|---|
| A fallback to Claude when the chosen server fails | Off. Needs its own decision | The program owner |
| Keys and limits for each person | Not needed now. If needed later, a gateway can sit in front of the model server, and MEHO changes one URL (for example `OPENAI_BASE_URL`) | The program owner |
| Reading the egress rule per tenant in live runs | Not connected yet | The program owner |
| The group summaries at connector ingest | Separate Task on docs answers | The program owner |

## Where it is documented

- [agent-runtime.md](../codebase/agent-runtime.md), section "How a live run picks its model".
