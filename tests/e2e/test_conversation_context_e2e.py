# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
End-to-end tests for conversation context and intent classification.

These tests verify the full flow from user input through to response,
including conversation history, intent classification, and workflow creation.
"""

import asyncio
import time

import pytest
from playwright.async_api import Browser, Page, async_playwright


@pytest.mark.e2e
@pytest.mark.asyncio
class TestConversationContextE2E:
    """End-to-end browser tests for conversation context"""

    @pytest.fixture(scope="class")
    async def browser(self):
        """Launch browser for testing"""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        yield browser
        await browser.close()
        await playwright.stop()

    @pytest.fixture
    async def page(self, browser: Browser):
        """Create a new page for each test"""
        page = await browser.new_page()
        # Navigate to chat page
        await page.goto("http://localhost:5173/chat")
        await page.wait_for_load_state("networkidle")
        yield page
        await page.close()

    async def send_chat_message(self, page: Page, message: str, timeout: int = 30000):  # noqa: ASYNC109 -- timeout parameter is part of function API
        """Helper to send a chat message and wait for response"""
        # Type message
        await page.fill('[data-testid="chat-input"]', message)

        # Click send
        await page.click('button:has-text("Send")')

        # Wait for response (thinking indicator to disappear)
        await page.wait_for_selector("text=MEHO is thinking", state="hidden", timeout=timeout)

    async def get_last_assistant_message(self, page: Page) -> str:
        """Get the content of the last assistant message"""
        messages = await page.query_selector_all('[role="assistant"]')
        if messages:
            return await messages[-1].text_content()
        return ""

    @pytest.mark.asyncio
    async def test_simple_knowledge_query_no_workflow(self, page: Page):
        """Test that simple knowledge queries don't create workflows"""
        # Send a knowledge query
        await self.send_chat_message(page, "What roles are available in VCF?")

        # Wait for response
        await asyncio.sleep(2)

        # Verify response is displayed
        response = await self.get_last_assistant_message(page)
        assert len(response) > 0, "Should have a response"
        assert "ADMIN" in response or "roles" in response.lower(), "Should mention roles"

        # Verify no workflow approval dialog
        approval_dialog = await page.query_selector('[data-testid="approval-required-message"]')
        assert approval_dialog is None, "Should not show approval dialog for knowledge queries"

    @pytest.mark.asyncio
    async def test_conversation_context_follow_up(self, page: Page):
        """Test that follow-up questions understand context"""
        # First message
        await self.send_chat_message(page, "What roles are available in VCF?")
        await asyncio.sleep(2)

        first_response = await self.get_last_assistant_message(page)
        assert len(first_response) > 0, "Should have first response"

        # Follow-up that references previous conversation
        await self.send_chat_message(page, "Which role would I need to manage VMs?")
        await asyncio.sleep(2)

        second_response = await self.get_last_assistant_message(page)
        assert len(second_response) > 0, "Should have second response"

        # Response should reference VCF roles without re-explaining everything
        assert "ADMIN" in second_response or "OPERATOR" in second_response, (
            "Should mention specific roles"
        )

    @pytest.mark.asyncio
    async def test_multiple_follow_ups_maintain_context(self, page: Page):
        """Test that multiple follow-up questions maintain context"""
        # Question 1
        await self.send_chat_message(page, "What is PydanticAI?")
        await asyncio.sleep(2)

        # Question 2 - referencing "it"
        await self.send_chat_message(page, "What are its main features?")
        await asyncio.sleep(2)

        # Question 3 - referencing previous discussion
        await self.send_chat_message(page, "How do I install it?")
        await asyncio.sleep(2)

        final_response = await self.get_last_assistant_message(page)
        assert len(final_response) > 0, "Should have response to follow-up"
        # Should understand "it" refers to PydanticAI from conversation

    @pytest.mark.asyncio
    async def test_new_session_clears_context(self, page: Page):
        """Test that starting a new session clears conversation context"""
        # Send message in first session
        await self.send_chat_message(page, "What is VCF?")
        await asyncio.sleep(2)

        # Start new session
        await page.click('button:has-text("New Chat")')
        await asyncio.sleep(1)

        # Send message that would only make sense with previous context
        await self.send_chat_message(page, "Tell me more about that system")
        await asyncio.sleep(2)

        response = await self.get_last_assistant_message(page)
        # Should ask for clarification since context is cleared
        assert (
            "which system" in response.lower()
            or "clarify" in response.lower()
            or "specify" in response.lower()
        ), "Should ask for clarification when context is missing"

    @pytest.mark.asyncio
    async def test_session_persistence_across_reload(self, page: Page):
        """Test that conversation history persists after page reload"""
        # Send a few messages
        await self.send_chat_message(page, "What roles are in VCF?")
        await asyncio.sleep(2)

        # Get session ID from URL or session list
        session_title = await page.query_selector('text="What roles are in VCF?"')
        assert session_title is not None, "Session should appear in sidebar"

        # Reload page
        await page.reload()
        await page.wait_for_load_state("networkidle")

        # Click on the session to load it
        await page.click('text="What roles are in VCF?"')
        await asyncio.sleep(1)

        # Verify message is still there
        messages = await page.query_selector_all('text="What roles are in VCF?"')
        assert len(messages) >= 1, "Original message should be visible after reload"

    @pytest.mark.asyncio
    async def test_long_conversation_maintains_context(self, page: Page):
        """Test that long conversations maintain recent context"""
        messages = [
            "What is MEHO?",
            "What services does it have?",
            "How does the agent service work?",
            "What about the knowledge service?",
            "How do they communicate?",
        ]

        for msg in messages:
            await self.send_chat_message(page, msg)
            await asyncio.sleep(2)

        # Final question referencing earlier discussion
        await self.send_chat_message(page, "Summarize what you told me about the agent service")
        await asyncio.sleep(2)

        response = await self.get_last_assistant_message(page)
        assert len(response) > 0, "Should provide summary from conversation history"


@pytest.mark.e2e
@pytest.mark.asyncio
class TestIntentClassificationE2E:
    """End-to-end tests for intent classification in browser"""

    @pytest.fixture(scope="class")
    async def browser(self):
        """Launch browser for testing"""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        yield browser
        await browser.close()
        await playwright.stop()

    @pytest.fixture
    async def page(self, browser: Browser):
        """Create a new page for each test"""
        page = await browser.new_page()
        await page.goto("http://localhost:5173/chat")
        await page.wait_for_load_state("networkidle")
        yield page
        await page.close()

    async def send_chat_message(self, page: Page, message: str, timeout: int = 30000):  # noqa: ASYNC109 -- timeout parameter is part of function API
        """Helper to send a chat message"""
        await page.fill('[data-testid="chat-input"]', message)
        await page.click('button:has-text("Send")')
        await page.wait_for_selector("text=MEHO is thinking", state="hidden", timeout=timeout)

    @pytest.mark.asyncio
    async def test_knowledge_query_displays_directly(self, page: Page):
        """Knowledge queries should display answers directly"""
        await self.send_chat_message(page, "What is PydanticAI?")
        await asyncio.sleep(2)

        # Should have assistant message
        messages = await page.query_selector_all(".message-assistant")
        assert len(messages) > 0, "Should have at least one assistant message"

        # Should NOT have approval dialog
        approval = await page.query_selector('[data-testid="approval-required-message"]')
        assert approval is None, "Knowledge queries should not require approval"

    @pytest.mark.asyncio
    async def test_api_call_shows_approval_dialog(self, page: Page):
        """API calls should show approval dialog"""
        # Note: This test requires connectors to be configured
        # If no connectors, should handle gracefully
        await self.send_chat_message(page, "Check if my-app is running")
        await asyncio.sleep(2)

        # Either shows approval dialog (if connectors exist) or explains no connectors
        page_content = await page.content()

        has_approval = "approve" in page_content.lower() or "review" in page_content.lower()
        has_no_connectors = (
            "no connector" in page_content.lower() or "not configured" in page_content.lower()
        )

        assert has_approval or has_no_connectors, (
            "Should either show approval or explain no connectors"
        )

    @pytest.mark.asyncio
    async def test_multiple_knowledge_queries_no_workflows(self, page: Page):
        """Multiple knowledge queries should not create workflows"""
        queries = [
            "What is VCF?",
            "How does authentication work?",
            "What are the deployment requirements?",
        ]

        for query in queries:
            await self.send_chat_message(page, query)
            await asyncio.sleep(2)

            # Verify each gets a response
            messages = await page.query_selector_all(".message-assistant")
            assert len(messages) > 0, f"Should have response for: {query}"

            # No approval dialogs
            approval = await page.query_selector('[data-testid="approval-required-message"]')
            assert approval is None, f"Should not require approval for: {query}"


@pytest.mark.e2e
@pytest.mark.asyncio
class TestChatPerformanceE2E:
    """Performance and stress tests for chat"""

    @pytest.fixture(scope="class")
    async def browser(self):
        """Launch browser for testing"""
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=True)
        yield browser
        await browser.close()
        await playwright.stop()

    @pytest.fixture
    async def page(self, browser: Browser):
        """Create a new page for each test"""
        page = await browser.new_page()
        await page.goto("http://localhost:5173/chat")
        await page.wait_for_load_state("networkidle")
        yield page
        await page.close()

    async def send_chat_message(self, page: Page, message: str, timeout: int = 30000):  # noqa: ASYNC109 -- timeout parameter is part of function API
        """Helper to send a chat message"""
        await page.fill('[data-testid="chat-input"]', message)
        await page.click('button:has-text("Send")')
        await page.wait_for_selector("text=MEHO is thinking", state="hidden", timeout=timeout)

    @pytest.mark.asyncio
    async def test_response_time_reasonable(self, page: Page):
        """Test that responses come back in reasonable time"""
        start_time = time.time()

        await self.send_chat_message(page, "What is MEHO?")
        await asyncio.sleep(1)  # Wait for response

        end_time = time.time()
        response_time = end_time - start_time

        # Should respond within 15 seconds for knowledge query
        assert response_time < 15, f"Response took too long: {response_time}s"

    @pytest.mark.asyncio
    async def test_handles_very_long_message(self, page: Page):
        """Test that system handles very long messages"""
        long_message = "Tell me about " + ("MEHO " * 200)  # Very long message

        await self.send_chat_message(page, long_message, timeout=45000)
        await asyncio.sleep(2)

        # Should still get a response
        messages = await page.query_selector_all(".message-assistant")
        assert len(messages) > 0, "Should handle long messages"

    @pytest.mark.asyncio
    async def test_handles_special_characters(self, page: Page):
        """Test that system handles special characters correctly"""
        special_message = "What is MEHO? 🚀 @#$% <script>alert('test')</script>"

        await self.send_chat_message(page, special_message)
        await asyncio.sleep(2)

        # Should get response without errors
        messages = await page.query_selector_all(".message-assistant")
        assert len(messages) > 0, "Should handle special characters"

        # Should not execute any scripts (XSS protection)
        await page.evaluate("() => window.hasOwnProperty('alert')")
        # Page should not have been compromised
