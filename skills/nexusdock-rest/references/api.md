# NexusDock REST API reference for the Skill

This reference records the API behavior the Skill relies on. The server's published OpenAPI contract remains authoritative.

## Authentication

Integration routes accept a Workspace PAT or a NexusDock OAuth access token:

```http
Authorization: Bearer <Workspace PAT or OAuth access token>
```

The credential determines the Workspace/Tenant. Never send a Tenant selector in request bodies or query parameters.

OAuth tokens for this REST API are bound to:

```text
resource = https://mcp.nexusdock.co/v1/integrations
scope    = integrations
```

They are not interchangeable with MCP OAuth tokens. The Skill uses RFC 8628 Device Authorization Grant by default:

```http
POST /oauth/register
POST /oauth/device_authorization
POST /oauth/token
POST /oauth/revoke
```

A pure Device Flow client registers `urn:ietf:params:oauth:grant-type:device_code` plus `refresh_token` and does not need any redirect URI. The Device Authorization response contains a secret `device_code` for the client and a short `user_code` for the human. Only the latter and `verification_uri[_complete]` may be displayed.

In production, the verification page is:

```text
https://mcp.nexusdock.co/activate
```

Token polling can return `authorization_pending`, `slow_down`, `access_denied`, or `expired_token`. Respect the returned polling interval. Access tokens are refreshed with refresh-token rotation; a reused old refresh token can revoke the grant, so callers must serialize refreshes.

## Discovery

### Devices

```http
GET /v1/integrations/devices
```

Use only devices where `online=true` for execution.

### Capabilities

```http
GET /v1/integrations/capabilities?node_id=<id>&type=tool&q=<text>
GET /v1/integrations/capabilities/{id}
```

For an executable Tool, the detail response includes:

- `id`
- `node_id`
- `name`
- `online`
- `stale`
- `contract_hash`
- `input_schema`
- optional `output_schema`

Do not cache a Tool detail indefinitely. Read it again immediately before execution.

## Create Execution

```http
POST /v1/integrations/executions
Idempotency-Key: <stable key for this exact logical request>
Content-Type: application/json
```

```json
{
  "capability_id": "cap_...",
  "contract_hash": "sha256:...",
  "input": {},
  "timeout_ms": 120000
}
```

`timeout_ms` is optional and must be between 1000 and 1800000.

Responses:

- `202`: new Execution queued.
- `200` with `Idempotency-Replayed: true`: the exact same idempotent request already exists.
- `409 CAPABILITY_SCHEMA_CHANGED`: fetch the capability detail again and rebuild the input.
- `409 IDEMPOTENCY_KEY_REUSED`: the key was already used for a different request. Do not alter the request and keep the same key.
- `429`: respect `Retry-After`; do not create a different idempotency key merely to bypass rate/capacity protection.

## Read/list/cancel

```http
GET  /v1/integrations/executions
GET  /v1/integrations/executions/{id}
POST /v1/integrations/executions/{id}/cancel
```

List filters:

- `status=queued|running|succeeded|failed|cancelled|unknown`
- `before=<execution id>`
- `limit=1..100`

Cancellation is best effort. A Tool may already have produced side effects before cancellation is observed.

## SSE events

```http
GET /v1/integrations/executions/{id}/events
Accept: text/event-stream
Last-Event-ID: <version>
```

Events contain status/version metadata, not the final Tool result. After a terminal event, read `GET /executions/{id}` for the final detail.

## Status semantics

| Status | Meaning | Automatic re-execution |
|---|---|---|
| `queued` | persisted, not yet dispatched | no |
| `running` | Cloud has claimed it and may have dispatched it | no |
| `succeeded` | final Tool result is known | not needed |
| `failed` | final failure is known | only after considering the specific error and user intent |
| `cancelled` | cancellation was accepted/processed | no |
| `unknown` | Tool may have executed, but the final result cannot be proven | **never** |

The `unknown` rule is the most important safety property: a fresh Execution could repeat a side effect.

## Recovery rules

- `CAPABILITY_SCHEMA_CHANGED`: refresh capability detail; compare the new schema with the user's intent before creating a new logical execution.
- Network failure before an Execution ID is returned: retry the same POST with the **same** Idempotency-Key and identical body.
- Network failure after an Execution ID is known: read that Execution; do not create another one.
- `unknown`: inspect external state or ask the user before any new execution.
- `429`: respect server backoff; keep the same logical request and idempotency key.
