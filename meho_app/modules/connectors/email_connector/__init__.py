# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (c) 2026 evoila Group
"""
Email Connector Module.

Provides email sending capability via a pluggable provider abstraction.
Supports SMTP, SendGrid, Mailgun, Amazon SES, and Generic HTTP providers.

The Python package is named ``email_connector`` to avoid colliding with the
Python standard library ``email`` package. The database-stored connector type
remains ``"email"`` and must not be renamed.

The ``EmailConnector`` class is intentionally NOT re-exported at package
level. Importing it eagerly here would force every consumer of the
package (including Alembic metadata discovery) to load aiosmtplib, the
SES provider, and the rest of the heavyweight provider tree. Use the
fully qualified path instead:

    from meho_app.modules.connectors.email_connector.connector import EmailConnector
    from meho_app.modules.connectors.email_connector.operations import EMAIL_OPERATIONS
"""
