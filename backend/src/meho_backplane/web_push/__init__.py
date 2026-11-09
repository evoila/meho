# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Push subscriptions: "Notify me about approvals" per browser.

A person switches notifications on for one browser on the account page.
The browser sends its push subscription (the push service address and two
keys), and MEHO stores it per person and browser. Sending the messages is
not part of this package yet.

* :mod:`.keys` -- the admin setting and the push key (VAPID).
* :mod:`.endpoints` -- the checks on a subscription: the push service must
  be one of the browser makers' services.
* :mod:`.models` -- the ``push_subscription`` table.
* :mod:`.store` -- save, list and delete subscriptions.

The console routes are in :mod:`meho_backplane.ui.routes.account_push`.
With ``WEB_PUSH_ENABLED`` off nothing here runs. See
``docs/codebase/ui.md``, "Approval notifications".
"""
