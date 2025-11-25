## 📘 Birddog Web Service API Reference

This document describes the HTTP endpoints exposed by the Birddog web service, which supports archive navigation, translation, download, and user session management.

---

### 🔐 Authentication

#### `POST /signup`
Create a new user account.

**Parameters:**
- `name` : User name
- `email` : User email
- `password` : User password

**Errors**
- `400` : Email already exists.

#### `POST /login`
Authenticate a user and start a session.

**Parameters:**
- `email` : User email
- `password` : User password

**Errors**
- `401` : Invalid email or password.

#### `GET /logout`
End the current user session.

#### `POST /change_password`
Change the password of the currently logged-in user.

**Parameters:**
- `email` : User email
- `current` : Current user password
- `new`: New user password

**Errors**
- `401` : Not logged in
- `403` : Current password is incorrect
- `404` : User not found
- `503` : Service unavailable

#### `POST /reset_password`
Request a password reset email. If the email is recognized, a reset token will be sent.

**Parameters:**
- `email` : User email

#### `GET|POST /reset_password/<token>`
Reset a password if the provided token is recognized.

**Parameters:**
- `password` : New password

---

### 📁 Archive Navigation

#### `GET /archives`
Return the list of available top-level archives.

**Errors**
- `404` : User session is invalid / not logged in
- `503` : Service unavailable
 
#### `GET /page`

Return the page data structure for the archive page with the given title.

**Query Parameters:**
- `title` : Page title
- `compare` (optional): Modification date string used to compare the current version against a previous one. Format: `YYYY,MM,DD,hh:mm`

**Errors**
- `400` : Missing required parameter: "title"
- `404` : Page not found, or user session is invalid / not logged in
- `503` : Service unavailable

---

#### `GET /export`
Download a JSON export of the document hierarchy.

**Query Parameters:**
- `title` : Page title

**Errors**
- `400` : Missing required parameter "title"
- `404` : Page not found, or user session is invalid / not logged in
- `503` : Service unavailable

---

### 📄 Download

#### `POST /download`
Download an `.xlsx` export of the document hierarchy.

**Query Parameters:**
- `compare` (optional): Modification date string used to generate a diff or highlight changes. Format: `YYYY,MM,DD,hh:mm`
- `title` : Page title
- `template` : 
- `table` : 
- `column_map` : 

**Errors**
- `404` : Page not found, or user session is invalid / not logged in
- `500` : Internal server error
- `503` : Service unavailable

---

### 👁️ Watchlist

#### `GET /watchlist`
Get the current user's watchlist.

**Errors**
- `404` : Not found
- `503` : Service unavailable

#### `POST /watchlist`
Add an archive/subarchive to the watchlist.

**Query Parameters:**
- `archive` : 
- `subarchive` : 
- `cutoff_date` : 

**Errors**
- `404` : Page not found
- `503` : Service unavailable

#### `DELETE /watchlist/<archive>/<subarchive>`
Remove an item from the watchlist.

**Errors**
- `404` : Entry not found
- `503` : Service unavailable

#### `GET /watchlist/<archive>/<subarchive>/check`
Check for new content or updates since last watch.

**Query Parameters:**
- `tree` : 

**Errors**
- `404` : Watchlist item not found
- `503` : Service unavailable

---

### 🔍 Resolve

#### `GET /resolve`
#### `GET /resolve/<archive>/<subarchive>`
#### `GET /resolve/<archive>/<subarchive>/<fond>`
#### `GET /resolve/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /resolve/<archive>/<subarchive>/<fond>/<opus>/<case>`
Resolve an incomplete or partial document reference to its canonical form.

**Query Parameters:**
- `title` : Page title 
- `tree` : 
- `deep` : 

**Errors**
- `404` : Watchlist item not found
- `500` : Exception during resolve
- `503` : Service unavailable

---

### 🌍 Translation

#### `GET /translate`
#### `GET /translate/<archive>/<subarchive>`
#### `GET /translate/<archive>/<subarchive>/<fond>`
#### `GET /translate/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /translate/<archive>/<subarchive>/<fond>/<opus>/<case>`
Trigger or check progress of a translation job.

**Query Parameters:**
- `title` : Page title 

**Errors**
- `404` : Not found
- `503` : Service unavailable

---

### 🧾 Logging

#### `GET /log`
Return the internal service logs (for debugging/monitoring).

**Errors**
- `404` : Not found
- `503` : Service unavailable

#### `GET /logs`

**Errors**
- `404` : Not found
- `503` : Service unavailable

#### `GET /service_usage`

**Query Parameters:**
- `range` : 
- `by_resource` : 

**Errors**
- `404` : Not found
- `503` : Service unavailable

#### `GET /good_dog`

#### `GET /bad_dog`

### 🧾 APP METRICS

#### `GET /metrics`

**Query Parameters:**
- `range` : 

**Errors**
- `404` : Not found
- `503` : Service unavailable

