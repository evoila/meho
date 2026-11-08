# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Keep knowledge and memory useful, and measure whether lessons help.

* :mod:`~meho_backplane.lessons.retrievals` records which entries a search
  gave to an agent session, and finds the ones the session then used.
* :mod:`~meho_backplane.lessons.metrics` computes the four learning numbers
  of ``meho audit reflex``.
* :mod:`~meho_backplane.lessons.upkeep` is the weekly job: it merges
  duplicate knowledge entries, retires expired ones, sets ``confirmed_at``
  again on used confirmed entries, and lists used candidates for a
  reviewer.

See ``docs/codebase/reflex-adoption.md`` and ``docs/codebase/kb.md``.
"""
