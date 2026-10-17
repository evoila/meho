# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Privacy-bounded request usage events (#3734).

Routine REST and MCP activity is usage, not an accountable action. This
package records it outside ``audit_log``:

* :mod:`.schema` -- what an event may hold (opaque ids, transport,
  enumerated route, duration, bounded outcome) and nothing else.
* :mod:`.collector` -- one ``request_finished`` event per request
  boundary, plus optional ``context_selected`` events.
* :mod:`.reader` -- tenant-authorized aggregate reads.
* :mod:`.retention` -- the daily prune job.

Collection is off unless ``REQUEST_USAGE_RETENTION_DAYS`` is set. During
the dual-write phase the wrapper ``audit_log`` rows are still written.
"""
