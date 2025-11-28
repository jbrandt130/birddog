
# 📘 Birddog Web Service API Reference

This document describes the HTTP endpoints exposed by the Birddog web service, which supports:

- User signup, login, and password management  
- Archive navigation and page retrieval  
- Spreadsheet export and download  
- Watchlist management and resolve helpers  
- Translation job control  
- Service logs and metrics  

Unless noted otherwise, endpoints that operate on user-specific data require a valid session, which is enforced via an internal helper:

```python
user, error_response, status = _get_current_user()
if error_response:
    return error_response, status
```

When `_get_current_user` fails, endpoints return:

- **404 Not Found** – no valid user session (not logged in / expired)  
- **503 Service Unavailable** – Birddog runtime not running or unreachable  

These status codes appear on most API methods that touch user state.

---

## 🔎 API Quick Reference

| Path                                                          | Method(s)        | Description                                           | Auth / Session                       | Response Type                       |
|---------------------------------------------------------------|------------------|-------------------------------------------------------|--------------------------------------|-------------------------------------|
| `/signup`                                                     | `POST`           | Create a new user account                             | Public                               | JSON                                |
| `/login`                                                      | `POST`           | Authenticate user and start a session                 | Public                               | JSON                                |
| `/logout`                                                     | `GET`            | Logout and redirect to home                           | Public (acts on current session)     | Redirect (HTML)                     |
| `/change_password`                                            | `POST`           | Change password for logged-in user                    | **Session required**                 | JSON                                |
| `/reset_password`                                             | `POST`           | Request password reset email                          | Public                               | JSON                                |
| `/reset_password/<token>`                                     | `GET`, `POST`    | Reset password via email token                        | Public (token-gated)                 | HTML (forms / redirect)             |
| `/archives`                                                   | `GET`            | List available archives                               | **Session required**                 | JSON                                |
| `/page`                                                       | `GET`            | Get archive page data (metadata + history)            | **Session required**                 | JSON                                |
| `/export`                                                     | `GET`            | Get export dialog metadata for a page                 | **Session required**                 | JSON                                |
| `/download`                                                   | `POST`           | Start async Excel export job                          | **Session required**                 | JSON (`202 Accepted`)               |
| `/download`                                                   | `GET`            | Poll export job / download Excel file                 | **Session required**                 | JSON (`202`) or XLSX (`200`)        |
| `/watchlist`                                                  | `GET`            | Get current user’s watchlist                          | **Session required**                 | JSON                                |
| `/watchlist`                                                  | `POST`           | Add/update watchlist entry                            | **Session required**                 | JSON (`201 Created`)                |
| `/watchlist/<archive>/<subarchive>`                           | `DELETE`         | Remove watchlist entry                                | **Session required**                 | Empty (`204`) or JSON error         |
| `/watchlist/<archive>/<subarchive>/check`                     | `GET`            | Check for updates for one watchlist entry             | **Session required**                 | JSON                                |
| `/resolve` and `/resolve/...` variants                        | `GET`            | Resolve / update watchlist item(s)                    | **Session required**                 | JSON                                |
| `/translate`                                                  | `GET`            | Start translation (optional) and report status        | **Session required**                 | JSON                                |
| `/log`                                                        | `GET`            | Return recent service logs                            | **Session required**                 | JSON                                |
| `/logs`                                                       | `GET`            | Render logs view                                      | **Session required**                 | HTML                                |
| `/service_usage`                                              | `GET`            | Service usage dashboard                               | Public                               | HTML                                |
| `/good_dog`                                                   | `GET`            | Unpause runtime                                       | Public                               | JSON                                |
| `/bad_dog`                                                    | `GET`            | Pause runtime                                         | Public                               | JSON                                |
| `/metrics`                                                    | `GET`            | Metrics dashboard (requests, durations, users)        | **Session required**                 | HTML                                |

---

## 🔐 Authentication & Session

### `POST /signup`

Create a new user account.

**Method:** `POST`  
**Path:** `/signup`  
**Request body:** `application/json`

**Request JSON**

- `name` (string, required) – User’s display name  
- `email` (string, required) – User email (must be unique)  
- `password` (string, required) – User password  

**Behavior**

- Calls `users.create(email, name, password)`.  
- If creation succeeds, logs creation and returns a success flag.  
- If the email is already registered, returns an error.

**Responses**

- `200 OK`

  ```json
  { "success": true }
  ```

- `400 Bad Request` – email already exists

  ```json
  { "success": false, "message": "Email already exists" }
  ```

---

### `POST /login`

Authenticate a user and start a session.

**Method:** `POST`  
**Path:** `/login`  
**Request body:** `application/json`

**Request JSON**

- `email` (string, required)  
- `password` (string, required)

**Behavior**

- Calls `users.login(email, password)`.  
- On success, session is established (via whatever mechanism `users.login` uses).  
- On failure, returns 401.

**Responses**

- `200 OK`

  ```json
  { "success": true }
  ```

- `401 Unauthorized` – invalid email or password

  ```json
  { "success": false, "message": "Invalid email or password" }
  ```

---

### `GET /logout`

End the current user session and redirect to the home page.

**Method:** `GET`  
**Path:** `/logout`

**Behavior**

- Calls `users.logout()`.  
- Redirects to the `home` route.

**Response**

- `302 Found` – HTTP redirect to home (HTML redirect, not JSON)

---

### `POST /change_password`

Change the password of the currently logged-in user.

**Method:** `POST`  
**Path:** `/change_password`  
**Request body:** `application/json`

**Authentication**

- Uses `_get_current_user`. If that fails, returns its error (404/503).  
- If `_get_current_user` returns `user = None`, explicitly returns 401.

**Request JSON**

- `current` (string, required) – Current password  
- `new` (string, required) – New password  

**Behavior**

1. Ensures the caller is logged in.  
2. Determines `email` from the current user.  
3. Looks up the user object via `users.lookup(email)`.  
4. If found, calls `user.change_password(current_pw, new_pw)`.

**Responses**

- `200 OK` – password changed

  ```json
  { "success": true, "message": "Password changed successfully" }
  ```

- `401 Unauthorized` – not logged in

  ```json
  { "success": false, "message": "Not logged in" }
  ```

- `403 Forbidden` – current password is incorrect

  ```json
  { "success": false, "message": "Current password is incorrect" }
  ```

- `404 Not Found` – user not found

  ```json
  { "success": false, "message": "User not found" }
  ```

- `404 / 503` – from `_get_current_user`

---

### `POST /reset_password`

Request a password reset email.

**Method:** `POST`  
**Path:** `/reset_password`  
**Request body:** `application/json`

**Request JSON**

- `email` (string, required)

**Behavior**

- Looks up user via `users.lookup(email)`.  
- If the user exists:
  - Creates a time-limited token via `serializer.dumps(email, salt='reset-password')`.  
  - Sends an email with a reset link to `/reset_password/<token>`.  
- If the user does **not** exist:
  - Returns the same “success” response (to avoid leaking which emails are registered).

**Responses**

- `200 OK` (for both known and unknown emails)

  ```json
  {
    "success": true,
    "message": "If that email is registered, a reset link was sent."
  }
  ```

  or, when user is known:

  ```json
  {
    "success": true,
    "message": "Check your email for a reset link."
  }
  ```

---

### `GET|POST /reset_password/<token>`

Reset a password using a valid reset token.

**Methods:** `GET`, `POST`  
**Path:** `/reset_password/<token>`  
**Response type:** HTML (rendered templates)

**Behavior**

1. Attempts to decode token:

   ```python
   email = serializer.loads(token, salt='reset-password', max_age=3600)
   ```

2. If token is invalid/expired, or user cannot be found, renders `reset_password_expired.html`.  
3. For `GET`:
   - Renders `reset_password_form.html` with the token.  
4. For `POST`:
   - Reads `password` from `request.form`.  
   - If empty, re-renders the form with an error.  
   - If non-empty, calls `user.set_password(new_pw)` and redirects to `home`.

**Responses**

- `200 OK` – HTML forms (valid/expired pages)  
- `302 Found` – redirect to home after successful reset  

---

## 📁 Archive Navigation

### `GET /archives`

Return the list of available archives and their associated root pages.

The response is a **JSON array**, where each array element is a **triple**:

```
[ archive_code, subarchive_code, canonical_unicode_page_title ]
```

### Meaning of Each Field

1. **archive_code**  
   Short uppercase archive identifier  
   Examples: `"DAARK"`, `"DACHGO"`, `"DAHMO"`, `"GDA"`

2. **subarchive_code**  
   A subdivision code  
   - Often a single letter such as `"D"`, `"R"`, `"P"`, `"K"`  
   - Sometimes a descriptive string, such as `"Digital collections"` or `"Church records"`

3. **canonical_unicode_page_title**  
   The full MediaWiki page title (Unicode), e.g.:  
   ```
   "Архів:ДААРК/Д"
   ```
   Note that titles may contain Cyrillic and other non‑ASCII characters.

**Method:** `GET`  
**Path:** `/archives`  
**Response type:** `application/json`  
**Authentication:**  
Uses `_get_current_user()`  
- `404` — if no active session  
- `503` — if runtime is unavailable  

### Example Response

```json
[
  ["DAARK", "D", "Архів:ДААРК/Д"],
  ["DACHGO", "D", "Архів:ДАЧго/Д"],
  ["DACHGO", "R", "Архів:ДАЧго/Р"],
  ["DAHMO", "K", "Архів:ДАХмо/К"],
  ["DAPO", "N", "Архів:ДАПо/Н"],
  ["DISZMO", "Digital collections", "Архів:ДІСЗМО/Цифрові_колекції"],
  ["KPDIMZ", "Church records", "Архів:Кам'янець-Подільський_державний_історичний_музей-заповідник/Церковні_літописи"]
]
```

### Success Responses

- `200 OK`  
  Returns the array of triples described above.

### Error Responses

- `404 Not Found` — user session missing  
- `503 Service Unavailable` — runtime not running
---

### `GET /page`

Return a structured representation of a given archive page (including lineage, basic metadata, and compressed history).

**Method:** `GET`  
**Path:** `/page`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `title` (string, required) – Page title to load  
- `compare` (string, optional) – Modification date string for comparison  
  - Format: `YYYY,MM,DD,hh:mm`

**Behavior**

1. If `title` is missing, returns 400.  
2. Uses `runtime.lookup_by_title(title)` and `page_address(title)` to resolve the page.  
3. If page is found:
   - If `compare` is provided, calls `page.compare(ref_date)`.  
   - Recomputes canonical address from `page.title`.  
   - Builds `page_dict` from `page.page` with extra fields:
     - `title`, `lineage`, `archive`, `subarchive`, `fond`, `opus`, `case`  
     - `kind`, `name`, `needs_translation`  
     - `history` – compressed via `_compress_history(page.history(cutoff_date='2000'))`  
   - Stores `user.set_preference("last_page", page.title)`.  
   - Returns JSON.

4. If page not found, logs error and returns 404.  

**Successful Response**

- `200 OK`

  ```json
  {
    "title": "Archive:123/SomePage",
    "lineage": [ ... ],
    "archive": "...",
    "subarchive": "...",
    "fond": "...",
    "opus": "...",
    "case": "...",
    "kind": "...",
    "name": "...",
    "needs_translation": true,
    "history": [ ... ],
    "...": "other page fields"
  }
  ```

**Error Responses**

- `400 Bad Request`

  ```text
  Missing required parameter: 'title'
  ```

- `404 Not Found`

  ```text
  Page not found
  ```

- `404 / 503` – from `_get_current_user`

---

## 📄 Export & Download

### `GET /export`

Return metadata needed to drive the spreadsheet export dialog for a given page.

**Method:** `GET`  
**Path:** `/export`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `title` (string, required) – Page title whose tables are to be exported

**Behavior**

1. If `title` missing, returns 400 JSON error.  
2. Tries `runtime.lookup_by_title(title)`; on exception, returns 404 JSON error.  
3. Builds `column_headers[table["name"]]` for each `table` in `page.tables` using `_make_unique`.  
4. Loads user preferences `export_{page.title}` and extracts:
   - `template`, `table`, `column_map`.  
5. Validates default table exists; otherwise clears it.  
6. If no default template, chooses one from `list_templates()` containing `page.kind`, or `"opus.xlsx"`.  
7. If no default table, uses first table name or `""`.  
8. For tables missing from `header_map`, infers a mapping via `classify_table_columns`.

**Successful Response**

- `200 OK`

  ```json
  {
    "title": "Archive:123/SomePage",
    "default_template": "opus.xlsx",
    "default_table": "main",
    "templates": [ "archive.xlsx", "fond.xlsx", "opus.xlsx" ],
    "column_classes": { "...": "..." },
    "column_headers": { "main": [ "Fond", "Opus", "Case" ] },
    "column_header_map": {
      "main": {
        "fond": [0],
        "opus": [1],
        "case": [2]
      }
    }
  }
  ```

**Error Responses**

- `400 Bad Request`

  ```json
  { "error": "Missing required parameter \"title\"" }
  ```

- `404 Not Found`

  ```json
  { "error": "Page not found" }
  ```

- `404 / 503` – from `_get_current_user`

---

### `POST /download` (start export job)

Start an asynchronous export job for a given page using the specified template, table, and column mapping.

**Method:** `POST`  
**Path:** `/download`  
**Request body:** `application/json`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Request JSON**

> All of these are accessed via `data["..."]` in the code and are effectively required.

- `title` (string) – Page title to export  
- `template` (string) – Template name  
- `table` (string) – Table name on the page  
- `column_map` (object) – Column mapping for this table  
- `compare` (string) – Modification date string  
  - Format: `YYYY,MM,DD,hh:mm`

**Behavior**

1. Validates user session.  
2. Reads request JSON and logs it.  
3. Uses `runtime.lookup_by_title(title)`; if no page, returns `404 "Page not found"`.  
4. Loads and updates export defaults under `export_{page.title}`:
   - Saves `template`, `table`, and merged `column_map` (pruned to existing tables).  
5. Starts export job via `runtime.export_manager.export_page(...)`.  
6. Returns `202` with `status` and `task_id`.

**Successful Response**

- `202 Accepted`

  ```json
  {
    "status": "in-progress",
    "task_id": "<task-id>"
  }
  ```

**Error Responses**

- `404 Not Found` – page not found

  ```text
  Page not found
  ```

  or

  ```json
  { "error": "Page not found" }
  ```

- `500 Internal Server Error` – generic exception (including missing fields)

  ```json
  { "error": "Internal server error" }
  ```

- `404 / 503` – from `_get_current_user`

---

### `GET /download` (poll / download file)

Poll the status of an export job and, once complete, download the `.xlsx` file.

**Method:** `GET`  
**Path:** `/download`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `task_id` (string, optional but expected) – Export job identifier  
- `title` (string, required) – Page title (for lookup & filename)

**Behavior**

1. Validates session.  
2. Reads `task_id` and `title`.  
3. Uses `runtime.lookup_by_title(title)`; if no page, returns `404 "Page not found"`.  
4. Logs current system resources.  
5. Checks job completion: `runtime.export_manager.is_complete(task_id)`  
6. If complete:
   - Fetches `excel_io` via `get_result(task_id)`.  
   - If `excel_io` truthy, uses `send_file` with:
     - `download_name = "<ascii-page-name>.xlsx"`  
     - XLSX MIME type  
   - If `excel_io` falsy, returns 500 JSON error.  
7. If not complete, returns `202` with `"status": "in-progress"`.

**Successful Responses**

- `200 OK` – file download (XLSX)  
- `202 Accepted` – still in progress

  ```json
  {
    "status": "in-progress",
    "task_id": "<task-id-or-null>"
  }
  ```

**Error Responses**

- `404 Not Found`

  ```text
  Page not found
  ```

- `500 Internal Server Error`

  ```json
  { "error": "Internal server error" }
  ```

- `404 / 503` – from `_get_current_user`

---

## 👁️ Watchlist

### `GET /watchlist`

Get the current user’s watchlist.

**Method:** `GET`  
**Path:** `/watchlist`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Behavior**

- Reads `user.watchlist` (a mapping keyed by `"archive-subarchive"`).  
- Formats the watchlist into a list of objects via `_format_watchlist`.

Each item has:

- `archive` (string)
- `subarchive` (string)
- `last_checked_date` (string or `null`)
- `cutoff_date` (string or `null`)
- `title` (string or `null`) – human-readable archive title, via `ARCHIVE_BY_ADDRESS`

**Response**

- `200 OK`

  ```json
  [
    {
      "archive": "archive1",
      "subarchive": "sub1",
      "last_checked_date": "2024,01,01,12:00",
      "cutoff_date": "2023,12,01,00:00",
      "title": "Some Archive Title"
    },
    ...
  ]
  ```

- `404 / 503` – from `_get_current_user`

---

### `POST /watchlist`

Add or update an archive/subarchive in the current user’s watchlist.

**Method:** `POST`  
**Path:** `/watchlist`  
**Request body:** `application/json`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Request JSON**

- `archive` (string, required)  
- `subarchive` (string, required)  
- `cutoff_date` (string, required)

**Behavior**

- Calls:

  ```python
  user.add_to_watchlist(
      archive=data["archive"],
      subarchive=data["subarchive"],
      cutoff_date=data["cutoff_date"]
  )
  ```

- Returns the **entire** current watchlist as a list (via `_format_watchlist`) with status `201`.

**Response**

- `201 Created`

  ```json
  [
    {
      "archive": "archive1",
      "subarchive": "sub1",
      "last_checked_date": "2024,01,01,12:00",
      "cutoff_date": "2023,12,01,00:00",
      "title": "Some Archive Title"
    },
    ...
  ]
  ```

- `404 / 503` – from `_get_current_user`

> There is no explicit validation of `archive`/`subarchive` in this route; invalid combinations may cause internal errors rather than a clean 4xx.

---

### `DELETE /watchlist/<archive>/<subarchive>`

Remove an item from the watchlist.

**Method:** `DELETE`  
**Path:** `/watchlist/<archive>/<subarchive>`  
**Response type:** empty or JSON error

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Behavior**

- Calls `user.remove_from_watchlist(archive, subarchive)`.

**Responses**

- `204 No Content` – item removed (empty body)  

- `404 Not Found` – item not present

  ```json
  { "error": "Entry not found" }
  ```

- `404 / 503` – from `_get_current_user`

---

### `GET /watchlist/<archive>/<subarchive>/check`

Check for new content or updates since the last watch, for a specific watchlist entry.

**Method:** `GET`  
**Path:** `/watchlist/<archive>/<subarchive>/check`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `tree` (optional flag) – if present, `tree=True`; otherwise `False`

**Behavior**

- Calls:

  ```python
  tree = request.args.get('tree') is not None
  result = user.check_archive(archive, subarchive, tree=tree)
  ```

- Returns:

  - `success`: boolean  
  - `unresolved`: result from `check_archive`  
  - `watchlist`: formatted current watchlist

**Responses**

- `200 OK`

  ```json
  {
    "success": true,
    "unresolved": [ ... ],
    "watchlist": [ ... ]
  }
  ```

- `404 Not Found` – if `user.check_archive` raises `KeyError`

  ```json
  { "error": "Watchlist item not found" }
  ```

- `404 / 503` – from `_get_current_user`

---

## 🔍 Resolve

### `GET /resolve` and variants

Resolve and update a watchlist item (and possibly its descendants) to its canonical form.

**Methods:** `GET`  
**Paths:**

- `/resolve`
- `/resolve/<archive>/<subarchive>`
- `/resolve/<archive>/<subarchive>/<fond>`
- `/resolve/<archive>/<subarchive>/<fond>/<opus>`
- `/resolve/<archive>/<subarchive>/<fond>/<opus>/<case>`

All of these route patterns map to the same function:

```python
resolve_update(archive=None, subarchive=None, fond=None, opus=None, case=None)
```

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `title` (string, optional) – currently read but **not used** in `resolve_update`  
- `tree` (flag, optional) – if present, `tree=True`; otherwise `False`  
- `deep` (flag, optional) – if present, `deep=True`; otherwise `False`

**Behavior**

- Logs the resolve request.  
- Calls:

  ```python
  result = user.resolve_item(
      archive, subarchive,
      fond=fond, opus=opus, case=case,
      tree=tree, deep=deep
  )
  ```

- Returns JSON with `success=True` and `unresolved=result` when successful.

**Responses**

- `200 OK`

  ```json
  {
    "success": true,
    "unresolved": [ ... ]
  }
  ```

- `404 Not Found`

  ```json
  { "error": "Watchlist item not found" }
  ```

  (For `KeyError`.)

  or

  ```json
  { "error": "No watcher found" }
  ```

  (For `FileNotFoundError`.)

- `500 Internal Server Error` – any other exception

  ```json
  { "error": "Exception during resolve" }
  ```

- `404 / 503` – from `_get_current_user`

---

## 🌍 Translation

### `GET /translate`

Trigger (optionally) and report status of translation jobs for the current user.

**Method:** `GET`  
**Path:** `/translate`  
**Response type:** `application/json`

> Note: The implementation defines only `GET /translate` with an optional `title` query parameter.

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `title` (string, optional) – If provided and the page exists, a new translation is started.

**Behavior**

1. Reads the optional `title`.  
2. If `title` is provided, attempts to look up the page via `runtime.lookup_by_title(title)`.  
3. If the page exists, calls `runtime.start_translation(page)` to start a translation job.  
4. Returns:

   - `enabled`: `runtime.translation_enabled`  
   - `available`: `runtime.translation_available`  
   - `translations`: a list of active translations from `_active_translations(user.email)`

   Each translation item has:

   - `title` – task name  
   - `progress` – number of completed units  
   - `total` – total units  

**Response**

- `200 OK`

  ```json
  {
    "enabled": true,
    "available": true,
    "translations": [
      {
        "title": "Archive:123/SomePage",
        "progress": 10,
        "total": 42
      }
    ]
  }
  ```

- `404 / 503` – from `_get_current_user`

> If `title` is invalid or the page cannot be found, no new translation is started, but the endpoint still returns `200` with the current translation status.

---

## 🧾 Logging & Service Views

### `GET /log`

Return the in-memory service log buffer as JSON.

**Method:** `GET`  
**Path:** `/log`  
**Response type:** `application/json`

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `limit` (integer, optional) – Maximum number of log entries to return

**Behavior**

- Calls `get_log_buffer().get_logs(limit)` and returns JSON.

**Response**

- `200 OK`

  ```json
  [ ... ]   // log entries, structure defined by log buffer
  ```

- `404 / 503` – from `_get_current_user`

---

### `GET /logs`

Render an HTML page showing logs.

**Method:** `GET`  
**Path:** `/logs`  
**Response type:** HTML

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Behavior**

- Renders `logs.html`.

**Response**

- `200 OK` – HTML page  
- `404 / 503` – from `_get_current_user`

---

### `GET /service_usage`

Render an HTML dashboard summarizing service usage over time.

**Method:** `GET`  
**Path:** `/service_usage`  
**Response type:** HTML

**Authentication**

- Currently does **not** enforce `_get_current_user`; the auth check is commented out, so this endpoint is effectively open.

**Query Parameters**

- `range` (string, optional, default `"1h"`) – Time window for analysis.
  - Supported values: `"5m"`, `"1h"`, `"4h"`, `"8h"`, `"24h"`, `"7d"`
- `by_resource` (string, optional) – If present and not `"0"`, groups summary “by resource”.

**Behavior**

1. Determines `delta` from `range` and computes time window `[now - delta, now]`.  
2. Loads logs via `ServiceLogger.get_logger().load_logs(...)`.  
3. If data is present, summarizes via `ServiceLogger.summarize_service_usage(...)`.  
4. Renders `service_usage.html` with:
   - `summary`  
   - `selected_range`  
   - `by_resource`  
   - `runtime_state` (`runtime.state`)

**Response**

- `200 OK` – HTML dashboard  

> Any exceptions would bubble up as 500s (not explicitly handled).

---

### `GET /good_dog`

Unpause the runtime (if paused) and report current run state.

**Method:** `GET`  
**Path:** `/good_dog`  
**Response type:** `application/json`

**Authentication**

- Session checks via `_get_current_user` are currently commented out; endpoint is open.

**Behavior**

- If `runtime.state != "running"`, calls `runtime.unpause()`.  
- Returns JSON with `runstate`.

**Response**

- `200 OK`

  ```json
  { "success": true, "runstate": "running" }
  ```

---

### `GET /bad_dog`

Pause the runtime (if running) and report current run state.

**Method:** `GET`  
**Path:** `/bad_dog`  
**Response type:** `application/json`

**Authentication**

- Session checks via `_get_current_user` are currently commented out; endpoint is open.

**Behavior**

- If `runtime.state != "paused"`, calls `runtime.pause()`.  
- Returns JSON with `runstate`.

**Response**

- `200 OK`

  ```json
  { "success": true, "runstate": "paused" }
  ```

---

## 📊 App Metrics

### `GET /metrics`

Render an HTML dashboard of application metrics (request counts, durations, user histograms).

**Method:** `GET`  
**Path:** `/metrics`  
**Response type:** HTML

**Authentication**

- Uses `_get_current_user` (404/503 on failure).

**Query Parameters**

- `range` (string, optional, default `"24h"`)  
  - Supported values: `"24h"`, `"7d"`, `"30d"`

**Behavior**

1. Computes `[now - delta, now]` window from `range`.  
2. Loads event logs via `_event_logger.load_logs(...)`.  
3. Computes:
   - `user_histogram(df).to_dict()`  
   - `summarize_duration_by_path_group(df)` (rounded to 4 decimals, converted to list of dicts)  
4. Renders `metrics.html` with:
   - `user_histogram`  
   - `duration_summary`  
   - `selected_range`

**Response**

- `200 OK` – HTML metrics dashboard  
- `404 / 503` – from `_get_current_user`
