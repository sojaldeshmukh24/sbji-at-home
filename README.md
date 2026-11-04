# SBJI@Home

## Django environment configuration

The project reads deployment settings from process environment variables; it
does not load a `.env` file. Debug mode is disabled unless explicitly enabled.
For local development, set `DJANGO_DEBUG=true` before starting Django. When no
`DJANGO_SECRET_KEY` is supplied in debug mode, a fresh temporary signing key is
generated for that process; existing development sessions will not survive a
server restart. Local hosts default to `localhost`, `127.0.0.1`, and `[::1]`.

Deployments must provide:

- `DJANGO_DEBUG=false`
- `DJANGO_SECRET_KEY`: a newly generated, private Django signing key.
- `DJANGO_ALLOWED_HOSTS`: comma-separated hostnames serving the application.
- `DJANGO_DEFAULT_FROM_EMAIL`: the verified sender address configured for the
  selected email service.
- `DJANGO_EMAIL_HOST`: SMTP host. Production defaults to Django's built-in
  SMTP backend and fails startup if the SMTP host or default sender is not
  configured. Optional SMTP variables are `DJANGO_EMAIL_PORT` (defaults to 587
  with TLS or 465 with SSL), `DJANGO_EMAIL_HOST_USER`,
  `DJANGO_EMAIL_HOST_PASSWORD`, `DJANGO_EMAIL_USE_TLS` (defaults to true in
  production), `DJANGO_EMAIL_USE_SSL` (defaults to false), and
  `DJANGO_EMAIL_TIMEOUT` (defaults to 10 seconds). `DJANGO_EMAIL_USER` and
  `DJANGO_EMAIL_PASSWORD` are accepted as legacy aliases; use the `HOST_USER`
  and `HOST_PASSWORD` names for new deployments.

The previous source-embedded signing key must not be reused. If it was ever
used by an internet-accessible deployment, replace it in the deployment's
secret store and invalidate sessions signed with it. Never put these values in
source control, templates, command history, or logs.

### HTTPS and trusted proxy configuration

Production session and CSRF cookies are secure-only whenever `DJANGO_DEBUG` is
false. Configure TLS at the application server or at a trusted ingress before
serving public traffic. The following settings are opt-in because the project
does not identify a TLS terminator or proxy:

- `DJANGO_SECURE_SSL_REDIRECT=true` makes Django redirect HTTP requests. Enable
  it only if Django can correctly determine whether requests are HTTPS.
- `DJANGO_SECURE_HSTS_SECONDS` sets the HSTS duration in seconds. Start with a
  short duration while validating HTTPS. Increase it only after confirming the
  entire hostname is HTTPS-only; use `DJANGO_SECURE_HSTS_INCLUDE_SUBDOMAINS`
  or `DJANGO_SECURE_HSTS_PRELOAD` only after separately verifying every
  affected subdomain and preload requirements.
- `DJANGO_TRUST_X_FORWARDED_PROTO=true` makes Django trust
  `X-Forwarded-Proto: https`. Use it only behind a trusted proxy that removes
  client-supplied copies and writes the header itself. Never enable it when
  clients can reach Django directly or spoof that header.

Do not enable HSTS or redirect settings until the chosen host's TLS and proxy
behavior has been tested. `manage.py check --deploy` should be run with the
intended production settings before release.

### Static assets and uploaded media

`collectstatic` writes application and Django admin assets to `STATIC_ROOT`,
which defaults to `staticfiles/` and can be changed with `DJANGO_STATIC_ROOT`.
Run it for each release, then configure the selected web server or static
storage to serve that directory at `STATIC_URL`. Django's development static
serving is not enabled when `DEBUG` is false.

Product uploads are stored below `MEDIA_ROOT` (default `media/`); set
`DJANGO_MEDIA_ROOT` to the selected persistent media mount if applicable.
Uploaded media is not part of `collectstatic`. The default local directory is
not guaranteed to survive an ephemeral deployment or be shared across multiple
application instances. Select durable media storage and include it in backups
before accepting production uploads. Do not delete or relocate existing media
during release preparation.

### Database, release, backup, and recovery

The current database configuration is a local SQLite file at `db.sqlite3`.
There is no production database engine/credential configuration or database
driver in this project. Do not deploy this setup to ephemeral storage or
multiple application instances. Select and test a persistent production
database, secure credentials, and an appropriate supported Django database
driver before release; the database choice is intentionally not guessed here.

For a release, back up the database and uploaded media using the chosen
storage/database provider's consistent backup mechanism, deploy the reviewed
code, run `manage.py migrate` against the intended database, and run
`manage.py collectstatic --noinput` to the configured static destination.
Smoke-test login, product browsing, checkout, order status, and staff access
before reopening traffic. Preserve the prior code and backups until the release
is accepted. Define and test a rollback procedure that restores a compatible
code version and, if necessary, the matching database and media backups. No
automated production backup, restore, deployment, or rollback is configured by
this repository.

### Monitoring and operational checks

No hosting platform, health endpoint, monitoring service, alert routing, or
production log destination is configured here. Before launch, choose how to
check application availability and database connectivity, capture application
errors without recording credentials/payment secrets, alert an operator, and
verify recovery from backups. Keep Django `DEBUG` disabled so unhandled errors
do not expose debug tracebacks.

## Razorpay test-mode configuration

Online checkout uses Razorpay **test mode only**. Cash on Delivery remains
available when test credentials are not configured. The application rejects
any Razorpay key ID that does not begin with `rzp_test_`; do not provide live
credentials.

Set these environment variables in the process environment that runs Django:

- `RAZORPAY_KEY_ID`: Razorpay test key ID (`rzp_test_...`).
- `RAZORPAY_KEY_SECRET`: matching Razorpay test secret.
- `RAZORPAY_WEBHOOK_SECRET`: webhook secret configured for the Test Mode
  webhook endpoint.

PowerShell example (values are placeholders; obtain test credentials from the
Razorpay Dashboard's Test Mode):

```powershell
$env:RAZORPAY_KEY_ID = "rzp_test_your_test_key_id"
$env:RAZORPAY_KEY_SECRET = "your_test_key_secret"
$env:RAZORPAY_WEBHOOK_SECRET = "your_test_webhook_secret"
.\.venv\Scripts\python.exe manage.py runserver
```

Keep the secret only in a protected local/deployment environment variable store.
Never commit actual credentials, place the secret in templates, or expose it to
browser code. The browser receives only the public test key ID and the server-
created Razorpay order details.

In the Razorpay Dashboard's **Test Mode**, configure a webhook pointing to
`https://your-domain/payments/razorpay/webhook/`, set its secret to the value
provided through `RAZORPAY_WEBHOOK_SECRET`, and enable only
`payment.captured` and `payment.failed`. The endpoint verifies Razorpay's
signature against the unmodified request body, then reconciles a payment only
when its Razorpay order ID, amount, currency, and state match a pending online
marketplace order. Duplicate or contradictory events are harmless, and stock
continues to use the existing reservation/payment transitions.

Development must use Razorpay Test Mode credentials only. Never use production
or live credentials during development; live payments are not supported.
The current integration explicitly accepts test-mode keys only. Production
Razorpay/live-payment use is therefore not ready or enabled; do not substitute
live credentials. Verify test payments, payment-failure handling, webhook
signature verification, and duplicate callbacks in the provider's sandbox
before a release that offers test-mode online checkout.

Pending online orders reserve stock for 15 minutes. Schedule this command to
run periodically (for example, once per minute) so expired test payments are
checked with Razorpay and their reservations are released:

```powershell
.\.venv\Scripts\python.exe manage.py expire_online_payments
```

The command reconciles captured payments before releasing stock. If Razorpay
cannot be reached, it reports an error and leaves the reservation unchanged for
a later retry.

## Order fulfillment

Products without an assigned vendor are fulfilled by the Sbji@Home platform.
Each order has one platform fulfillment for its unassigned products and one
fulfillment per assigned vendor, including mixed orders. Staff manage platform
fulfillments from the Django admin's Vendor orders actions; vendor accounts can
only manage their own fulfillments. The customer order status reflects the
least-progressed non-rejected fulfillment and becomes Delivered only after all
remaining fulfillments are delivered. When all fulfillments reject, the order
is Rejected; when some reject and the rest are delivered, it is Partially
fulfilled. A full rejection sends the order-level rejection notice, while
individual item-unavailability notices are reserved for partial outcomes.
