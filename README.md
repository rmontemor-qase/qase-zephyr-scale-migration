# Zephyr Scale Cloud → Qase Migration

Python script that migrates test data from **Zephyr Scale Cloud** into **Qase**. It uses the [Zephyr Scale Cloud REST API v2](https://support.smartbear.com/zephyr-scale-cloud/api-docs/) and the [Qase API](https://developers.qase.io/).

## What it migrates

### Projects

| Source | Qase |
| --- | --- |
| Zephyr Scale project name and key | Project **title** and **code** |

### Folders → suites

| Source | Qase |
| --- | --- |
| ZS folder tree (root and nested) | **Suites** — full hierarchy preserved |

### Test cases

| Source | Qase |
| --- | --- |
| Case **name** | Case **title** |
| **Objective** / **description** (HTML stripped) | Case **description** |
| **Precondition** (HTML stripped) | Case **preconditions** |
| **Priority** (Highest/High/Normal/Low/Lowest) | Case **priority** |
| **Status** (Approved/Draft/Deprecated) | Case **status** |
| **Folder** | Case **suite** |
| **Labels** | Case **tags** (up to 10) |
| **Custom fields** | Qase custom fields (when mapped) |
| **Step-by-step** test script (`inline.description`, `inline.expectedResult`, `inline.testData`) | Case **steps** |
| **File attachments** (from the Attachments tab) | Case **attachments** — downloaded via ZS REST API and re-uploaded to Qase |

### Test cycles → runs

| Source | Qase |
| --- | --- |
| Cycle **name** | Run **title** |
| Cycle **description** | Run **description** |
| Cycle **created/completed dates** | Run **timestamps** |
| Cycle **status** (Done/Completed → run completed) | Run **completed** flag |
| Referenced test cases | Run **case list** |

### Test executions → results

| Source | Qase |
| --- | --- |
| Execution **status** (Pass/Fail/Blocked/Skip/WIP/Unexecuted) | Result **status** |
| Execution **comment** | Result **comment** |
| Execution **date** | Result **timestamp** |
| **File attachments** on execution | Result **attachments** — downloaded via ZS REST API and re-uploaded to Qase |

---

## Limitations

### Inline images in rich-text fields are not migrated

ZS Cloud stores images embedded in rich-text fields (objective, precondition, description) on SmartBear's own CloudFront CDN (`cloudfront.tm4j.smartbear.com`). Downloading from that CDN requires a **Forge JWT** that is only issued by the ZS JavaScript app running inside an authenticated browser session — it cannot be obtained through any REST API.

Inline images are replaced with a text note in the form `[Image: filename.png]` so the migrated case preserves a record of what images existed.

**File attachments** (uploaded via the Attachments tab) are a separate concern and are fully migrated. ZS serves them directly through the REST API (`GET /testcases/{key}/attachments/{id}`) using the same Bearer token, with no CDN involvement.

### Zephyr Scale Cloud only

This script targets the Zephyr Scale Cloud REST API v2. It does not support Zephyr Scale Server/Data Center, Zephyr Squad, or Zephyr Enterprise — those products use different APIs and data models.

### Users are not migrated

Zephyr Scale Cloud does not expose user details through the API in a way that maps cleanly to Qase users. All migrated test cases and run results are assigned to the `users.default` Qase user ID specified in the config.

### Test plans are not migrated

Zephyr Scale test plans are not fetched or mapped. Only test cycles (and the executions within them) are imported as Qase runs.

### Custom fields — partial support

Custom fields are fetched via `GET /customfields` and mapped by ID. Only fields whose values are scalar strings, numbers, or single-select are reliably mapped. Multi-select, rich-text, and user-type custom fields may be coerced to strings or dropped.

### BDD / plain-text test scripts

ZS test cases with `testScript.type = "BDD"` or `"PLAIN_TEXT"` do not have structured steps. Their script content is not currently extracted or mapped to Qase steps.

### Run filtering is approximate

The `runs.created_after` Unix timestamp filters cycles client-side after fetching from the ZS API, because the v2 `/testcycles` endpoint does not support a server-side date filter. Very large cycle lists are fetched in full before filtering.

### `POST /testcases` silently ignores the `folder` field

This is a known ZS API quirk. The script always follows case creation with a `PUT /testcases/{key}` call to assign the folder. If a `PUT` fails, the case will exist in Qase but will not be placed in the correct suite.

### Attachment size limits

Qase enforces a per-file size limit on uploads. Attachments exceeding the limit are skipped and logged as warnings. The Qase limit varies by plan; check your workspace settings if large attachments are expected.

### Executions with "in progress" or "untested" status are skipped

Executions whose status maps to `in_progress` or `untested` do not produce a result row in Qase. The associated test case is included in the run's case list but left untested.

---

## Prerequisites

- Python 3.11 or newer
- A Zephyr Scale Cloud API token
- A Qase API token
- Network access to both `api.zephyrscale.smartbear.com` and `api.qase.io`

### Getting a Zephyr Scale API token

1. In Jira, go to **Apps** → **Zephyr Scale**.
2. Open your profile menu (top-right) → **ZephyrScale API Access Tokens**.
3. Generate a new token and copy it.

### Getting a Qase API token

Open your Qase workspace → **Settings** → **API tokens** → **Generate token**.

---

## Installation

```bash
git clone <repository-url>
cd qase-zephyr-scale-migration
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

---

## Configuration

Copy the example file and fill in your credentials:

```bash
cp config.example.json config.json
```

`config.json` is excluded from version control — never commit it.

### Full example

```json
{
    "qase": {
        "api_token": "<QASE_API_TOKEN>",
        "host": "qase.io",
        "ssl": true,
        "request_max_retries": 5,
        "request_retry_backoff_sec": 2
    },
    "users": {
        "migrate": false,
        "default": 1
    },
    "zephyr_scale": {
        "api": {
            "token": "<ZEPHYR_SCALE_API_TOKEN>",
            "host": "https://api.zephyrscale.smartbear.com/v2"
        }
    },
    "projects": {
        "import": ["ZSM", "PROJ2"],
        "status": "all"
    },
    "runs": {
        "created_after": 0
    },
    "jira": {
        "base_url": "https://yourcompany.atlassian.net",
        "email": "you@example.com",
        "api_token": "<ATLASSIAN_API_TOKEN>"
    }
}
```

### Configuration reference

#### `qase`

| Field | Description |
| --- | --- |
| `api_token` | Qase personal API token |
| `host` | Qase API host. Default: `qase.io`. Change only for dedicated clusters |
| `ssl` | Use HTTPS. Default: `true` |
| `request_max_retries` | Max retries on transient errors. Default: `5` |
| `request_retry_backoff_sec` | Base backoff in seconds between retries. Default: `2` |

#### `users`

| Field | Description |
| --- | --- |
| `migrate` | User migration. Currently not supported — always set to `false` |
| `default` | Qase user ID to assign as author when no user mapping is available |

#### `zephyr_scale.api`

| Field | Description |
| --- | --- |
| `token` | Zephyr Scale Cloud API token (Bearer JWT) |
| `host` | ZS API base URL. Default: `https://api.zephyrscale.smartbear.com/v2` |

#### `projects`

| Field | Description |
| --- | --- |
| `import` | List of Zephyr Scale project keys to migrate. Use `[]` to migrate all accessible projects |
| `status` | Filter by project status: `all`, `active`, or `archived` |

#### `runs`

| Field | Description |
| --- | --- |
| `created_after` | Unix timestamp (seconds). Only cycles created after this date are migrated. Set to `0` to migrate all |

#### `jira` (optional — for file attachment downloads)

Jira credentials are used to download file attachments that are hosted in the Jira instance rather than on the ZS API directly. The Atlassian API token is a standard personal token, not the ZS token.

| Field | Description |
| --- | --- |
| `base_url` | Jira Cloud base URL, e.g. `https://yourcompany.atlassian.net` |
| `email` | Atlassian account email |
| `api_token` | Atlassian API token (create at https://id.atlassian.com/manage-profile/security/api-tokens) |

---

## Run

```bash
source .venv/bin/activate
python start.py
```

With a custom config path:

```bash
python start.py path/to/config.json
```

Logs are written to `logs/` and migration statistics to `stats/`.

---

## Migration order

The importer runs phases in this sequence:

1. **Projects** — Fetch ZS projects and create or match them in Qase. Builds the project key map.
2. **Fields** — Resolve Qase system field option IDs (priority, status) for use during case creation.
3. **Per-project data** (projects run in parallel):
   - **Suites** — Fetch ZS folder tree and create Qase suites top-down so parents exist before children.
   - **Cases** — Fetch all test cases, resolve priorities and statuses, fetch step details, download and upload file attachments, then bulk-create in Qase.
   - **Runs** — Fetch test cycles, create Qase runs, download execution attachments, and bulk-submit results.

---

## Project structure

```
qase-zephyr-scale-migration/
├── start.py                                  # Entry point
├── config.example.json                       # Config template
├── requirements.txt
├── src/
│   ├── importer.py                           # Orchestration (phases 1–3)
│   ├── api/
│   │   ├── zephyr_scale.py                   # ZS Cloud REST API client
│   │   └── qase_scim.py                      # Qase SCIM client (optional)
│   ├── service/
│   │   ├── qase.py                           # Qase SDK wrapper
│   │   ├── qase_scim.py
│   │   └── zephyr_scale.py                   # ZS service wrapper
│   ├── entities/
│   │   └── zephyr_scale/
│   │       ├── projects.py                   # Project import
│   │       ├── fields.py                     # System field resolution
│   │       ├── suites.py                     # Folder → suite import
│   │       ├── cases.py                      # Test case import + attachments
│   │       ├── runs.py                       # Cycle → run + execution → result import
│   │       └── attachments.py               # Attachment coordination
│   └── support/
│       ├── config_manager.py                 # Dot-path config access
│       ├── logger.py
│       ├── mappings.py                       # ID cross-reference registry
│       ├── pools.py                          # Thread pool wrappers
│       ├── stats.py                          # Migration statistics
│       ├── throttled_pool.py                 # Rate-limited executor
│       └── zs_cdn_session.py                 # CDN session stub (see Limitations)
├── logs/                                     # Runtime logs (gitignored)
└── stats/                                    # Migration stats output (gitignored)
```

---

## Troubleshooting

### Authentication errors (401)

- Verify `zephyr_scale.api.token` — ZS tokens expire; generate a fresh one from the ZS app.
- Verify `qase.api_token` — confirm the token is active in Qase Settings → API tokens.
- If Jira attachment downloads fail with 401, check `jira.email` and `jira.api_token`.

### No projects found

- Confirm the keys in `projects.import` match the actual Zephyr Scale project keys (case-sensitive).
- Set `projects.import` to `[]` to import all accessible projects and check which keys appear in the log.

### Cases created but not placed in suites

This is usually caused by the ZS API `POST /testcases` silently ignoring the `folder` field. The script works around this by issuing a `PUT /testcases/{key}` after each create. If you see suite_id warnings in the log, check whether the ZS API returned a successful `PUT` for those cases.

### Attachments missing or skipped

- File attachments failing with 401 may need Jira credentials — add the `jira` section to `config.json`.
- Inline images in objective/precondition fields are intentionally not downloaded (see [Limitations](#inline-images-are-not-migrated)). They appear as `[Image: filename]` text in the migrated case description.
- Attachments that exceed Qase's per-file size limit are skipped and logged as warnings.

### Rate limiting (429)

The ZS API enforces rate limits per account. The script uses exponential backoff on 429 responses. If you see persistent throttling, reduce concurrent API calls by lowering the worker count in `src/importer.py` (`_ZEPHYR_SOURCE_POOL_WORKERS`).

### Run results missing

- Executions with status `in_progress` or `untested` are intentionally skipped — they are included in the run's case list but no result row is created.
- Confirm the executions exist in ZS for the cycle and that `runs.created_after` is not filtering them out.

---

## API references

- [Zephyr Scale Cloud REST API v2](https://support.smartbear.com/zephyr-scale-cloud/api-docs/)
- [Qase API reference](https://developers.qase.io/)
