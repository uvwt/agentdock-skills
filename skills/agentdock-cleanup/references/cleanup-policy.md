# Cleanup policy and producer integration

## Scope and allowlist

`CLEANUP_SCOPE_JSON` is a host-injected JSON object. Optional keys are `workspace`, `runtime_tmp`, `temp`, `profile_cache`, `npm_cache`, `logs`, and `protected` (an array). `profile_cache` explicitly identifies a browser profile cache outside TEMP; it and all profile-named subtrees are report-only even when scopes overlap. Values are absolute local paths, with no traversal, symlinks, junctions, device paths, alternate data streams, UNC paths or volume roots. All roles are report scopes; none alone proves ownership. At least one report scope is required for scan/plan. The host must verify these locations independently. Do not take a generic project path and label it as runtime storage.

The host should supply additional exact protected roots for its configuration, state, authentication, installed skills and binaries. The built-in protected names/extensions are defense in depth. They do not replace the positive deletion allowlist or the signed ownership evidence. Unknown workspace files remain `USER_ARTIFACT`.

| Location | Exact candidate form | Required receipt kind | Delete support |
|---|---|---|---|
| Directly in `workspace/.playwright-mcp` | `page-` or `snapshot-` + ISO-like timestamp + `.yml`/`.yaml` | `playwright_snapshot` | Closed, signed ordinary files only |
| Same | `console-` + timestamp + `.log` | `playwright_console` | Same |
| Same | `page-` or `screenshot-` + timestamp + `.png` | `playwright_screenshot` | Same |
| Directly in `runtime_tmp` | `agentdock-` + 32 hexadecimal characters + `.tmp` | `runtime_temp` | Same |
| TEMP matching isolated profiles or `ms-playwright-mcp` | Any directory contents | None accepted for deletion | Report only |
| npm cache, `_cacache`, `_npx` | Any | None accepted for deletion | Report only |
| Logs role | Any | None accepted for deletion | Statistics and retention/rotation recommendation |

Timestamp syntax is fixed in `PLAYWRIGHT_NAMES` in `run.py`. Nonmatching output names and legacy tmp names remain report-only. The runtime tmp naming convention is a **new producer adapter contract**, not a claim that all current AgentDock builds already generate these names. No upstream runtime code is changed by this Skill.

`PROTECTED` wins over candidates, and shared-cache boundaries win even if the host also configures them as a runtime root. Profile directories are `CONDITIONAL`; active ones have `in_use=true`. Names such as config/auth/secret/credentials/DPAPI/token/task/runtime configuration, protected root descendants, source extensions, executable libraries and installed Skill storage are retained. Generic `SAFE_CACHE` classification is never a deletion permission.

## Authenticated ownership receipts

No existing producer receipt API has been verified in the current Skill repository/package contract. This release consumes a proposed external adapter format; it does not enroll existing resources. Until a trusted AgentDock producer is integrated, expect **zero eligible bytes**, even when large candidate directories are found. Do not generate receipts from an inventory scan to overcome this restriction.

The producer records a receipt only after it created and closed an allowed artifact. The trusted host supplies a random 32-byte signing key through `CLEANUP_SIGNING_KEY`; receipt storage and the key must be writable/readable only by the intended OS principal. A hostile process with the same user's secret or arbitrary code-execution privileges is outside this helper's trust boundary. HMAC authentication does not protect against a compromised producer or host. Key creation, secure distribution and producer integration are not performed by this package.

The external JSON registry has this shape (values below are descriptive placeholders, not usable signatures):

```json
{
  "version": 1,
  "receipts": [
    {
      "body": {
        "owner": "agentdock",
        "producer": "playwright-mcp",
        "kind": "playwright_snapshot",
        "path": "<canonical absolute artifact path>",
        "closed": true,
        "fingerprint": {
          "device": 0,
          "inode": 0,
          "size": 0,
          "mtime_ns": 0,
          "change_time_ns": 0,
          "sha256": "<content SHA-256>"
        }
      },
      "signature": "<HMAC-SHA256 lowercase hexadecimal>"
    }
  ]
}
```

For `runtime_temp`, producer is exactly `agentdock`; for Playwright kinds it is exactly `playwright-mcp`. `closed` must be the JSON boolean true. Registry reads are limited to 2 MiB and invalid/unreadable receipts grant no eligibility.

Canonical JSON is Python `json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)` encoded UTF-8. Receipt signature is HMAC-SHA256 over ASCII `ownership`, a NUL byte, then canonical body JSON. The fingerprint comes from the producer's final ordinary file; Windows ChangeTime is read from the native handle in 100 ns ticks multiplied by 100 (not the POSIX epoch), consistently for receipts and revalidation. Files above 64 MiB are report-only in v1. The helper has no receipt-creation action.

## Plans, confirmation and replay

Plans are returned to the caller, never persisted by the helper. `plan_id` is SHA-256 of canonical plan body JSON; the signature is HMAC over `plan`, NUL and that body. The body binds the exact item paths/fingerprints/sizes, policy/package version, host scope digest, creation/expiry and a random nonce. The top-level `items` projection must equal signed body items.

The caller must retain the complete plan and collect explicit confirmation **after** displaying it. Both confirmation `plan_id` and top-level `plan_id` must match. The confirmation phrase is an exact, case-sensitive string. A signed plan does not authorize a changed target: clean re-evaluates eligibility, receipt validity, fingerprint, retention and process use for every item. Expiry is fixed at 15 minutes; retention is seven days and cannot be lowered in input.

There is no persistent used-plan journal because scan/plan must not create state. Replaying against a missing or replaced artifact yields `PLAN_STALE`; unchanged items skipped in a partially executed plan can be retried within its validity after obtaining appropriate confirmation. Logical file bytes are reported; filesystem allocation savings can differ.

## Process and filesystem race protection

Windows uses a bounded `Get-CimInstance Win32_Process` snapshot. Node, Chrome, Edge, AgentDock, Playwright or Python processes with unavailable command lines make visibility incomplete. A query failure or timeout also blocks eligibility. Paths are compared case-insensitively after slash normalization. A profile in `--user-data-dir` or an `_npx` ancestor of a running MCP CLI path is reported in use. Conservative substring matches may retain extra files. Raw command lines, environment values and credentials are never emitted.

Before each deletion, process and producer state are re-read. The Windows backend then pins every ancestor directory with no delete/rename sharing, opens the file with read/delete access and zero sharing, rejects reparse attributes and unexpected final handle paths, and checks identity, size, write/change time and SHA-256 through that same handle. After another process and receipt check, `SetFileInformationByHandle(FileDispositionInfo)` marks that handle for deletion. The stream is closed and the path is checked for absence. It never calls recursive deletion, path-based unlink, a shell cleanup command, or a fallback after sharing violations.

The mechanism uses the Windows [CreateFile sharing contract](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew) and [handle-based file information updates](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-setfileinformationbyhandle). Windows and the underlying filesystem remain the enforcement boundary. This is not an atomic transaction across all plan items. Directory/profile removal remains disabled because producer quiescence and a verified inventory would be needed to make recursive removal safe. The helper does not stop processes, alter ACLs or elevate privileges.

Reporting scans are metadata snapshots and can become stale. Link changes during a read-only size estimate do not confer deletion permission; mutation uses the separate locked revalidation path. On unsupported platforms the real process snapshot is incomplete and `clean` is unavailable.

## Stable outcomes

| Code | Meaning |
|---|---|
| `ELIGIBLE` | May be included in a plan; still requires confirmation/revalidation |
| `CONFIRMATION_REQUIRED` | Missing exact plan binding or phrase |
| `INVALID_PLAN` | Changed/unsigned plan or mismatched projection |
| `PLAN_STALE` | Expired plan, changed scope, missing/replaced/modified artifact or revoked receipt |
| `RESOURCE_NOW_IN_USE` | Process reference or OS sharing conflict |
| `PROCESS_STATE_UNKNOWN` | Incomplete process visibility |
| `OUTSIDE_CLEANUP_BOUNDARY` | Out-of-scope path, traversal, alias or reparse point |
| `PROTECTED_RESOURCE` | Protected state, credentials, source or host-provided root |
| `OWNERSHIP_UNPROVEN` | Unrecognized file or absent/invalid/nonmatching receipt |
| `SHARED_RESOURCE` | npm shared resources, always report-only |
| `DIRECTORY_REPORT_ONLY` / `LOG_REPORT_ONLY` | Unsupported removal category |
| `LINK_NOT_ALLOWED` | Hardlink or nonordinary file |
| `RETENTION_NOT_MET` / `FILE_TOO_LARGE` | Conservative v1 limits |
| `RESOURCE_MISSING` / `RESOURCE_UNREADABLE` | Read-only observation failed |
| `WOULD_DELETE` | Dry-run revalidation succeeded, no deletion |
| `DELETE_DENIED` | OS denied guarded operation; do not retry with elevated privileges |
| `VERIFY_FAILED` | Target path is still present; do not claim reclaimed space |
| `DELETED` | Guarded operation followed by confirmed absence |

Unknown or partially measured resources must never be represented as reclaimed space. Shared/cache totals may overlap children and must be presented as estimates.
