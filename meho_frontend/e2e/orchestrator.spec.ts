import { test, expect } from '@playwright/test';

/**
 * E2E tests for orchestrator UI functionality (TASK-181)
 *
 * These tests verify the orchestrator progress UI with mocked SSE events.
 */
test.describe('Orchestrator UI', () => {
  // Mock SSE events for orchestrator
  const mockOrchestratorEvents = [
    { type: 'orchestrator_start', agent: 'orchestrator', data: { goal: 'Test query' } },
    { type: 'iteration_start', agent: 'orchestrator', data: { iteration: 1 } },
    {
      type: 'dispatch_start',
      agent: 'orchestrator',
      data: {
        iteration: 1,
        connectors: [
          { id: 'conn-1', name: 'K8s Production' },
          { id: 'conn-2', name: 'GCP Production' },
        ],
      },
    },
    {
      type: 'early_findings',
      agent: 'orchestrator',
      data: {
        connector_id: 'conn-1',
        connector_name: 'K8s Production',
        findings_preview: 'Found 3 pods in namespace default',
        status: 'success',
        remaining_count: 1,
      },
    },
    {
      type: 'connector_complete',
      agent: 'orchestrator',
      data: {
        connector_id: 'conn-2',
        connector_name: 'GCP Production',
        status: 'success',
        findings_preview: 'Found 2 VM instances',
      },
    },
    {
      type: 'synthesis_start',
      agent: 'orchestrator',
      data: { partial: false },
    },
    {
      type: 'final_answer',
      agent: 'orchestrator',
      data: {
        content: 'Based on my analysis...',
        iterations: 1,
        connectors_queried: ['K8s Production', 'GCP Production'],
        total_time_ms: 5000,
      },
    },
    {
      type: 'orchestrator_complete',
      agent: 'orchestrator',
      data: {
        success: true,
        iterations: 1,
        total_time_ms: 5000,
      },
    },
  ];

  test.beforeEach(async ({ page }) => {
    // Set up authenticated state with mock token
    await page.goto('/login');
    await page.evaluate(() => {
      // nosemgrep: detected-jwt-token -- mock JWT for E2E test with .mock signature, cannot authenticate against real systems
      const mockToken =
        'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0QGV4YW1wbGUuY29tIiwidGVuYW50X2lkIjoidGVzdC10ZW5hbnQiLCJyb2xlcyI6WyJ1c2VyIl0sImV4cCI6OTk5OTk5OTk5OX0.mock';
      localStorage.setItem('meho_token', mockToken);
    });
  });

  test('shows orchestrator progress', async ({ page }) => {
    // Mock the SSE stream
    await page.route('**/api/chat/stream', async (route) => {
      const body = mockOrchestratorEvents
        .map((e) => `data: ${JSON.stringify(e)}\n\n`)
        .join('');

      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
        body,
      });
    });

    await page.goto('/');

    // Send a message to trigger orchestrator
    const chatInput = page.getByRole('textbox');
    await chatInput.fill('Show me the status of my systems');
    await chatInput.press('Enter');

    // Wait for orchestrator UI to appear
    await expect(page.getByText('Orchestrator')).toBeVisible({ timeout: 10000 });
  });

  test('displays connector cards with status badges', async ({ page }) => {
    // Mock the SSE stream
    await page.route('**/api/chat/stream', async (route) => {
      const body = mockOrchestratorEvents
        .map((e) => `data: ${JSON.stringify(e)}\n\n`)
        .join('');

      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
        body,
      });
    });

    await page.goto('/');

    const chatInput = page.getByRole('textbox');
    await chatInput.fill('Query test');
    await chatInput.press('Enter');

    // Should show connector names
    await expect(page.getByText('K8s Production')).toBeVisible({ timeout: 10000 });
    await expect(page.getByText('GCP Production')).toBeVisible({ timeout: 10000 });

    // Should show success status
    await expect(page.getByText('Success').first()).toBeVisible({ timeout: 10000 });
  });

  test('shows error indicator when connector fails', async ({ page }) => {
    const errorEvents = [
      { type: 'orchestrator_start', agent: 'orchestrator', data: { goal: 'Test' } },
      { type: 'iteration_start', agent: 'orchestrator', data: { iteration: 1 } },
      {
        type: 'dispatch_start',
        agent: 'orchestrator',
        data: {
          iteration: 1,
          connectors: [{ id: 'conn-1', name: 'Failing Connector' }],
        },
      },
      {
        type: 'connector_complete',
        agent: 'orchestrator',
        data: {
          connector_id: 'conn-1',
          connector_name: 'Failing Connector',
          status: 'failed',
          findings_preview: null,
        },
      },
      {
        type: 'final_answer',
        agent: 'orchestrator',
        data: {
          content: 'Could not complete query',
          iterations: 1,
          connectors_queried: ['Failing Connector'],
          total_time_ms: 1000,
          partial: true,
        },
      },
    ];

    await page.route('**/api/chat/stream', async (route) => {
      const body = errorEvents.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');

      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
        body,
      });
    });

    await page.goto('/');

    const chatInput = page.getByRole('textbox');
    await chatInput.fill('Test query');
    await chatInput.press('Enter');

    // Should show failed status
    await expect(page.getByText('Failed')).toBeVisible({ timeout: 10000 });

    // Should show error indicator
    await expect(page.getByText('Errors').or(page.getByText('⚠'))).toBeVisible({
      timeout: 10000,
    });
  });

  test('shows timeout indicator when connector times out', async ({ page }) => {
    const timeoutEvents = [
      { type: 'orchestrator_start', agent: 'orchestrator', data: { goal: 'Test' } },
      { type: 'iteration_start', agent: 'orchestrator', data: { iteration: 1 } },
      {
        type: 'dispatch_start',
        agent: 'orchestrator',
        data: {
          iteration: 1,
          connectors: [{ id: 'conn-1', name: 'Slow Connector' }],
        },
      },
      {
        type: 'connector_complete',
        agent: 'orchestrator',
        data: {
          connector_id: 'conn-1',
          connector_name: 'Slow Connector',
          status: 'timeout',
          findings_preview: null,
        },
      },
      {
        type: 'final_answer',
        agent: 'orchestrator',
        data: {
          content: 'Query timed out',
          iterations: 1,
          connectors_queried: ['Slow Connector'],
          total_time_ms: 30000,
          partial: true,
        },
      },
    ];

    await page.route('**/api/chat/stream', async (route) => {
      const body = timeoutEvents.map((e) => `data: ${JSON.stringify(e)}\n\n`).join('');

      await route.fulfill({
        status: 200,
        headers: {
          'Content-Type': 'text/event-stream',
          'Cache-Control': 'no-cache',
          Connection: 'keep-alive',
        },
        body,
      });
    });

    await page.goto('/');

    const chatInput = page.getByRole('textbox');
    await chatInput.fill('Test query');
    await chatInput.press('Enter');

    // Should show timeout status
    await expect(page.getByText('Timeout')).toBeVisible({ timeout: 10000 });
  });

});
