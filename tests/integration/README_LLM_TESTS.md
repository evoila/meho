# LLM Integration Tests

## Overview

The agent dependencies module includes integration tests that make **real calls to OpenAI's API** to verify LLM-powered result interpretation.

## Tests

- `test_interpret_results_with_real_llm` - Tests LLM analysis of diagnostic data
- `test_interpret_results_handles_empty_results` - Tests edge case handling

## Enabling LLM Tests

These tests are currently **skipped** because `OPENAI_API_KEY` is not set.

### To enable:

1. Add your OpenAI API key to `.env`:
   ```bash
   OPENAI_API_KEY=sk-your-key-here
   ```

2. Run the tests:
   ```bash
   pytest tests/integration/test_agent_dependencies_integration.py::test_interpret_results_with_real_llm -v -s
   ```

3. Expected output:
   ```
   === LLM Interpretation ===
   Key Findings:
   1. One pod is failing on Kubernetes cluster prod-01
   2. Deployment occurred 2 hours ago with degraded status
   ...
   ```

## Why Test Real LLM Calls?

✅ **Validates actual OpenAI integration** (not just mocks)  
✅ **Catches API changes** (schema, response format)  
✅ **Verifies prompt engineering** (are we getting useful responses?)  
✅ **Tests error handling** (rate limits, network issues)  
✅ **Ensures production readiness**  

## Cost Considerations

- Each LLM test costs ~$0.001-0.01 (very low)
- Tests use GPT-4 for quality analysis
- Temperature=0.3 for consistency
- Worth the cost for confidence!

## CI/CD

For CI/CD pipelines:
- Set `OPENAI_API_KEY` as a secret environment variable
- Tests will automatically run when key is available
- Tests gracefully skip if key is missing (won't fail CI)

