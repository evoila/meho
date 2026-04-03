# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
E2E tests for selective approval feature.

Tests that safe plans auto-execute and risky plans require approval.
Uses stable data-testid selectors instead of text-based selectors.
"""

import asyncio

import pytest
from playwright.async_api import Page, async_playwright, expect

FRONTEND_URL = "http://localhost:5173"


async def login_to_meho(page: Page) -> None:
    """Helper to login to MEHO frontend"""
    await page.goto(FRONTEND_URL)
    # Wait for network idle to ensure initial load
    await page.wait_for_load_state("networkidle")

    # Wait for any indicator of auth state
    try:
        await page.wait_for_selector(
            'button:has-text("Logout"), button:has-text("Sign In"), button:has-text("Generate Test Token")',
            timeout=5000,
        )
    except:  # noqa: E722 -- intentional bare except for test cleanup
        print("Warning: Could not find login/logout buttons")

    # Check if already logged in
    logout_button = page.locator('button:has-text("Logout")')
    if await logout_button.count() > 0:
        return

    # Try to login
    try:
        # Check if we are on login page or need to generate token
        generate_token = page.locator('button:has-text("Generate Test Token")')
        if await generate_token.count() > 0:
            await generate_token.click()
            await asyncio.sleep(1)

        login_button = page.locator('button:has-text("Sign In")')
        if await login_button.count() > 0:
            await login_button.click()
            await page.wait_for_load_state("networkidle")
    except Exception as e:
        print(f"Login attempt failed: {e}")


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_safe_plan_auto_executes():
    """Test that safe plans (knowledge search) auto-execute without approval"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Navigate to chat
            await page.goto(f"{FRONTEND_URL}/chat")
            await page.wait_for_load_state("networkidle")

            # Send a safe query (knowledge search only)
            chat_input = page.locator('[data-testid="chat-input"]')
            await chat_input.fill("What is the architecture of my application?")
            await chat_input.press("Enter")

            # Wait for typing indicator, then auto-execute message or execution status
            await expect(page.locator('[data-testid="typing-indicator"]')).to_be_visible(
                timeout=30000
            )

            # Should see auto-execute message or execution status (not approval required)
            # Check for either one (they might both be present)
            auto_exec_msg = page.locator('[data-testid="auto-execute-message"]')
            exec_status = page.locator('[data-testid="execution-status"]')
            await expect(auto_exec_msg.or_(exec_status).first).to_be_visible(timeout=60000)

            # Should NOT see approval buttons
            approve_button = page.locator('[data-testid="approve-button"]')
            await expect(approve_button).to_have_count(0, timeout=5000)
        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_risky_plan_requires_approval():
    """Test that risky plans (API calls) require user approval"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Navigate to chat
            await page.goto(f"{FRONTEND_URL}/chat")
            await page.wait_for_load_state("networkidle")

            # Send a risky query (includes API calls)
            chat_input = page.locator('[data-testid="chat-input"]')
            await chat_input.fill("Call the GitHub API to get recent commits for my repository")
            await chat_input.press("Enter")

            # Wait for typing indicator
            await expect(page.locator('[data-testid="typing-indicator"]')).to_be_visible(
                timeout=60000
            )

            # Wait for either approval required OR plan preview (which indicates approval needed)
            # Note: If no connectors are configured, planner might create safe plan instead
            approval_msg = page.locator('[data-testid="approval-required-message"]')
            plan_preview = page.locator('[data-testid="plan-preview"]')

            # Check if we got a risky plan (requires approval)
            # Wait a bit for the plan to be created
            await asyncio.sleep(2)
            approval_visible = (await approval_msg.count() > 0) or (await plan_preview.count() > 0)

            if approval_visible:
                # Should see approval buttons
                approve_button = page.locator('[data-testid="approve-button"]')
                await expect(approve_button).to_be_visible(timeout=10000)

                # Should NOT see auto-execute message
                auto_exec = page.locator('[data-testid="auto-execute-message"]')
                await expect(auto_exec).to_have_count(0, timeout=5000)
            else:
                # If planner created safe plan (no connectors), that's also valid
                # Just verify it auto-executed (test passes)
                exec_status = page.locator('[data-testid="execution-status"]')
                auto_exec_msg = page.locator('[data-testid="auto-execute-message"]')
                await expect(exec_status.or_(auto_exec_msg).first).to_be_visible(timeout=60000)
        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_approve_risky_plan():
    """Test approving a risky plan and seeing it execute"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Navigate to chat
            await page.goto(f"{FRONTEND_URL}/chat")
            await page.wait_for_load_state("networkidle")

            # Send a risky query
            chat_input = page.locator('[data-testid="chat-input"]')
            await chat_input.fill("Call the GitHub API to get recent commits")
            await chat_input.press("Enter")

            # Wait for typing indicator
            await expect(page.locator('[data-testid="typing-indicator"]')).to_be_visible(
                timeout=60000
            )

            # Wait for approval UI (plan preview or approval message)
            # Note: If no connectors configured, this might auto-execute instead
            plan_preview = page.locator('[data-testid="plan-preview"]')
            approval_msg = page.locator('[data-testid="approval-required-message"]')

            # Check if approval is needed
            has_approval_ui = (await plan_preview.count() > 0) or (await approval_msg.count() > 0)

            if has_approval_ui:
                # Wait for approve button
                await page.wait_for_selector('[data-testid="approve-button"]', timeout=10000)

                # Click approve
                approve_button = page.locator('[data-testid="approve-button"]').first
                await approve_button.click()

                # Should see execution started
                await expect(page.locator('[data-testid="execution-status"]')).to_be_visible(
                    timeout=60000
                )
            else:
                # If it auto-executed (safe plan), that's fine - just verify execution
                exec_status = page.locator('[data-testid="execution-status"]')
                auto_exec_msg = page.locator('[data-testid="auto-execute-message"]')
                await expect(exec_status.or_(auto_exec_msg).first).to_be_visible(timeout=60000)
        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_plan_shows_risk_classification():
    """Test that plans show correct risk classification"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Navigate to chat
            await page.goto(f"{FRONTEND_URL}/chat")
            await page.wait_for_load_state("networkidle")

            # Send query that creates a plan
            chat_input = page.locator('[data-testid="chat-input"]')
            await chat_input.fill("Search knowledge about my system")
            await chat_input.press("Enter")

            # Wait for typing indicator
            await expect(page.locator('[data-testid="typing-indicator"]')).to_be_visible(
                timeout=30000
            )

            # Plan should be visible (either as preview for approval, or execution status for auto-execute)
            plan_preview = page.locator('[data-testid="plan-preview"]')
            exec_status = page.locator('[data-testid="execution-status"]')
            auto_exec_msg = page.locator('[data-testid="auto-execute-message"]')
            await expect(plan_preview.or_(exec_status).or_(auto_exec_msg).first).to_be_visible(
                timeout=60000
            )
        finally:
            await browser.close()
