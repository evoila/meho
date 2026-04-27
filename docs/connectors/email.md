# Email

> Last verified: v2.0

The Email connector enables MEHO to send branded HTML notifications to your team. When MEHO completes an investigation, diagnoses an issue, or needs to escalate a finding, it can compose and send a professional email summary with a link back to the investigation session. Supports multiple email providers: SMTP, SendGrid, Mailgun, Amazon SES, and Generic HTTP.

## Authentication

Authentication is provider-specific:

| Provider | Credential Fields | Notes |
|----------|------------------|-------|
| SMTP | `smtp_username`, `smtp_password` | Standard SMTP credentials. Host, port, and TLS configured separately. |
| SendGrid | `api_key` | SendGrid API key with Mail Send permission |
| Mailgun | `api_key`, `mailgun_domain` | Mailgun API key + sending domain |
| Amazon SES | `ses_access_key`, `ses_secret_key`, `ses_region` | AWS IAM credentials with SES send permission. Default region: `us-east-1` |
| Generic HTTP | `endpoint_url`, `auth_header`, `payload_template` | Custom HTTP endpoint for any email service with a REST API |

**Setup:**

1. Choose your email provider and gather the required credentials.
2. Configure the **From Email** address (the sender address for all emails from this connector).
3. Set **Default Recipients** -- a comma-separated list of email addresses that will receive notifications by default.
4. Optionally set a **From Name** (defaults to "MEHO").
5. MEHO sends a test email during connector creation to verify end-to-end delivery.

!!! tip "Provider Selection"
    - **SMTP** works with any email server (Office 365, Gmail, corporate SMTP).
    - **SendGrid** and **Mailgun** are purpose-built for transactional email with delivery tracking.
    - **Amazon SES** is the lowest-cost option for AWS environments.
    - **Generic HTTP** is the escape hatch for any email service with a REST API.

## Operations

MEHO registers 2 operations for Email (1 READ, 1 WRITE):

| Operation | Trust Level | Description |
|-----------|-------------|-------------|
| `send_email` | WRITE | Send a branded HTML email to the configured default recipients. Provide subject and body in markdown. The connector renders branded HTML with MEHO styling and a session link. |
| `check_status` | READ | Check the delivery status of a previously sent email. Returns status (sent/accepted/failed), timestamp, and error details. This is a DB lookup, not a provider API call. |

!!! info "Branded HTML Output"
    The `send_email` operation accepts markdown input and renders it as a professional branded HTML email. The email includes MEHO styling and a link to the investigation session so recipients can view the full context.

## Example Queries

Ask MEHO questions like:

- "Send an email summary of today's incidents to the ops team"
- "Email the on-call engineer about the disk alert"
- "Notify the team about the high CPU issue on prod-web-01"
- "Send a summary of this investigation to the platform team"
- "Email a post-mortem summary with the root cause findings"
- "Check the delivery status of the last email I sent"
- "Send an alert email about the failing health checks"

## Topology

Email does not contribute topology entities. It functions as a notification connector -- operators use it to communicate investigation findings and alerts to team members and stakeholders.

## Delivery History

MEHO logs every email sent through the connector, tracking:

- **From/To addresses** and subject line
- **Provider type** and provider-assigned message ID
- **Delivery status**: `sent`, `accepted`, or `failed`
- **Error details** if delivery failed
- **Timestamp** of the delivery attempt

The delivery history is accessible via the MEHO API and can be queried to audit email notifications.

## Troubleshooting

### SMTP Authentication Failed

**Symptom:** Connector creation fails with authentication error when using SMTP.
**Cause:** Incorrect SMTP credentials, or the SMTP server requires app-specific passwords (e.g., Gmail with 2FA).
**Fix:** For Gmail, generate an app-specific password in your Google account security settings. For Office 365, ensure SMTP AUTH is enabled for your account. Verify the SMTP host (e.g., `smtp.gmail.com`), port (typically `587` for TLS), and credentials.

### SendGrid API Key Permissions

**Symptom:** `send_email` fails with 403 Forbidden when using SendGrid.
**Cause:** The SendGrid API key does not have the "Mail Send" permission.
**Fix:** In SendGrid, navigate to **Settings > API Keys** and create a key with at minimum the "Mail Send" permission.

### Email Formatting Issues

**Symptom:** The email body appears as raw markdown instead of formatted HTML.
**Cause:** This should not happen -- the connector renders markdown to HTML automatically.
**Fix:** If you see raw markdown in emails, report it as a bug. The connector uses a built-in template engine to convert markdown to branded HTML.

### Test Email Not Received

**Symptom:** Connector creation succeeds but the test email never arrives.
**Cause:** The email may be caught by spam filters, or the from address may not be authorized by the email provider.
**Fix:** Check your spam/junk folder. Verify that the from address domain has SPF, DKIM, and DMARC records configured for your email provider. For SendGrid/Mailgun, verify the sender domain in their dashboard.

### Amazon SES Sandbox Mode

**Symptom:** SES emails only deliver to verified email addresses.
**Cause:** New SES accounts start in sandbox mode, which restricts sending to verified addresses only.
**Fix:** In the AWS Console, navigate to SES and either verify recipient addresses (for testing) or request production access to send to any address.
