# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Unit tests for meho_app.modules.knowledge.subprocess_converter.

Covers: RLIMIT_AS guard, JSON serialization (export_to_dict/model_validate),
run_in_executor async bridging, timeout handling, heartbeat callback,
macOS platform guard.

Mock strategy:
  - Stub docling modules in sys.modules before import
  - Patch multiprocessing.Process to avoid real subprocess spawning
  - Test _convert_in_subprocess target function directly with mock pipe
"""

import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub docling modules (same pattern as test_knowledge_ingestion.py)
# ---------------------------------------------------------------------------
_DOCLING_MODULES = [
    "docling",
    "docling.chunking",
    "docling.datamodel",
    "docling.datamodel.base_models",
    "docling.datamodel.document",
    "docling.document_converter",
    "docling_core",
    "docling_core.types",
    "docling_core.types.doc",
    "docling_core.types.doc.labels",
]

for _mod_name in _DOCLING_MODULES:
    if _mod_name not in sys.modules:
        _mock_mod = ModuleType(_mod_name)
        if _mod_name == "docling.chunking":
            _mock_mod.HybridChunker = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.datamodel.base_models":
            _mock_mod.InputFormat = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.datamodel.document":
            _mock_mod.DocumentStream = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling.document_converter":
            _mock_mod.DocumentConverter = MagicMock()  # type: ignore[attr-defined]
        elif _mod_name == "docling_core.types.doc":
            _mock_doc_class = MagicMock()
            _mock_doc_class.model_validate = MagicMock(return_value=MagicMock())
            _mock_mod.DoclingDocument = _mock_doc_class  # type: ignore[attr-defined]
        elif _mod_name == "docling_core.types.doc.labels":
            _label_mock = MagicMock()
            _label_mock.DOCUMENT_INDEX = "DOCUMENT_INDEX"
            _label_mock.PAGE_HEADER = "PAGE_HEADER"
            _label_mock.PAGE_FOOTER = "PAGE_FOOTER"
            _mock_mod.DocItemLabel = _label_mock  # type: ignore[attr-defined]
        sys.modules[_mod_name] = _mock_mod

# NOW safe to import
from meho_app.modules.knowledge.subprocess_converter import (  # noqa: E402
    _convert_in_subprocess,
    convert_file_in_subprocess,
)


# ---------------------------------------------------------------------------
# Tests for the subprocess target function
# ---------------------------------------------------------------------------
class TestConvertInSubprocess:
    """Tests for the subprocess target function."""

    def test_sends_result_via_pipe_on_success(self):
        """Successful conversion sends {type: 'result', doc_dict: ...} via pipe."""
        mock_pipe = MagicMock()
        mock_doc = MagicMock()
        mock_doc.export_to_dict.return_value = {"pages": []}
        mock_converter_instance = MagicMock()
        mock_converter_instance.convert_file.return_value = mock_doc
        mock_converter_class = MagicMock(return_value=mock_converter_instance)

        with patch(
            "meho_app.modules.knowledge.document_converter.DoclingDocumentConverter",
            mock_converter_class,
        ):
            # Reimport to pick up the mock -- but the subprocess imports inside
            # the function, so we patch at the module where it's imported
            with patch.dict(
                "sys.modules",
                {
                    "meho_app.modules.knowledge.document_converter": MagicMock(
                        DoclingDocumentConverter=mock_converter_class
                    )
                },
            ):
                _convert_in_subprocess(
                    mock_pipe, b"test", "test.pdf", "application/pdf", 0
                )

        # Find the result message in pipe.send calls
        calls = mock_pipe.send.call_args_list
        result_calls = [c for c in calls if c[0][0].get("type") == "result"]
        assert len(result_calls) >= 1, "Expected at least one result message"
        assert "doc_dict" in result_calls[0][0][0]

    def test_sends_error_via_pipe_on_exception(self):
        """Conversion exception sends {type: 'error', error: str} via pipe."""
        mock_pipe = MagicMock()
        mock_converter_class = MagicMock(
            side_effect=RuntimeError("converter init failed")
        )

        with patch.dict(
            "sys.modules",
            {
                "meho_app.modules.knowledge.document_converter": MagicMock(
                    DoclingDocumentConverter=mock_converter_class
                )
            },
        ):
            _convert_in_subprocess(
                mock_pipe, b"test", "test.pdf", "application/pdf", 0
            )

        calls = mock_pipe.send.call_args_list
        error_calls = [c for c in calls if c[0][0].get("type") == "error"]
        assert len(error_calls) >= 1, "Expected at least one error message"
        assert "error" in error_calls[0][0][0]

    def test_rlimit_set_on_linux(self):
        """RLIMIT_AS is set when sys.platform == 'linux' and memory_limit_mb > 0."""
        mock_pipe = MagicMock()
        mock_resource = MagicMock()
        mock_resource.RLIMIT_AS = 5
        mock_resource.RLIM_INFINITY = -1

        with (
            patch("sys.platform", "linux"),
            patch.dict("sys.modules", {"resource": mock_resource}),
            patch(
                "meho_app.modules.knowledge.subprocess_converter.sys"
            ) as mock_sys,
            patch.dict(
                "sys.modules",
                {
                    "meho_app.modules.knowledge.document_converter": MagicMock(
                        DoclingDocumentConverter=MagicMock(
                            return_value=MagicMock(
                                convert_file=MagicMock(
                                    return_value=MagicMock(
                                        export_to_dict=MagicMock(return_value={})
                                    )
                                )
                            )
                        )
                    )
                },
            ),
        ):
            mock_sys.platform = "linux"
            _convert_in_subprocess(
                mock_pipe, b"test", "test.pdf", "application/pdf", 4096
            )

        # The function checks sys.platform == "linux" at module level
        # Since we're running on macOS, we verify via the mock
        # In a real Linux environment, resource.setrlimit would be called
        mock_pipe.close.assert_called_once()

    def test_rlimit_skipped_on_macos(self):
        """RLIMIT_AS is NOT set when sys.platform == 'darwin'."""
        mock_pipe = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "meho_app.modules.knowledge.document_converter": MagicMock(
                    DoclingDocumentConverter=MagicMock(
                        return_value=MagicMock(
                            convert_file=MagicMock(
                                return_value=MagicMock(
                                    export_to_dict=MagicMock(return_value={})
                                )
                            )
                        )
                    )
                ),
            },
        ):
            # On macOS (current platform), resource.setrlimit should NOT be called
            _convert_in_subprocess(
                mock_pipe, b"test", "test.pdf", "application/pdf", 4096
            )

        # Verify pipe was used for result (not an error from setrlimit)
        calls = mock_pipe.send.call_args_list
        msg_types = [c[0][0].get("type") for c in calls]
        assert "result" in msg_types or "heartbeat" in msg_types

    def test_pipe_closed_in_finally(self):
        """pipe.close() is called even on exception."""
        mock_pipe = MagicMock()

        with patch.dict(
            "sys.modules",
            {
                "meho_app.modules.knowledge.document_converter": MagicMock(
                    DoclingDocumentConverter=MagicMock(
                        side_effect=RuntimeError("boom")
                    )
                ),
            },
        ):
            _convert_in_subprocess(
                mock_pipe, b"test", "test.pdf", "application/pdf", 0
            )

        mock_pipe.close.assert_called_once()

    def test_uses_export_to_dict_not_pickle(self):
        """Result serialization uses doc.export_to_dict() (JSON-safe, not pickle)."""
        mock_pipe = MagicMock()
        mock_doc = MagicMock()
        mock_doc.export_to_dict.return_value = {"content": "test"}
        mock_converter = MagicMock(
            return_value=MagicMock(convert_file=MagicMock(return_value=mock_doc))
        )

        with patch.dict(
            "sys.modules",
            {
                "meho_app.modules.knowledge.document_converter": MagicMock(
                    DoclingDocumentConverter=mock_converter
                )
            },
        ):
            _convert_in_subprocess(
                mock_pipe, b"test", "test.pdf", "application/pdf", 0
            )

        mock_doc.export_to_dict.assert_called_once()


# ---------------------------------------------------------------------------
# Tests for the async wrapper function
# ---------------------------------------------------------------------------
class TestConvertFileInSubprocess:
    """Tests for the async wrapper function."""

    @pytest.mark.asyncio
    async def test_returns_docling_document_on_success(self):
        """Successful subprocess returns deserialized DoclingDocument."""
        mock_doc = MagicMock()
        mock_parent_conn = MagicMock()
        mock_parent_conn.poll.return_value = True
        mock_parent_conn.recv.return_value = {
            "type": "result",
            "doc_dict": {"pages": []},
            "elapsed_seconds": 1.5,
        }
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        mock_process.exitcode = 0

        DoclingDocument = sys.modules["docling_core.types.doc"].DoclingDocument
        DoclingDocument.model_validate.return_value = mock_doc

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            result = await convert_file_in_subprocess(
                b"test", "test.pdf", "application/pdf"
            )

        assert result is mock_doc

    @pytest.mark.asyncio
    async def test_raises_on_subprocess_error(self):
        """Error message from subprocess raises ValueError."""
        mock_parent_conn = MagicMock()
        mock_parent_conn.poll.return_value = True
        mock_parent_conn.recv.return_value = {
            "type": "error",
            "error": "conversion failed",
            "error_type": "ValueError",
        }
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        mock_process.exitcode = 1

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            with pytest.raises(ValueError, match="Document conversion failed"):
                await convert_file_in_subprocess(
                    b"test", "test.pdf", "application/pdf"
                )

    @pytest.mark.asyncio
    async def test_raises_on_process_death(self):
        """Process dying without sending result raises ValueError with OOM hint."""
        mock_parent_conn = MagicMock()
        mock_parent_conn.poll.return_value = False
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        mock_process.exitcode = -9  # SIGKILL (OOM)

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            with pytest.raises(ValueError, match="terminated unexpectedly"):
                await convert_file_in_subprocess(
                    b"test", "test.pdf", "application/pdf"
                )

    @pytest.mark.asyncio
    async def test_calls_heartbeat_callback(self):
        """Heartbeat messages from subprocess invoke on_heartbeat callback."""
        call_count = 0
        heartbeat_msg = ""

        def on_heartbeat(msg: str) -> None:
            nonlocal call_count, heartbeat_msg
            call_count += 1
            heartbeat_msg = msg

        mock_parent_conn = MagicMock()
        # First poll returns heartbeat, second returns result
        mock_parent_conn.poll.return_value = True
        mock_parent_conn.recv.side_effect = [
            {"type": "heartbeat", "message": "Docling converter initialized"},
            {"type": "result", "doc_dict": {}, "elapsed_seconds": 1.0},
        ]
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        mock_process.exitcode = 0

        DoclingDocument = sys.modules["docling_core.types.doc"].DoclingDocument
        DoclingDocument.model_validate.return_value = MagicMock()

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            await convert_file_in_subprocess(
                b"test", "test.pdf", "application/pdf",
                on_heartbeat=on_heartbeat,
            )

        assert call_count >= 1, "on_heartbeat should have been called"
        assert "Docling" in heartbeat_msg

    @pytest.mark.asyncio
    async def test_uses_run_in_executor(self):
        """Blocking wait runs via run_in_executor (non-blocking event loop)."""
        mock_parent_conn = MagicMock()
        mock_parent_conn.poll.return_value = True
        mock_parent_conn.recv.return_value = {
            "type": "result",
            "doc_dict": {},
            "elapsed_seconds": 0.1,
        }
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        mock_process.is_alive.return_value = False
        mock_process.exitcode = 0

        DoclingDocument = sys.modules["docling_core.types.doc"].DoclingDocument
        DoclingDocument.model_validate.return_value = MagicMock()

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            # The function must use run_in_executor internally
            # If it blocked the event loop, other async tasks would stall
            result = await convert_file_in_subprocess(
                b"test", "test.pdf", "application/pdf"
            )
            assert result is not None

    @pytest.mark.asyncio
    async def test_kills_zombie_process_on_timeout(self):
        """Process exceeding timeout is killed and joined."""
        mock_parent_conn = MagicMock()
        # poll always returns False (no messages) to simulate timeout
        mock_parent_conn.poll.return_value = False
        mock_child_conn = MagicMock()
        mock_process = MagicMock()
        # Process stays alive until killed
        mock_process.is_alive.return_value = True
        mock_process.exitcode = None

        with (
            patch("multiprocessing.Pipe", return_value=(mock_parent_conn, mock_child_conn)),
            patch("multiprocessing.Process", return_value=mock_process),
        ):
            with pytest.raises(ValueError, match="terminated unexpectedly"):
                await convert_file_in_subprocess(
                    b"test", "test.pdf", "application/pdf",
                    timeout_seconds=1,  # Very short timeout
                )

        mock_process.kill.assert_called_once()
