# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Drop a cached login token only when it is the token a failed call sent.

Login tokens are cached per credential (``credential_cache_key``), so two
targets that use one secret against one login host share one token
(evoila/meho#5033). When a call gets a ``401``, the connector drops the
cached token and the next call logs in again. With a shared token, calls on
both targets run at the same time. So a ``401`` can arrive late: another
call has already logged in again and cached a new token. Dropping that new
token would start one more login, and on a vendor that allows one session
per user, that login ends the token the other calls are using right now.

:func:`drop_rejected_token` compares first. It drops the cached token only
when the failed request carried it. When a newer token is cached, it keeps
it, and the retry uses it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, MutableMapping

import httpx

__all__ = ["drop_rejected_token"]


def drop_rejected_token[K, V](
    cache: MutableMapping[K, V],
    key: K,
    rejected: httpx.Request | None,
    headers_for: Callable[[V], Mapping[str, str]],
) -> bool:
    """Drop ``cache[key]`` when *rejected* sent it. Return ``True`` when it was dropped.

    *headers_for* turns a cached value into the auth headers the connector
    sends with it. The cached token was sent when *rejected* carries every
    one of those headers with the same value.

    *rejected* ``None`` means the caller has no failed request to compare
    (for example a caller that asks to drop the token for another reason),
    so the cached token is dropped.

    The caller must hold the lock that guards *cache*.
    """
    cached = cache.get(key)
    if cached is None:
        return False
    if rejected is not None:
        sent = headers_for(cached)
        if any(rejected.headers.get(name) != value for name, value in sent.items()):
            return False
    del cache[key]
    return True
