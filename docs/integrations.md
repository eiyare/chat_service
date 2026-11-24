# Connecting an app to saaschat

Any app or website (Moodle, Odoo, WordPress, a custom system) can connect to a saaschat
workspace. A connected app can:

1. **Sign in its users**: the assistant greets them by name and keeps their conversations
   across devices; with a *scope*, it answers them from that scope's content only.
2. **Send content**: into named *scopes* (a course, a client account, a product line), or into
   the *shared* scope, which adds to the workspace's knowledge for everyone.
3. **Offer tools**: actions the assistant can take on the app for a user, such as looking up their
   orders or invoices. saaschat calls the app back, signed, and the app answers as that user.
4. **Copy the workspace's passages** (optional), e.g. to keep a local copy.

The Moodle plugin (`integrations/moodle/local_saaschat`) is a complete example. This guide uses an
Odoo website as the running example.

## 1. Connect the app in saaschat

In the workspace: **Admin → Integrations → Connect an app**, type *App or website*. Enter the app's
address (e.g. `https://www.ei.consulting`) and tick what it may do. Its page shows:

- the **key** (public, e.g. `Xr4…`), used in the API address and as the token issuer;
- the **secret** (keep it private), used to call saaschat and to sign users;
- the **API address**: `https://<saaschat>/i/<key>/`.

Give the app the key and secret. "Generate a new secret" replaces it (the old one stops working).

## 2. Show the chat and sign in users

Add the widget to the site's pages (Admin → Embed gives the snippet), and for a signed-in user add
`data-user` with a token:

```html
<script src="https://<saaschat>/static/widget/embed.js" data-key="<widget key>"
        data-user="<token>" async></script>
```

The token is `base64url(JSON payload)` + `"."` + hex `HMAC-SHA256(secret, base64url part)`, with
the payload:

| Field   | Required | Meaning |
|---------|----------|---------|
| `iss`   | yes | the integration's **key** |
| `sub`   | yes | the user's ID in the app: letters, digits, `. _ @ -`, up to 64 |
| `name`  | no  | their name, for the greeting (`{name}`) |
| `exp`   | yes | expiry, unix seconds (e.g. now + 12 hours) |
| `scope` | no  | answer from this scope's content only, e.g. `client:acme` (needs "Content from the app") |
| `page`  | no  | the item key the user is viewing in that scope, e.g. `page:pricing` |

Python (e.g. an Odoo controller or QWeb helper):

```python
import base64, hashlib, hmac, json, time

def saaschat_token(key, secret, user_id, name, scope=None, page=None, hours=12):
    payload = {"iss": key, "sub": str(user_id), "name": name, "exp": int(time.time()) + hours * 3600}
    if scope:
        payload["scope"] = scope
    if page:
        payload["page"] = page
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return body + "." + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
```

PHP: `$body = rtrim(strtr(base64_encode(json_encode($payload)), '+/', '-_'), '=');`
`$token = $body . '.' . hash_hmac('sha256', $body, $secret);`

Only sign users server-side, and only for the user who is logged in. Visitors without a token are
guests. Each user (and each scope) keeps their own conversations.

## 3. Send content

Every API call sends `Authorization: Bearer <secret>`. Scopes are names of letters, digits and
`: _ . -` (up to 100); use `shared` for content everyone may be answered from.

```bash
API=https://<saaschat>/i/<key>
AUTH="Authorization: Bearer <secret>"

# Who am I, and what am I allowed to do?
curl -H "$AUTH" $API/

# Add or replace an item (JSON text: Markdown or plain text)...
curl -H "$AUTH" -H "Content-Type: application/json" -X POST $API/scopes/shared/items/ \
     -d '{"key": "page:services", "version": "2026-11-24", "name": "Our services", "text": "# Services\n..."}'

# ...or a file (the workspace's accepted types, e.g. pdf, docx, md)
curl -H "$AUTH" -F key=file:brochure -F version=3 -F name="Brochure" -F file=@brochure.pdf \
     $API/scopes/client:acme/items/

curl -H "$AUTH" $API/scopes/                       # scopes and item counts
curl -H "$AUTH" $API/scopes/shared/items/          # {"items": {key: version}, "extensions": [...], ...}
curl -H "$AUTH" -X POST $API/scopes/shared/items/delete/ -d '{"keys": ["page:old"]}'
curl -H "$AUTH" -X POST $API/scopes/client:acme/delete/   # everything in a scope
```

Items are processed in the background and searchable within seconds to minutes. Sync by comparing
`GET .../items/` versions with yours: send only changed items and delete removed ones. Item keys
are letters, digits and `: _ . @ / -` (up to 100). An item named with key `overview` gives the
scope its title (shown to the assistant as where the user is).

## 4. Offer tools

Describe the tools once (and whenever they change):

```bash
curl -H "$AUTH" -H "Content-Type: application/json" -X PUT $API/manifest/ -d '{
  "callback": "/saaschat/tools",
  "tools": [
    {"name": "get_my_orders", "kind": "read", "needs_user": true,
     "description": "The signed-in customer'"'"'s recent orders: number, date, status, total and tracking.",
     "parameters": {"type": "object", "properties": {
        "limit": {"type": "integer", "description": "How many, newest first (default 5)"}}}},
    {"name": "cancel_order", "kind": "action", "needs_user": true,
     "description": "Cancel one of the customer'"'"'s orders that has not shipped.",
     "parameters": {"type": "object", "properties": {"order": {"type": "string"}}, "required": ["order"]}},
    {"name": "opening_hours", "kind": "read", "needs_user": false,
     "description": "Opening hours and public holidays."}
  ]}'
```

- `name`: lower_case_with_underscores, unique; `search_content` and `search_course` are reserved.
- `description`: what it does and when to use it. The model chooses tools from this, so be specific.
- `parameters`: a JSON Schema object for the arguments.
- `kind`: `read` (looks something up) or `action` (changes something). For actions the assistant
  is told to say exactly what it will do and wait for the user to agree before calling.
- `needs_user`: `true` (default) offers the tool only to users this app signed in; `false` offers
  it to every visitor of the workspace's widget.

Then tick **Tools** on the integration. When the model uses a tool, saaschat POSTs to
`<app address><callback>`:

```json
{"tool": "get_my_orders", "arguments": {"limit": 3},
 "user_token": "<the token you issued for this user>", "scope": "client:acme", "page": null}
```

with headers `X-Saaschat-Integration: <key>`, `X-Saaschat-Timestamp: <unix seconds>` and
`X-Saaschat-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`.

The app must:

1. check the signature and that the timestamp is recent (e.g. within 5 minutes);
2. for user tools, check `user_token` (your own signature and expiry) and act **as that user only**;
3. answer within 15 seconds with JSON. Whatever you return goes to the model; return
   `{"error": "..."}` (any status) for a failure the user should hear about.

```python
import hashlib, hmac, json, time

def verify_saaschat_call(secret, headers, raw_body):
    stamp = headers.get("X-Saaschat-Timestamp", "")
    sent = headers.get("X-Saaschat-Signature", "").removeprefix("sha256=")
    expected = hmac.new(secret.encode(), stamp.encode() + b"." + raw_body, hashlib.sha256).hexdigest()
    return stamp.isdigit() and abs(time.time() - int(stamp)) < 300 and hmac.compare_digest(expected, sent)
```

Keep answers small and factual (saaschat passes up to 12,000 characters to the model). In the
workspace's Brain map each call shows as a step with the tool, its arguments and a summary
("3 orders"), never the data itself.

## 5. Copy the workspace's passages (optional)

With **Let the app copy passages** ticked:

```bash
curl -H "$AUTH" $API/documents/            # {"documents": [{"id", "name", "version"}]}
curl -H "$AUTH" $API/documents/12/chunks/  # {"id", "name", "chunks": [{"index", "section", "text"}]}
```

## Errors

`403` wrong secret · `404` unknown or disabled integration · `409` the capability is turned off
in saaschat (the message says which) · `400` invalid input (the message says what) · `413` file
too large.

## Example: an Odoo module

A `saaschat` Odoo module would:

- **Settings**: saaschat address, widget key, integration key and secret.
- **Widget**: inherit `website.layout` to add the script, with `data-user` for logged-in portal
  users (`sub` = partner ID, `name` = partner name).
- **Content** (scheduled action): send published website pages and product descriptions to the
  `shared` scope, keyed `page:<id>` / `product:<id>` with `write_date` as the version; delete the
  ones unpublished since.
- **Tools** (`/saaschat/tools` controller, `auth="public"`, CSRF off, signature checked):
  `get_my_orders` (`sale.order` for the token's partner), `get_my_invoices` (`account.move`),
  `create_support_ticket` (action, e.g. `helpdesk.ticket` or a `mail.activity`).
- **Manifest**: sent on install and when settings are saved.

The Moodle plugin maps the same pieces: students are signed users, each course is a scope
(`course:<id>`), and the student data tools are its tools.
