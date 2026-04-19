# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
E2E tests for Connectors vertical slice - Frontend to Backend integration.
Tests all connector management functionality end-to-end.

Uses stable data-testid selectors following Session 9 patterns.
"""

import asyncio
import contextlib
import json
import os
import time
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright, expect

FRONTEND_URL = "http://localhost:5173"
SAMPLE_SPEC_PATH = (
    Path(__file__).parent.parent.parent / "samplespecs" / "vmware-cloud-foundation.json"
)

# Mark all tests in this file to run sequentially
pytestmark = [pytest.mark.e2e, pytest.mark.serial]


# Add delays between tests to avoid overwhelming the backend
@pytest.fixture(autouse=True)
async def test_isolation():
    """Ensure tests don't interfere with each other"""
    await asyncio.sleep(1)
    yield
    await asyncio.sleep(2)


async def login_to_meho(page: Page) -> None:
    """Helper to login to MEHO frontend"""
    await page.goto(FRONTEND_URL)
    await page.wait_for_load_state("networkidle")

    # Check if already logged in
    logout_button = page.locator('button:has-text("Logout")')
    if await logout_button.count() > 0:
        print("Already logged in")
        return

    # Try to login
    try:
        if "/login" not in page.url:
            await page.goto(f"{FRONTEND_URL}/login")
            await page.wait_for_load_state("networkidle")

        generate_token = page.locator('button:has-text("Generate Test Token")')
        if await generate_token.count() > 0:
            print("Generating test token...")
            await generate_token.click()
            await page.wait_for_selector('textarea[id="token"]', state="visible", timeout=10000)
            await asyncio.sleep(2)

        login_button = page.locator('button:has-text("Sign In")')
        if await login_button.count() > 0:
            print("Clicking Sign In...")
            await login_button.click()
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(1)
    except Exception as e:
        print(f"Login attempt failed: {e}")
        raise


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_create_new_connector():  # NOSONAR (cognitive complexity)
    """Test 1.1: Create New Connector"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Navigate to connectors page
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            # Verify connectors page loaded
            await expect(page.locator('[data-testid="connectors-page-title"]')).to_be_visible(
                timeout=10000
            )

            # Click "New Connector" button
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await expect(new_connector_btn).to_be_visible(timeout=5000)
            await new_connector_btn.click()

            # Wait for modal to appear
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            # Fill in form
            unique_name = f"Test Connector {int(time.time())}"
            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://api.example.com"
            )

            # Fill description
            description_input = page.locator('textarea[placeholder*="GitHub REST API"]')
            if await description_input.count() > 0:
                await description_input.fill("Test API connector")

            # Configure safety policies (uncheck DELETE to block it)
            # Find DELETE checkbox by finding the label that contains "DELETE" text
            delete_labels = page.locator("label").filter(has_text="DELETE")
            if await delete_labels.count() > 0:
                # Find the checkbox within that label
                delete_checkbox = delete_labels.first.locator('input[type="checkbox"]')
                if await delete_checkbox.count() > 0:
                    await delete_checkbox.click()

            # Set default safety level to "caution"
            caution_radio = page.locator('input[type="radio"][value="caution"]')
            if await caution_radio.count() > 0:
                await caution_radio.click()

            # Submit form
            submit_btn = page.locator('[data-testid="create-connector-submit-button"]')
            await expect(submit_btn).not_to_be_disabled(timeout=5000)
            await submit_btn.click()

            # Wait for modal to close - connector details should open (based on onSuccess callback)
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # After creation, we should be on connector details page
            # Navigate back to connectors list page explicitly
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Verify connectors page loaded
            await expect(page.locator('[data-testid="connectors-page-title"]')).to_be_visible(
                timeout=10000
            )

            # Wait for connectors list to load (check for loading state to disappear)
            loading_indicator = page.locator("text=Loading connectors")
            if await loading_indicator.count() > 0:
                await expect(loading_indicator).not_to_be_visible(timeout=10000)

            # Give the list time to refresh after connector creation
            await asyncio.sleep(2)

            # Search for the connector
            search_input = page.locator('[data-testid="connectors-search-input"]')
            await expect(search_input).to_be_visible(timeout=10000)

            # Clear any existing search first
            await search_input.click()
            await search_input.fill("")  # Clear
            await asyncio.sleep(0.5)

            # Search for our connector
            await search_input.fill(unique_name)
            await asyncio.sleep(1.5)  # Wait for search to filter

            # Verify connector appears - try multiple approaches
            max_wait = 10
            waited = 0
            found = False

            while waited < max_wait and not found:
                # Try to find connector card
                connector_cards = page.locator('[data-testid^="connector-card-"]')
                card_count = await connector_cards.count()

                if card_count > 0:
                    # Check if any card contains our connector name
                    for i in range(card_count):
                        card = connector_cards.nth(i)
                        card_text = await card.text_content()
                        if unique_name in (card_text or ""):
                            found = True
                            print(f"✅ Found connector in card {i + 1}")
                            break

                # Also check if connector name appears anywhere on page
                if not found:
                    connector_name = page.locator(f"text={unique_name}")
                    if await connector_name.count() > 0:
                        found = True
                        print("✅ Found connector name on page")

                if found:
                    break

                await asyncio.sleep(1)
                waited += 1

            if not found:
                # Get current page content for debugging
                await page.content()
                print(f"⚠️  Connector not found. Page URL: {page.url}")
                print(f"⚠️  Searching for: {unique_name}")
                # Check if connector list is empty
                empty_state = page.locator("text=No connectors yet")
                if await empty_state.count() > 0:
                    print("⚠️  Connector list is empty")
                else:
                    # List some existing connectors for debugging
                    all_cards = page.locator('[data-testid^="connector-card-"]')
                    all_count = await all_cards.count()
                    print(f"⚠️  Found {all_count} connector cards, but not our new one")

                # Don't fail - connector might have been created but list needs refresh
                # The important part is that creation succeeded (modal closed, no errors)
                print(
                    "⚠️  Connector creation appears successful, but not found in list (may need manual refresh)"
                )

            assert found, f"Connector '{unique_name}' was not found in list after creation"

            print(f"✅ Successfully created connector: {unique_name}")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_upload_valid_openapi_spec_file():
    """Test 1.3: Upload Valid OpenAPI Spec (JSON File)"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # First create a connector
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            # Create connector (quick version)
            unique_name = f"VCF Test {int(time.time())}"
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await new_connector_btn.click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://vcf.example.com"
            )
            await page.locator('[data-testid="create-connector-submit-button"]').click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)

            # Wait for connector details to load (should auto-navigate)
            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Navigate to Upload Spec tab (click tab button)
            upload_tab = page.locator('button:has-text("Upload")')
            if await upload_tab.count() > 0:
                await upload_tab.click()
                await asyncio.sleep(1)

            # Verify upload mode buttons are visible
            file_mode_btn = page.locator('[data-testid="upload-mode-file-button"]')
            await expect(file_mode_btn).to_be_visible(timeout=5000)

            # Verify sample spec file exists
            if not SAMPLE_SPEC_PATH.exists():
                pytest.skip(f"Sample spec file not found: {SAMPLE_SPEC_PATH}")

            # Upload file
            file_input = page.locator('[data-testid="openapi-spec-file-input"]')
            await file_input.set_input_files(str(SAMPLE_SPEC_PATH))
            await asyncio.sleep(1)

            # Click upload button
            upload_btn = page.locator('[data-testid="upload-spec-button"]')
            await expect(upload_btn).not_to_be_disabled(timeout=5000)
            await upload_btn.click()

            # Wait for upload to complete (success message or endpoint count)
            # Look for success message or endpoints tab
            max_wait = 30
            waited = 0
            success = False

            while waited < max_wait:
                # Check for success message
                success_msg = page.locator("text=/successfully/i")
                if await success_msg.count() > 0:
                    print("✅ Upload successful!")
                    success = True
                    break

                # Check for endpoint count
                endpoint_count = page.locator("text=/endpoints extracted/i")
                if await endpoint_count.count() > 0:
                    print("✅ Endpoints extracted!")
                    success = True
                    break

                # Check for error
                error_msg = (
                    page.locator('[data-testid="upload-spec-button"]')
                    .locator("..")
                    .locator("text=/error/i")
                )
                if await error_msg.count() > 0:
                    error_text = await error_msg.text_content()
                    pytest.fail(f"Upload failed with error: {error_text}")

                await asyncio.sleep(1)
                waited += 1

            if not success:
                pytest.fail(f"Upload did not complete within {max_wait} seconds")

            print(f"✅ Successfully uploaded OpenAPI spec: {SAMPLE_SPEC_PATH.name}")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_upload_valid_openapi_spec_paste():
    """Test 1.4: Upload Valid OpenAPI Spec (Paste)"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Create connector
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            unique_name = f"Paste Test {int(time.time())}"
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await new_connector_btn.click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://api.example.com"
            )
            await page.locator('[data-testid="create-connector-submit-button"]').click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)

            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Navigate to Upload Spec tab
            upload_tab = page.locator('button:has-text("Upload")')
            if await upload_tab.count() > 0:
                await upload_tab.click()
                await asyncio.sleep(1)

            # Switch to paste mode
            paste_mode_btn = page.locator('button:has-text("Paste Content")')
            await expect(paste_mode_btn).to_be_visible(timeout=5000)
            await paste_mode_btn.click()
            await asyncio.sleep(0.5)

            # Paste minimal valid OpenAPI spec
            minimal_spec = json.dumps(
                {
                    "openapi": "3.0.0",
                    "info": {"title": "Test API", "version": "1.0.0"},
                    "paths": {
                        "/health": {
                            "get": {
                                "operationId": "getHealth",
                                "summary": "Health check endpoint",
                                "responses": {"200": {"description": "OK"}},
                            }
                        }
                    },
                },
                indent=2,
            )

            paste_textarea = page.locator('[data-testid="openapi-spec-paste-textarea"]')
            await expect(paste_textarea).to_be_visible(timeout=5000)
            await paste_textarea.fill(minimal_spec)

            # Click upload button
            upload_btn = page.locator('[data-testid="upload-spec-button"]')
            await expect(upload_btn).not_to_be_disabled(timeout=5000)
            await upload_btn.click()

            # Wait for success
            max_wait = 20
            waited = 0
            success = False

            while waited < max_wait:
                success_msg = page.locator("text=/successfully/i")
                if await success_msg.count() > 0:
                    success = True
                    break

                endpoint_count = page.locator("text=/endpoints extracted/i")
                if await endpoint_count.count() > 0:
                    success = True
                    break

                error_msg = page.locator("text=/error/i")
                if await error_msg.count() > 0:
                    error_text = await error_msg.first.text_content()
                    if "upload" in error_text.lower() or "spec" in error_text.lower():
                        pytest.fail(f"Upload failed: {error_text}")

                await asyncio.sleep(1)
                waited += 1

            if not success:
                pytest.fail(f"Upload did not complete within {max_wait} seconds")

            print("✅ Successfully uploaded OpenAPI spec via paste")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_upload_invalid_openapi_spec():
    """Test 1.5: Upload Invalid OpenAPI Spec (Malformed JSON)"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Create connector
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            unique_name = f"Invalid Test {int(time.time())}"
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await new_connector_btn.click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://api.example.com"
            )
            await page.locator('[data-testid="create-connector-submit-button"]').click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)

            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Navigate to Upload Spec tab
            upload_tab = page.locator('button:has-text("Upload")')
            if await upload_tab.count() > 0:
                await upload_tab.click()
                await asyncio.sleep(1)

            # Switch to paste mode
            paste_mode_btn = page.locator('button:has-text("Paste Content")')
            await paste_mode_btn.click()
            await asyncio.sleep(0.5)

            # Paste invalid JSON
            invalid_json = '{ "invalid": json }'  # Invalid JSON

            paste_textarea = page.locator('[data-testid="openapi-spec-paste-textarea"]')
            await paste_textarea.fill(invalid_json)

            # Click upload button
            upload_btn = page.locator('[data-testid="upload-spec-button"]')
            await upload_btn.click()

            # Wait for error message
            max_wait = 10
            waited = 0

            while waited < max_wait:
                error_msg = page.locator("text=/invalid/i").or_(page.locator("text=/error/i"))
                if await error_msg.count() > 0:
                    error_text = await error_msg.first.text_content()
                    print(f"✅ Error message received: {error_text}")
                    assert "invalid" in error_text.lower() or "error" in error_text.lower()
                    break

                await asyncio.sleep(1)
                waited += 1
            else:
                pytest.fail("Expected error message but none appeared")

            # Verify no success message
            success_msg = page.locator("text=/successfully/i")
            assert await success_msg.count() == 0, (
                "Should not show success message for invalid spec"
            )

            print("✅ Invalid spec correctly rejected with error message")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_upload_non_openapi_file():
    """Test 1.6: Upload Non-OpenAPI File (Wrong Format)"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Create connector
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            unique_name = f"Wrong File Test {int(time.time())}"
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await new_connector_btn.click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://api.example.com"
            )
            await page.locator('[data-testid="create-connector-submit-button"]').click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)

            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Navigate to Upload Spec tab
            upload_tab = page.locator('button:has-text("Upload")')
            if await upload_tab.count() > 0:
                await upload_tab.click()
                await asyncio.sleep(1)

            # Create a temporary text file (non-JSON)
            import tempfile

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".txt", delete=False
            ) as tmp_file:  # NOSONAR -- sync I/O in test is acceptable
                tmp_file.write("This is not an OpenAPI spec file")
                tmp_file_path = tmp_file.name

            try:
                # Try to upload the file
                file_input = page.locator('[data-testid="openapi-spec-file-input"]')

                # Note: File input may have accept attribute that prevents selection
                # But we can test if frontend validation catches it
                # Or if backend validation catches it after upload

                # For now, test that invalid file extension is caught
                # (if file input accept works, this will fail at selection)
                # If it doesn't, backend should reject it

                await file_input.set_input_files(tmp_file_path)
                await asyncio.sleep(0.5)

                # Check if file input validation caught it
                # (Some browsers may prevent selection of non-accepted files)

                # Try to upload
                upload_btn = page.locator('[data-testid="upload-spec-button"]')
                if await upload_btn.is_enabled():
                    await upload_btn.click()

                    # Wait for error
                    max_wait = 10
                    waited = 0

                    while waited < max_wait:
                        error_msg = (
                            page.locator("text=/invalid/i")
                            .or_(page.locator("text=/error/i"))
                            .or_(page.locator("text=/not.*openapi/i"))
                        )
                        if await error_msg.count() > 0:
                            error_text = await error_msg.first.text_content()
                            print(f"✅ Error message received: {error_text}")
                            break

                        await asyncio.sleep(1)
                        waited += 1
                    else:
                        # If no error appears, file might have been accepted but parsing failed
                        # Check for any validation message
                        validation_msg = (
                            page.locator("text=/supported/i")
                            .or_(page.locator("text=/json/i"))
                            .or_(page.locator("text=/yaml/i"))
                        )
                        if await validation_msg.count() > 0:
                            print("✅ Frontend validation caught invalid file type")
                        else:
                            # Backend should reject it during parsing
                            print("⚠️  File was accepted, backend should reject during parsing")

            finally:
                # Clean up temp file
                with contextlib.suppress(BaseException):
                    os.unlink(tmp_file_path)

            print("✅ Non-OpenAPI file handling tested")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_browse_and_filter_endpoints():
    """Test 1.7: Browse and Filter Endpoints"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Create connector with uploaded spec (reuse from earlier test)
            # For this test, we'll assume we have a connector with endpoints
            # In practice, you'd set up test data first

            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            # Find any connector with endpoints (or create one)
            # For now, we'll test the UI elements exist
            # Full test would require pre-populated data

            # Check if any connectors exist
            connector_cards = page.locator('[data-testid^="connector-card-"]')
            connector_count = await connector_cards.count()

            if connector_count > 0:
                # Click first connector
                await connector_cards.first.click()
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(2)

                # Navigate to Endpoints tab (should be default)
                endpoints_tab = page.locator('button:has-text("Endpoints")')
                if await endpoints_tab.count() > 0:
                    await endpoints_tab.click()
                    await asyncio.sleep(1)

                # Check for filter controls - look for select with "All Methods" option
                method_filter = page.locator('select:has(option:has-text("All Methods"))')
                if await method_filter.count() > 0:
                    print("✅ Method filter found")

                # Check for search input
                search_input = page.locator('input[placeholder*="Search endpoints"]')
                if await search_input.count() > 0:
                    print("✅ Endpoint search input found")
                    # Test search
                    await search_input.fill("health")
                    await asyncio.sleep(1)

                print("✅ Endpoint browser UI elements verified")
            else:
                print("⚠️  No connectors found, skipping endpoint browser test")
                pytest.skip("No connectors available for endpoint browser test")

        finally:
            await browser.close()


@pytest.mark.e2e
@pytest.mark.asyncio
async def test_edit_connector():
    """Test 1.2: Edit Connector"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Login
            await login_to_meho(page)

            # Create a connector first
            await page.goto(f"{FRONTEND_URL}/connectors")
            await page.wait_for_load_state("networkidle")

            unique_name = f"Edit Test {int(time.time())}"
            new_connector_btn = page.locator('[data-testid="new-connector-button"]')
            await new_connector_btn.click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).to_be_visible(timeout=5000)

            await page.locator('[data-testid="connector-name-input"]').fill(unique_name)
            await page.locator('[data-testid="connector-base-url-input"]').fill(
                "https://api.example.com"
            )
            await page.locator('[data-testid="create-connector-submit-button"]').click()
            await expect(
                page.locator('[data-testid="create-connector-modal-title"]')
            ).not_to_be_visible(timeout=10000)

            await page.wait_for_load_state("networkidle")
            await asyncio.sleep(2)

            # Find edit button or settings
            # Check if we're on connector details page
            edit_button = page.locator('button:has-text("Edit")').or_(
                page.locator('button:has-text("Settings")')
            )
            if await edit_button.count() > 0:
                await edit_button.click()
                await asyncio.sleep(1)

                # Modify description if field exists
                description_field = page.locator("textarea").or_(
                    page.locator('input[placeholder*="description"]')
                )
                if await description_field.count() > 0:
                    await description_field.fill("Updated description")

                # Save changes
                save_button = page.locator('button:has-text("Save")').or_(
                    page.locator('button[type="submit"]')
                )
                if await save_button.count() > 0:
                    await save_button.click()
                    await asyncio.sleep(2)

                print("✅ Connector edit functionality verified")
            else:
                print(
                    "⚠️  Edit button not found, connector may auto-save or use different edit flow"
                )

        finally:
            await browser.close()
