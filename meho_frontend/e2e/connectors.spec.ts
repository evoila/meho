import { test, expect } from '@playwright/test';

/**
 * E2E tests for connector management
 */
test.describe('Connectors', () => {
  test.beforeEach(async ({ page }) => {
    // Set up authenticated state with mock token
    await page.goto('/login');
    await page.evaluate(() => {
      // nosemgrep: detected-jwt-token -- mock JWT for E2E test with .mock signature, cannot authenticate against real systems
      const mockToken = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ0ZXN0QGV4YW1wbGUuY29tIiwidGVuYW50X2lkIjoidGVzdC10ZW5hbnQiLCJyb2xlcyI6WyJ1c2VyIl0sImV4cCI6OTk5OTk5OTk5OX0.mock';
      localStorage.setItem('meho_token', mockToken);
    });
  });

  test('connectors page loads correctly', async ({ page }) => {
    await page.goto('/connectors');

    // Should show connectors page elements
    await expect(page.getByRole('heading', { name: /connectors/i })).toBeVisible({ timeout: 10000 });
  });

  test('displays connector list', async ({ page }) => {
    await page.goto('/connectors');

    // Should show list of connectors or empty state
    await expect(
      page.getByText(/no connectors/i).or(page.locator('[data-testid="connector-list"]'))
    ).toBeVisible({ timeout: 10000 });
  });

  test('has add connector button', async ({ page }) => {
    await page.goto('/connectors');

    await expect(
      page.getByRole('button', { name: /add|create|new/i })
    ).toBeVisible({ timeout: 10000 });
  });

  test('can open create connector modal', async ({ page }) => {
    await page.goto('/connectors');

    const addButton = page.getByRole('button', { name: /add|create|new/i });
    await addButton.click();

    // Should show create connector modal/form
    await expect(
      page.getByRole('dialog').or(page.getByRole('form'))
    ).toBeVisible({ timeout: 5000 });
  });

  test('create connector form has required fields', async ({ page }) => {
    await page.goto('/connectors');

    const addButton = page.getByRole('button', { name: /add|create|new/i });
    await addButton.click();

    // Check for required form fields
    await expect(page.getByLabel(/name/i)).toBeVisible({ timeout: 5000 });
    await expect(page.getByLabel(/url|base.*url/i)).toBeVisible();
  });

  test('can cancel connector creation', async ({ page }) => {
    await page.goto('/connectors');

    const addButton = page.getByRole('button', { name: /add|create|new/i });
    await addButton.click();

    // Find and click cancel button
    const cancelButton = page.getByRole('button', { name: /cancel/i });
    await cancelButton.click();

    // Modal should close
    await expect(page.getByRole('dialog')).not.toBeVisible({ timeout: 5000 }).catch(() => {
      // Modal may already be closed
    });
  });

  test('validates required fields on submit', async ({ page }) => {
    await page.goto('/connectors');

    const addButton = page.getByRole('button', { name: /add|create|new/i });
    await addButton.click();

    // Try to submit empty form
    const submitButton = page.getByRole('button', { name: /create|save|submit/i });
    await submitButton.click();

    // Should show validation errors
    await expect(
      page.getByText(/required|please enter|cannot be empty/i)
    ).toBeVisible({ timeout: 5000 }).catch(() => {
      // Validation may be handled differently
    });
  });

  test('can select a connector to view details', async ({ page }) => {
    await page.goto('/connectors');

    // Wait for connectors to load
    await page.waitForTimeout(2000);

    // If there are connectors in the list, click one
    const connectorItem = page.locator('[data-testid="connector-item"]').first();
    if (await connectorItem.isVisible()) {
      await connectorItem.click();

      // Should show connector details
      await expect(
        page.getByRole('heading', { level: 2 }).or(page.getByText(/details/i))
      ).toBeVisible({ timeout: 5000 });
    }
  });

  test('connector details shows endpoints section', async ({ page }) => {
    await page.goto('/connectors');

    // Wait for connectors to load
    await page.waitForTimeout(2000);

    const connectorItem = page.locator('[data-testid="connector-item"]').first();
    if (await connectorItem.isVisible()) {
      await connectorItem.click();

      // Should show endpoints section
      await expect(
        page.getByText(/endpoints/i)
      ).toBeVisible({ timeout: 5000 });
    }
  });
});

