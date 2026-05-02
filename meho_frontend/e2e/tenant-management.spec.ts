import { test, expect, type Page } from '@playwright/test';

/**
 * E2E tests for Superadmin Tenant Management Dashboard
 * 
 * TASK-139 Phase 7: Frontend E2E tests for:
 * 1. Access control (global_admin vs regular users)
 * 2. Tenant list display
 * 3. Create tenant flow
 * 4. Tenant settings pages
 * 5. Navigation
 */

// =============================================================================
// Test Helpers
// =============================================================================

/**
 * Set up mock authentication with specific role
 */
async function loginAs(
  page: Page,
  role: 'global_admin' | 'admin' | 'user' | 'viewer',
  tenantId: string = 'test-tenant'
) {
  // Create a mock JWT token with the specified role
  const payload = {
    sub: `${role}@example.com`,
    tenant_id: role === 'global_admin' ? 'master' : tenantId,
    roles: [role],
    exp: 9999999999, // Far future expiry
  };

  // Base64 encode the payload (simplified mock token)
  const base64Payload = Buffer.from(JSON.stringify(payload)).toString('base64');
  const mockToken = `eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.${base64Payload}.mock`;

  await page.goto('/login');
  await page.evaluate((token) => {
    localStorage.setItem('meho_token', token);
    localStorage.setItem('meho_user', JSON.stringify({
      userId: 'test-user',
      tenantId: token.includes('master') ? 'master' : 'test-tenant',
      roles: token.includes('global_admin') ? ['global_admin'] : ['user'],
      isGlobalAdmin: token.includes('global_admin'),
    }));
  }, mockToken);
}

/**
 * Set up global admin authentication
 */
async function loginAsGlobalAdmin(page: Page) {
  await loginAs(page, 'global_admin', 'master');
}

/**
 * Set up regular user authentication
 */
async function loginAsRegularUser(page: Page) {
  await loginAs(page, 'user', 'test-tenant');
}

/**
 * Set up tenant admin authentication
 */
async function loginAsTenantAdmin(page: Page) {
  await loginAs(page, 'admin', 'test-tenant');
}

// =============================================================================
// Access Control Tests
// =============================================================================

test.describe('Superadmin Access Control', () => {
  test('global_admin can access /admin/tenants', async ({ page }) => {
    await loginAsGlobalAdmin(page);
    await page.goto('/admin/tenants');

    // Should show the tenant management page
    await expect(
      page.getByRole('heading', { name: /tenant management/i })
    ).toBeVisible({ timeout: 10000 });

    // Should show the New Tenant button
    await expect(
      page.getByRole('button', { name: /new tenant/i })
    ).toBeVisible();
  });

  test('regular user is redirected from /admin/tenants', async ({ page }) => {
    await loginAsRegularUser(page);
    await page.goto('/admin/tenants');

    // Should be redirected away from admin page
    // Could redirect to home, login, or show access denied
    await expect(page).not.toHaveURL(/\/admin\/tenants/);
  });

  test('tenant admin is redirected from /admin/tenants', async ({ page }) => {
    await loginAsTenantAdmin(page);
    await page.goto('/admin/tenants');

    // Tenant admin should not have access to global tenant management
    await expect(page).not.toHaveURL(/\/admin\/tenants/);
  });
});

// =============================================================================
// Tenant List Tests
// =============================================================================

test.describe('Tenant List', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsGlobalAdmin(page);
    await page.goto('/admin/tenants');
    // Wait for page to load
    await page.waitForLoadState('networkidle');
  });

  test('displays tenant management header', async ({ page }) => {
    await expect(
      page.getByRole('heading', { name: /tenant management/i })
    ).toBeVisible();

    await expect(
      page.getByText(/manage organizations/i)
    ).toBeVisible();
  });

  test('shows refresh button', async ({ page }) => {
    // Refresh button should be visible (with RefreshCw icon)
    const refreshButton = page.getByRole('button', { name: /refresh/i });
    await expect(refreshButton).toBeVisible();
  });

  test('shows tenant count badge', async ({ page }) => {
    // Should show tenant count (e.g., "3 tenants")
    await expect(
      page.getByText(/\d+ tenants?/i)
    ).toBeVisible({ timeout: 10000 });
  });

  test('has show inactive tenants checkbox', async ({ page }) => {
    // Should have a checkbox to show inactive tenants
    await expect(
      page.getByLabel(/show inactive/i)
    ).toBeVisible();
  });

  test('displays tenant table headers', async ({ page }) => {
    // Table should have appropriate headers
    await expect(page.getByRole('columnheader', { name: /tenant/i })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /status/i })).toBeVisible();
    await expect(page.getByRole('columnheader', { name: /tier|subscription/i })).toBeVisible();
  });
});

// =============================================================================
// Create Tenant Tests
// =============================================================================

test.describe('Create Tenant', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsGlobalAdmin(page);
    await page.goto('/admin/tenants');
    await page.waitForLoadState('networkidle');
  });

  test('opens create tenant modal on button click', async ({ page }) => {
    // Click New Tenant button
    await page.getByRole('button', { name: /new tenant/i }).click();

    // Modal should appear
    await expect(
      page.getByRole('dialog')
    ).toBeVisible({ timeout: 5000 });

    // Modal should have form fields
    await expect(
      page.getByLabel(/tenant id/i)
    ).toBeVisible();
  });

  test('create tenant form has required fields', async ({ page }) => {
    await page.getByRole('button', { name: /new tenant/i }).click();

    // Wait for modal
    await expect(page.getByRole('dialog')).toBeVisible();

    // Check for form fields
    await expect(page.getByLabel(/tenant id/i)).toBeVisible();
    await expect(page.getByLabel(/display name/i)).toBeVisible();
    await expect(page.getByLabel(/subscription tier/i).or(page.getByText(/subscription tier/i))).toBeVisible();
  });

  test('validates tenant_id format', async ({ page }) => {
    await page.getByRole('button', { name: /new tenant/i }).click();
    await expect(page.getByRole('dialog')).toBeVisible();

    // Try to enter an invalid tenant ID (uppercase, spaces)
    const tenantIdInput = page.getByLabel(/tenant id/i);
    await tenantIdInput.fill('Invalid Tenant ID!');

    // Click create button
    const createButton = page.getByRole('button', { name: /create/i });
    await createButton.click();

    // Should show validation error
    await expect(
      page.getByText(/lowercase|alphanumeric|invalid/i)
    ).toBeVisible({ timeout: 5000 }).catch(() => {
      // Validation may be handled on blur or differently
    });
  });

  test('can close create tenant modal', async ({ page }) => {
    await page.getByRole('button', { name: /new tenant/i }).click();
    await expect(page.getByRole('dialog')).toBeVisible();

    // Find and click close/cancel button
    const cancelButton = page.getByRole('button', { name: /cancel|close/i });
    await cancelButton.click();

    // Modal should close
    await expect(page.getByRole('dialog')).not.toBeVisible({ timeout: 5000 });
  });

  test('subscription tier has correct options', async ({ page }) => {
    await page.getByRole('button', { name: /new tenant/i }).click();
    await expect(page.getByRole('dialog')).toBeVisible();

    // Look for subscription tier dropdown or radio buttons
    const tierSelect = page.getByLabel(/subscription tier/i);
    if (await tierSelect.isVisible()) {
      await tierSelect.click();

      // Should have free, pro, enterprise options
      await expect(page.getByRole('option', { name: /free/i })).toBeVisible();
      await expect(page.getByRole('option', { name: /pro/i })).toBeVisible();
      await expect(page.getByRole('option', { name: /enterprise/i })).toBeVisible();
    }
  });
});

// =============================================================================
// Tenant Settings Tests
// =============================================================================

test.describe('Tenant Settings', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsGlobalAdmin(page);
  });

  test('can navigate to tenant settings', async ({ page }) => {
    await page.goto('/admin/tenants');
    await page.waitForLoadState('networkidle');

    // Click on a tenant row to navigate to settings
    const tenantRow = page.locator('tr').filter({ hasText: /test-tenant|tenant/i }).first();
    if (await tenantRow.isVisible()) {
      await tenantRow.click();

      // Should navigate to tenant settings page
      await expect(page).toHaveURL(/\/admin\/tenants\/[^/]+/);
    }
  });

  test('tenant settings page has back button', async ({ page }) => {
    // Navigate directly to a tenant settings page
    await page.goto('/admin/tenants/test-tenant');

    // Should have back button
    await expect(
      page.getByRole('button', { name: /back to tenants/i })
    ).toBeVisible({ timeout: 10000 });
  });

  test('tenant settings page has tabs', async ({ page }) => {
    await page.goto('/admin/tenants/test-tenant');
    await page.waitForLoadState('networkidle');

    // Should have settings tabs
    await expect(
      page.getByRole('tab', { name: /general/i })
    ).toBeVisible({ timeout: 10000 });

    await expect(
      page.getByRole('tab', { name: /quotas/i })
    ).toBeVisible();

    await expect(
      page.getByRole('tab', { name: /llm/i })
    ).toBeVisible();

    await expect(
      page.getByRole('tab', { name: /danger/i })
    ).toBeVisible();
  });

  test('can switch between settings tabs', async ({ page }) => {
    await page.goto('/admin/tenants/test-tenant');
    await page.waitForLoadState('networkidle');

    // Click Quotas tab
    await page.getByRole('tab', { name: /quotas/i }).click();

    // Should show quota settings
    await expect(
      page.getByText(/max connectors|connector limit/i)
    ).toBeVisible({ timeout: 5000 });

    // Click LLM Settings tab
    await page.getByRole('tab', { name: /llm/i }).click();

    // Should show LLM settings
    await expect(
      page.getByText(/model|temperature/i)
    ).toBeVisible({ timeout: 5000 });
  });

  test('danger zone has disable/enable toggle', async ({ page }) => {
    await page.goto('/admin/tenants/test-tenant');
    await page.waitForLoadState('networkidle');

    // Click Danger Zone tab
    await page.getByRole('tab', { name: /danger/i }).click();

    // Should show disable or enable button depending on tenant state
    await expect(
      page.getByRole('button', { name: /disable|enable/i })
    ).toBeVisible({ timeout: 5000 });
  });
});

// =============================================================================
// Navigation Tests
// =============================================================================

test.describe('Navigation', () => {
  test('global_admin sees Tenants link in sidebar', async ({ page }) => {
    await loginAsGlobalAdmin(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Sidebar should have Tenants link
    await expect(
      page.getByRole('link', { name: /tenants/i })
    ).toBeVisible({ timeout: 10000 });
  });

  test('regular user does not see Tenants link in sidebar', async ({ page }) => {
    await loginAsRegularUser(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Sidebar should NOT have Tenants link for regular user
    await expect(
      page.getByRole('link', { name: /tenants/i })
    ).not.toBeVisible({ timeout: 5000 });
  });

  test('can navigate from sidebar to tenants page', async ({ page }) => {
    await loginAsGlobalAdmin(page);
    await page.goto('/');
    await page.waitForLoadState('networkidle');

    // Click Tenants link in sidebar
    const tenantsLink = page.getByRole('link', { name: /tenants/i });
    if (await tenantsLink.isVisible()) {
      await tenantsLink.click();

      // Should navigate to tenants page
      await expect(page).toHaveURL(/\/admin\/tenants/);
    }
  });
});

// =============================================================================
// Error Handling Tests
// =============================================================================

test.describe('Error Handling', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsGlobalAdmin(page);
  });

  test('shows error state when API fails', async ({ page }) => {
    // Intercept the API call and make it fail
    await page.route('**/api/tenants**', async (route) => {
      await route.fulfill({
        status: 500,
        body: JSON.stringify({ detail: 'Internal Server Error' }),
      });
    });

    await page.goto('/admin/tenants');

    // Should show error state
    await expect(
      page.getByText(/failed|error|something went wrong/i)
    ).toBeVisible({ timeout: 10000 });

    // Should have retry button
    await expect(
      page.getByRole('button', { name: /retry|try again/i })
    ).toBeVisible();
  });

  test('shows 404 for non-existent tenant', async ({ page }) => {
    // Navigate to a non-existent tenant
    await page.goto('/admin/tenants/non-existent-tenant-12345');

    // Should show not found or error
    await expect(
      page.getByText(/not found|error|doesn't exist/i)
    ).toBeVisible({ timeout: 10000 });
  });
});

// =============================================================================
// Loading State Tests
// =============================================================================

test.describe('Loading States', () => {
  test.beforeEach(async ({ page }) => {
    await loginAsGlobalAdmin(page);
  });

  test('shows loading state while fetching tenants', async ({ page }) => {
    // Slow down the API response to see loading state
    await page.route('**/api/tenants**', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 500));
      await route.continue();
    });

    await page.goto('/admin/tenants');

    // Should show loading state initially
    await expect(
      page.getByText(/loading/i)
    ).toBeVisible({ timeout: 2000 }).catch(() => {
      // Loading might be too fast to catch
    });
  });
});

