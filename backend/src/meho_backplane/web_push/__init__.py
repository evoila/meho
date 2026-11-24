# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 evoila Group

"""Push subscriptions: "Notify me about approvals" per browser.

A person switches notifications on for one browser on the account page.
The browser sends its push subscription (the push service address and two
keys), and MEHO stores it per person and browser. When a request waits for
approval, MEHO sends a message to the browsers of the people who may approve
it, and a second one when it is decided.

* :mod:`.keys` -- the admin setting and the push key (VAPID).
* :mod:`.endpoints` -- the checks on a subscription: the push service must
  be one of the browser makers' services.
* :mod:`.models` -- the ``push_subscription`` table.
* :mod:`.store` -- save, list and delete subscriptions.
* :mod:`.recipients` -- who may approve a request, so who gets a message.
* :mod:`.encryption` -- the message encryption and the signed token.
* :mod:`.send` -- send the messages, in the background, with a rate limit.

The console routes are in :mod:`meho_backplane.ui.routes.account_push`.
With ``WEB_PUSH_ENABLED`` off nothing here runs. See
``docs/codebase/ui.md``, "Approval notifications", and
``docs/codebase/approvals.md``, "Notifications".
"""
