# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Shared XSS-breakout payload and checker for console ``x-data`` sinks.

First written for the Broadcast ``op_id`` filter (review finding on PR
#1044). That filter is gone (#3762); the runbook editor tests still use
the payload and the parser-grounded check.
"""

from __future__ import annotations

from html.parser import HTMLParser

__all__ = ["_XSS_OP_ID", "_assert_no_xss_breakout"]


#: A crafted ``op_id`` query value that tries to break out of an
#: ``x-data`` attribute and inject live event handlers. It carries every
#: HTML metacharacter the hardening must neutralise: a double-quote (the
#: byte ``| tojson`` does NOT escape, which terminates a double-quoted
#: attribute early), a single-quote, ``<`` / ``>`` / ``&``, and a literal
#: ``</script>`` (which would close a data-island script element). The
#: ``autofocus onfocus=...`` payload is what would fire if the value
#: escaped the attribute and grafted itself onto the host element.
_XSS_OP_ID = (
    "\" autofocus onfocus=alert(document.cookie) x='1' <b>& </script><img src=x onerror=alert(2)>"
)


class _XDataAttrCollector(HTMLParser):
    """Collect the parsed attribute list of every element carrying ``x-data``.

    The browser's HTML parser is the ground truth for "did the attribute
    break out": if the injected ``op_id`` terminated ``x-data`` early, the
    parser sees ``autofocus`` / ``onfocus`` as *separate attributes* on the
    host element rather than as bytes inside the single ``x-data`` value.
    We parse the response the same way and assert no such stray attribute
    appears. Parser-grounded so the test holds for either safe rendering
    (single-quoted ``x-data`` or a data-island refactor).
    """

    def __init__(self) -> None:
        super().__init__()
        # One entry per element that has an ``x-data`` attribute: the full
        # list of (name, value) attribute pairs the parser saw on it.
        self.x_data_elements: list[list[tuple[str, str | None]]] = []
        # Every attribute NAME the parser saw across the whole document
        # (lower-cased), to catch a handler injected onto ANY element.
        self.all_attr_names: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name.lower() for name, _ in attrs}
        self.all_attr_names |= names
        if "x-data" in names:
            self.x_data_elements.append(attrs)


def _assert_no_xss_breakout(body: str) -> None:
    """Assert the rendered HTML did not let ``op_id`` break out of ``x-data``.

    Fails on the pre-fix ``{{ op_id | tojson }}`` inside a *double-quoted*
    ``x-data="..."`` (the double-quote bytes ``tojson`` leaves raw
    terminate the attribute, so the parser grafts ``autofocus`` / ``onfocus``
    onto the element); passes once the attribute is single-quoted (or the
    config moves to a data island).
    """
    parser = _XDataAttrCollector()
    parser.feed(body)

    # No event handler / autofocus leaked onto ANY element as a parsed
    # attribute. If the attribute broke out, the parser would surface these.
    assert "onfocus" not in parser.all_attr_names, (
        "op_id broke out of x-data: 'onfocus' parsed as a live attribute"
    )
    assert "autofocus" not in parser.all_attr_names, (
        "op_id broke out of x-data: 'autofocus' parsed as a live attribute"
    )
    assert "onerror" not in parser.all_attr_names, (
        "op_id broke out: 'onerror' parsed as a live attribute"
    )

    # The op_id payload must live ENTIRELY inside an x-data attribute value
    # -- never as a stray attribute and never as injected element markup.
    # At least one x-data element must carry the marker substring inside its
    # x-data value (proving it stayed contained), and no x-data element may
    # carry it as a separate attribute.
    contained_somewhere = False
    for attrs in parser.x_data_elements:
        for name, value in attrs:
            if name.lower() == "x-data" and value and "onfocus=alert" in value:
                contained_somewhere = True
            else:
                # The marker must not appear in any OTHER attribute on an
                # x-data element (that would mean it escaped the value).
                assert value is None or "onfocus=alert" not in value, (
                    f"op_id leaked into a non-x-data attribute {name!r}"
                )
    assert contained_somewhere, (
        "expected the op_id payload to stay contained inside an x-data value"
    )

    # No premature script-element close from the </script> in the payload
    # (guards the data-island alternative too). The only </script> tags in
    # the document must pair with a <script ...> the templates author.
    assert body.count("</script>") == body.count("<script"), (
        "a </script> in op_id closed a script element early"
    )
