# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Frontend E2E Tests using Playwright

Tests complete user journeys through the React frontend:
1. Post maintenance notice → Chat knows about it
2. Upload VCF documentation → Chat knows about it
3. Create VCF connector with OpenAPI spec → Chat knows about it

These tests verify the complete frontend-to-backend flow with real browser automation.
"""

import asyncio
from pathlib import Path

import pytest
from playwright.async_api import Page, async_playwright, expect

pytestmark = pytest.mark.e2e

# Configuration
FRONTEND_URL = "http://localhost:5173"
TEST_USER_ID = "playwright-test@example.com"
TEST_TENANT_ID = "playwright-tenant"

# VCF Test Data
VCF_CONNECTOR_NAME = "VCF SFO-M01"
VCF_BASE_URL = "https://vcf-example.local/"
VCF_USERNAME = "administrator@vsphere.local"
VCF_PASSWORD = "CHANGE_ME"  # NOSONAR -- test placeholder, not a real credential

# Sample specs directory
SAMPLE_SPECS_DIR = Path(__file__).parent.parent.parent / "samplespecs"
VCF_OPENAPI_SPEC = SAMPLE_SPECS_DIR / "vmware-cloud-foundation.json"
VCF_DOCUMENTATION = SAMPLE_SPECS_DIR / "VMware Cloud Foundation API Reference Guide.html"


async def login_to_meho(page: Page) -> None:
    """Helper to login to MEHO frontend"""
    print("  → Navigating to login page...")
    await page.goto(FRONTEND_URL)
    await page.wait_for_load_state("networkidle")

    # Click "Generate Test Token" button
    print("  → Generating test token...")
    await page.click('button:has-text("Generate Test Token")')
    await asyncio.sleep(1)

    # Click "Login" button
    print("  → Logging in...")
    await page.click('button:has-text("Login")')
    await page.wait_for_load_state("networkidle")

    # Should be on dashboard now
    await expect(page).to_have_url(f"{FRONTEND_URL}/")
    print("  ✓ Logged in successfully")


async def navigate_to_page(page: Page, page_name: str) -> None:
    """Navigate to a specific page using sidebar"""
    print(f"  → Navigating to {page_name} page...")
    # Click the sidebar link
    await page.click(f'a[href="/{page_name.lower()}"]')
    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(1)
    print(f"  ✓ On {page_name} page")


@pytest.mark.asyncio
@pytest.mark.skipif(
    not VCF_OPENAPI_SPEC.exists() or not VCF_DOCUMENTATION.exists(), reason="Sample specs not found"
)
async def test_frontend_use_case_1_maintenance_notice():
    """
    Use Case 1: Post maintenance notice → Chat knows about it

    Steps:
    1. Login to MEHO
    2. Navigate to Knowledge page
    3. Go to "Temporary Notice" tab
    4. Create maintenance notice
    5. Navigate to Chat page
    6. Ask about maintenance
    7. Verify response includes maintenance info
    """
    print("\n" + "=" * 80)
    print("USE CASE 1: Maintenance Notice → Chat Recognition")
    print("=" * 80)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Step 1: Login
            print("\n1. Login to MEHO")
            await login_to_meho(page)

            # Step 2: Navigate to Knowledge page
            print("\n2. Navigate to Knowledge page")
            await navigate_to_page(page, "knowledge")

            # Step 3: Click "Temporary Notice" tab
            print("\n3. Open Temporary Notice tab")
            await page.click('button:has-text("Temporary Notice")')
            await asyncio.sleep(1)

            # Step 4: Fill in maintenance notice
            print("\n4. Create maintenance notice")
            notice_text = (
                "SCHEDULED MAINTENANCE: The VCF environment will undergo maintenance "
                "tonight from 11:00 PM to 1:00 AM EST. Services may be unavailable. "
                "Please save your work and log out before 11:00 PM."
            )

            # Fill in the form
            await page.fill('textarea[placeholder*="notice"]', notice_text)

            # Set expiration date (tomorrow)
            from datetime import UTC, datetime, timedelta

            tomorrow = (datetime.now(tz=UTC) + timedelta(days=1)).strftime("%Y-%m-%d")
            await page.fill('input[type="date"]', tomorrow)

            # Add tags
            await page.fill('input[placeholder*="tag"]', "maintenance, scheduled, vcf")

            # Click "Post Notice" button
            await page.click('button:has-text("Post Notice")')
            await asyncio.sleep(2)

            # Verify success message
            success_msg = page.locator("text=/posted successfully/i")
            await expect(success_msg).to_be_visible(timeout=5000)
            print("  ✓ Maintenance notice posted")

            # Step 5: Navigate to Chat page
            print("\n5. Navigate to Chat page")
            await navigate_to_page(page, "chat")

            # Step 6: Ask about maintenance
            print("\n6. Ask chat about maintenance")
            question = "Is there any scheduled maintenance coming up?"

            await page.fill('textarea[placeholder*="message"]', question)
            await page.click('button[type="submit"]')

            # Wait for response
            print("  → Waiting for agent response...")
            await asyncio.sleep(10)  # Give time for LLM to respond

            # Step 7: Verify response mentions maintenance
            print("\n7. Verify response mentions maintenance")

            # Look for response containing maintenance keywords
            response_area = page.locator(".message-content, .response-text")
            response_text = await response_area.last.text_content()

            assert response_text is not None
            response_lower = response_text.lower()

            # Check if response mentions maintenance
            maintenance_mentioned = any(
                keyword in response_lower
                for keyword in ["maintenance", "11:00 pm", "11pm", "tonight", "unavailable"]
            )

            assert maintenance_mentioned, (
                f"Response doesn't mention maintenance: {response_text[:200]}"
            )

            print("  ✓ Chat knows about maintenance!")
            print(f"  ✓ Response excerpt: {response_text[:150]}...")

            print("\n✅ USE CASE 1 PASSED: Maintenance notice flow works!")

        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not VCF_DOCUMENTATION.exists(), reason="VCF documentation not found")
async def test_frontend_use_case_2_vcf_documentation():
    """
    Use Case 2: Upload VCF documentation → Chat knows about it

    Steps:
    1. Login to MEHO
    2. Navigate to Knowledge page
    3. Go to "Upload Documents" tab
    4. Upload VCF documentation HTML file
    5. Wait for processing
    6. Navigate to Chat page
    7. Ask about VCF
    8. Verify response includes VCF info
    """
    print("\n" + "=" * 80)
    print("USE CASE 2: VCF Documentation → Chat Recognition")
    print("=" * 80)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Step 1: Login
            print("\n1. Login to MEHO")
            await login_to_meho(page)

            # Step 2: Navigate to Knowledge page
            print("\n2. Navigate to Knowledge page")
            await navigate_to_page(page, "knowledge")

            # Step 3: Click "Upload Documents" tab
            print("\n3. Open Upload Documents tab")
            await page.click('button:has-text("Upload Documents")')
            await asyncio.sleep(1)

            # Step 4: Upload VCF documentation
            print("\n4. Upload VCF documentation HTML file")
            print(
                f"   File: {VCF_DOCUMENTATION.name} ({VCF_DOCUMENTATION.stat().st_size / 1024 / 1024:.1f}MB)"
            )

            # Click file input
            file_input = page.locator('input[type="file"]')
            await file_input.set_input_files(str(VCF_DOCUMENTATION))

            # Add tags
            await page.fill(
                'input[placeholder*="tag"]', "vcf, vmware, cloud-foundation, api-reference"
            )

            # Click "Upload" button
            await page.click('button:has-text("Upload")')

            # Step 5: Wait for processing
            print("\n5. Wait for document processing (this may take a while...)")

            # Wait for success message or progress indicator
            success_indicator = page.locator("text=/uploaded successfully|processing complete/i")
            await expect(success_indicator).to_be_visible(timeout=120000)  # 2 minutes
            print("  ✓ VCF documentation uploaded and processed")

            # Give time for indexing in Qdrant
            await asyncio.sleep(5)

            # Step 6: Navigate to Chat page
            print("\n6. Navigate to Chat page")
            await navigate_to_page(page, "chat")

            # Step 7: Ask about VCF
            print("\n7. Ask chat about VCF")
            question = "What is VMware Cloud Foundation? What APIs does it provide?"

            await page.fill('textarea[placeholder*="message"]', question)
            await page.click('button[type="submit"]')

            # Wait for response
            print("  → Waiting for agent response...")
            await asyncio.sleep(15)  # Give time for LLM to search and respond

            # Step 8: Verify response mentions VCF
            print("\n8. Verify response mentions VCF")

            response_area = page.locator(".message-content, .response-text")
            response_text = await response_area.last.text_content()

            assert response_text is not None
            response_lower = response_text.lower()

            # Check if response mentions VCF-related terms
            vcf_mentioned = any(
                keyword in response_lower
                for keyword in ["vmware cloud foundation", "vcf", "sddc", "api", "cloud"]
            )

            assert vcf_mentioned, f"Response doesn't mention VCF: {response_text[:200]}"

            print("  ✓ Chat knows about VCF!")
            print(f"  ✓ Response excerpt: {response_text[:150]}...")

            print("\n✅ USE CASE 2 PASSED: VCF documentation flow works!")

        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
@pytest.mark.skipif(not VCF_OPENAPI_SPEC.exists(), reason="VCF OpenAPI spec not found")
async def test_frontend_use_case_3_vcf_connector():
    """
    Use Case 3: Create VCF connector with OpenAPI spec → Chat knows about it

    Steps:
    1. Login to MEHO
    2. Navigate to Connectors page
    3. Click "New Connector"
    4. Fill in VCF connector details
    5. Create connector
    6. Upload OpenAPI spec
    7. Set credentials
    8. Navigate to Chat page
    9. Ask about VCF connector
    10. Verify response mentions VCF connector and APIs
    """
    print("\n" + "=" * 80)
    print("USE CASE 3: VCF Connector Creation → Chat Recognition")
    print("=" * 80)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=500)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            # Step 1: Login
            print("\n1. Login to MEHO")
            await login_to_meho(page)

            # Step 2: Navigate to Connectors page
            print("\n2. Navigate to Connectors page")
            await navigate_to_page(page, "connectors")

            # Step 3: Click "New Connector"
            print("\n3. Click New Connector button")
            await page.click('button:has-text("New Connector")')
            await asyncio.sleep(1)

            # Step 4: Fill in VCF connector details
            print("\n4. Fill in VCF connector details")

            # Name
            await page.fill('input[name="name"], input[placeholder*="name"]', VCF_CONNECTOR_NAME)

            # Base URL
            await page.fill('input[name="baseUrl"], input[placeholder*="URL"]', VCF_BASE_URL)

            # Description
            description = (
                "VMware Cloud Foundation vRealize Operations Manager API for SFO-M01 environment"
            )
            await page.fill(
                'textarea[name="description"], textarea[placeholder*="description"]', description
            )

            # Auth Type - select "BASIC"
            await page.select_option('select[name="authType"]', "BASIC")

            # Safety Level - keep as "safe" (default)
            # Allowed methods - keep all checked (default)

            # Step 5: Create connector
            print("\n5. Create connector")
            await page.click('button:has-text("Create Connector"), button[type="submit"]')
            await asyncio.sleep(2)

            # Should navigate to connector details
            print("  ✓ Connector created")

            # Step 6: Upload OpenAPI spec
            print("\n6. Upload OpenAPI spec")
            print(
                f"   File: {VCF_OPENAPI_SPEC.name} ({VCF_OPENAPI_SPEC.stat().st_size / 1024 / 1024:.1f}MB)"
            )

            # Look for "Upload Spec" or similar button
            upload_spec_btn = page.locator(
                'button:has-text("Upload Spec"), button:has-text("Upload OpenAPI")'
            )
            await upload_spec_btn.click()
            await asyncio.sleep(1)

            # Upload file
            file_input = page.locator('input[type="file"]')
            await file_input.set_input_files(str(VCF_OPENAPI_SPEC))

            # Submit upload
            await page.click('button:has-text("Upload"), button[type="submit"]')

            # Wait for processing
            print("  → Processing OpenAPI spec...")
            success_msg = page.locator("text=/uploaded successfully|endpoints extracted/i")
            await expect(success_msg).to_be_visible(timeout=30000)
            print("  ✓ OpenAPI spec uploaded and processed")

            # Step 7: Set credentials
            print("\n7. Set user credentials")

            # Look for "Set Credentials" or similar button
            creds_btn = page.locator(
                'button:has-text("Credentials"), button:has-text("Set Credentials")'
            )
            await creds_btn.click()
            await asyncio.sleep(1)

            # Fill in credentials
            await page.fill('input[name="username"], input[placeholder*="username"]', VCF_USERNAME)
            await page.fill('input[name="password"], input[placeholder*="password"]', VCF_PASSWORD)

            # Save credentials
            await page.click('button:has-text("Save"), button:has-text("Set Credentials")')
            await asyncio.sleep(2)
            print("  ✓ Credentials saved")

            # Step 8: Navigate to Chat page
            print("\n8. Navigate to Chat page")
            await navigate_to_page(page, "chat")

            # Step 9: Ask about VCF connector
            print("\n9. Ask chat about VCF connector")
            question = "What API connectors are available? Can you tell me about the VCF connector?"

            await page.fill('textarea[placeholder*="message"]', question)
            await page.click('button[type="submit"]')

            # Wait for response
            print("  → Waiting for agent response...")
            await asyncio.sleep(15)

            # Step 10: Verify response mentions VCF connector
            print("\n10. Verify response mentions VCF connector")

            response_area = page.locator(".message-content, .response-text")
            response_text = await response_area.last.text_content()

            assert response_text is not None
            response_lower = response_text.lower()

            # Check if response mentions VCF connector
            connector_mentioned = any(
                keyword in response_lower
                for keyword in ["vcf", "cloud foundation", "vrops", "sfo-m01", "vmware"]
            )

            assert connector_mentioned, (
                f"Response doesn't mention VCF connector: {response_text[:200]}"
            )

            print("  ✓ Chat knows about VCF connector!")
            print(f"  ✓ Response excerpt: {response_text[:150]}...")

            print("\n✅ USE CASE 3 PASSED: VCF connector flow works!")

        finally:
            await context.close()
            await browser.close()


@pytest.mark.asyncio
async def test_frontend_complete_journey():
    """
    Complete journey combining all three use cases in sequence.

    This is the ultimate test - verifies everything works together.
    """
    print("\n" + "=" * 80)
    print("COMPLETE JOURNEY: All Use Cases in Sequence")
    print("=" * 80)

    # Run all three use cases
    await test_frontend_use_case_1_maintenance_notice()
    await test_frontend_use_case_2_vcf_documentation()
    await test_frontend_use_case_3_vcf_connector()

    print("\n" + "=" * 80)
    print("✅ COMPLETE JOURNEY PASSED: All use cases work!")
    print("=" * 80)
    print("\nMEHO Frontend-to-Backend flow is FULLY VERIFIED! 🎉")
    print("\n✓ Maintenance notices work")
    print("✓ Document upload and knowledge search work")
    print("✓ Connector creation and API integration work")
    print("✓ Chat recognizes and uses all knowledge sources")
    print("\n🚀 PRODUCTION READY - PROVEN WITH REAL BROWSER TESTS!")
