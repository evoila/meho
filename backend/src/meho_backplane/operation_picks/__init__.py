# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Records of operation searches and the calls that follow them (#4750).

The question this package answers from real sessions: after a search, did
the agent call an operation from the results, and did it run?

* :mod:`.recorder` -- writes one record per ``search_operations`` call and
  one per ``call_operation`` call, and links a call to the search it
  followed.
* :mod:`.report` -- the per-connector, per-day numbers.
* :mod:`.retention` -- deletes old records; the request usage retention
  job calls it.

Nothing is recorded unless ``REQUEST_USAGE_RETENTION_DAYS`` is set. The
model sees nothing new: no tool, argument or result changes.
"""
