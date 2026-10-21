# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Secret shapes ``vrops.api_file.get`` refuses before it returns a file.

The stored-result redaction (:mod:`meho_backplane.redaction`) finds a
labelled secret only when the label sits directly before ``=`` or ``:``
(``password=...``, ``token: ...``). A JSON file writes the label in
quotes (``"password": "..."``), and a PEM private key has no label at
all, so neither reaches the redaction rules. The read must never return
a file with a secret in clear text, so it checks these shapes itself, on
the whole file text, and refuses the file when one is found.

The check names the shape and where it starts; it never repeats the
matched text.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["SECRET_SHAPES", "find_secret_shape"]

#: One secret value: anything up to whitespace or a quote, at least 8
#: characters, the same bound the redaction rules use. The minimum keeps
#: a schema stub such as ``"password": "string"`` out.
_VALUE: Final[str] = r"[^\s'\"]{8,4096}"

#: The labels the redaction rule ``api_key`` knows, here allowed in quotes.
_LABEL: Final[str] = (
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|session[_-]?token"
    r"|secret(?:[_-]?(?:key|id))?|private[_-]?key|password|passwd|pwd|client[_-]?secret|token)"
)

#: ``"password": "value"`` and ``'token': 'value'``: a quoted label, ``:``
#: or ``=``, and a value of at least 8 characters. The label may carry a
#: prefix inside the quotes (``"userPassword"``, ``"x-auth-token"``). A
#: JSON schema entry such as ``"password": {"type": "string"}`` does not
#: match, because the run after the colon is one ``{`` and then a quote.
_QUOTED_LABELLED_SECRET: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w-])['\"][\w.-]*" + _LABEL + r"['\"]\s*[=:]\s*['\"]?" + _VALUE,
    re.IGNORECASE,
)

#: ``"Authorization": "Basic ..."``: the header as a quoted JSON field.
_QUOTED_AUTHORIZATION: Final[re.Pattern[str]] = re.compile(
    r"['\"]authorization['\"]\s*[=:]\s*['\"]?[A-Za-z]+\s+[^\s'\"]{1,4096}",
    re.IGNORECASE,
)

#: PEM private keys of every kind (``PRIVATE KEY``, ``RSA PRIVATE KEY``,
#: ``EC PRIVATE KEY``, ``ENCRYPTED PRIVATE KEY``, ``OPENSSH PRIVATE KEY``).
_PEM_PRIVATE_KEY: Final[re.Pattern[str]] = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY", re.IGNORECASE
)

#: Shape name, as the error message says it, to its pattern.
SECRET_SHAPES: Final[dict[str, re.Pattern[str]]] = {
    "a quoted password or token field": _QUOTED_LABELLED_SECRET,
    "a quoted Authorization field": _QUOTED_AUTHORIZATION,
    "a PEM private key": _PEM_PRIVATE_KEY,
}


def find_secret_shape(text: str) -> str | None:
    """Return the first secret shape in *text* and where it starts, or ``None``.

    The answer is one phrase, for example ``a PEM private key at
    character 1024``. The matched text itself is never returned.
    """
    for name, pattern in SECRET_SHAPES.items():
        match = pattern.search(text)
        if match is not None:
            return f"{name} at character {match.start()}"
    return None
