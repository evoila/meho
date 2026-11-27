# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group
"""Replace the image proxy's name with ``PROXY`` in test report files.

GitHub hides a secret in the job log only. A test report that CI uploads
(a JUnit XML file, a pytest log) can hold the full name of a test image.
That name starts with the proxy's ``<host>/<project>`` (the repository
secret ``CI_IMAGE_PROXY``). So each test job runs this script on its
reports before it uploads them.

Usage::

    CI_IMAGE_PROXY=<host>/<project> python3 -I hide_proxy_name.py FILE...

The script replaces the whole value and its host part (with and without
its port), also in URL-encoded form (``/`` as ``%2F``, ``:`` as ``%3A``,
upper or lower case hex). It reads the value as text, never as a pattern.
A file that does not exist is skipped. With an empty ``CI_IMAGE_PROXY``
the script changes nothing. It prints no value and no file content.

Standard library only, so it runs with ``python3 -I`` before or without
the project's environment.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from urllib.parse import quote

REPLACEMENT = b"PROXY"


def _encoded(text: str) -> set[str]:
    forms = {text}
    for safe in ("", "/", ":"):
        upper = quote(text, safe=safe)
        forms.add(upper)
        forms.add(_lower_hex(upper))
    return forms


def _lower_hex(text: str) -> str:
    return re.sub(r"%[0-9A-F]{2}", lambda m: m.group(0).lower(), text)


def forms_of(proxy: str) -> list[bytes]:
    """Every text to replace, longest first, so the whole value goes before its host."""
    host = proxy.split("/", 1)[0]
    # A DNS or connect error names the host without its port.
    hosts = {host, host.rsplit(":", 1)[0]}
    forms = _encoded(proxy)
    for name in hosts:
        if name:
            forms |= _encoded(name)
    return sorted((f.encode() for f in forms if f), key=len, reverse=True)


def hide(data: bytes, forms: list[bytes]) -> bytes:
    for form in forms:
        data = data.replace(form, REPLACEMENT)
    return data


def main(argv: list[str]) -> int:
    proxy = os.environ.get("CI_IMAGE_PROXY", "").strip()
    if not proxy:
        print("CI_IMAGE_PROXY is empty: the reports stay as they are.")
        return 0
    forms = forms_of(proxy)
    changed = 0
    for name in argv:
        path = Path(name)
        if not path.is_file():
            continue
        data = path.read_bytes()
        new = hide(data, forms)
        if new != data:
            path.write_bytes(new)
            changed += 1
    print(f"Checked {len(argv)} report path(s); changed {changed} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
