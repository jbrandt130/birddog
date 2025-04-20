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
- `401` : Invalid login credentials.

#### `GET /logout`
End the current user session.

#### `POST /change_password`
Change the password of the currently logged-in user.

**Parameters:**
- `email` : User email
- `current` : Current user password
- `new`: New user password

**Errors**
- `403` : Invalid current password
- `404` : Unknown user

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

#### `GET /page/<archive>`
#### `GET /page/<archive>/<subarchive>`
#### `GET /page/<archive>/<subarchive>/<fond>`
#### `GET /page/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /page/<archive>/<subarchive>/<fond>/<opus>/<case>`
Return the specified page data structure.

**Query Parameters:**
- `compare` (optional): Modification date string used to compare the current version against a previous one. Format: `YYYY,MM,DD,hh:mm`

---

### 📄 Download

#### `GET /download/<archive>`
#### `GET /download/<archive>/<subarchive>`
#### `GET /download/<archive>/<subarchive>/<fond>`
#### `GET /download/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /download/<archive>/<subarchive>/<fond>/<opus>/<case>`
Download an `.xlsx` export of the document hierarchy.

**Query Parameters:**
- `compare` (optional): Modification date string used to generate a diff or highlight changes. Format: `YYYY,MM,DD,hh:mm`

---

### 👁️ Watchlist

#### `GET /watchlist`
Get the current user's watchlist.

#### `POST /watchlist`
Add an archive/subarchive to the watchlist.

#### `DELETE /watchlist/<archive>/<subarchive>`
Remove an item from the watchlist.

#### `GET /watchlist/<archive>/<subarchive>/check`
Check for new content or updates since last watch.

---

### 🔍 Resolve

#### `GET /resolve/<archive>/<subarchive>`
#### `GET /resolve/<archive>/<subarchive>/<fond>`
#### `GET /resolve/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /resolve/<archive>/<subarchive>/<fond>/<opus>/<case>`
Resolve an incomplete or partial document reference to its canonical form.

---

### 🌍 Translation

#### `GET /translate`
#### `GET /translate/<archive>/<subarchive>`
#### `GET /translate/<archive>/<subarchive>/<fond>`
#### `GET /translate/<archive>/<subarchive>/<fond>/<opus>`
#### `GET /translate/<archive>/<subarchive>/<fond>/<opus>/<case>`
Trigger or check progress of a translation job.

---

### 🧾 Logging

#### `GET /log`
Return the internal service logs (for debugging/monitoring).
