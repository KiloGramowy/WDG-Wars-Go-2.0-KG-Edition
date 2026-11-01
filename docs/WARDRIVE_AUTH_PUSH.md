# Wardrive Auth Push — Game as 2FA Authenticator

> Inherited upstream integration/reference notes. For this fork’s current receipt handling, GPS provenance and verified limits, see the [feature guide](FEATURE_GUIDE.md) and [validation record](VALIDATION.md). This document alone does not certify the behavior of an independently hosted server.

## Concept

Instead of username + password, the wardrive website uses the game as an authenticator.
User clicks "Login" on web → server sends challenge → game shows PIN → user enters PIN on web → logged in.

## Flow

```
[Browser]                    [Server]                    [Game on uConsole]
    |                            |                            |
    |  1. POST /auth/request     |                            |
    |  {api_key_prefix: "9cd1"}  |                            |
    |--------------------------->|                            |
    |                            |  2. Generate PIN (6 digits)|
    |                            |     + token (32 hex)       |
    |                            |     Store: pending_auth    |
    |                            |     {token, pin, api_key,  |
    |                            |      expires: now+60s}     |
    |  3. Return token           |                            |
    |<---------------------------|                            |
    |  {token: "abc123..."}      |                            |
    |                            |                            |
    |  4. Show "Waiting for      |                            |
    |     PIN from device..."    |                            |
    |     (poll /auth/check)     |                            |
    |                            |  5. Game polls             |
    |                            |<---------------------------|
    |                            |  GET /api/auth/pending/    |
    |                            |  Header: X-API-Key: full   |
    |                            |                            |
    |                            |  6. Return PIN to game     |
    |                            |--------------------------->|
    |                            |  {pending:true, pin:847291,|
    |                            |   token:"abc123",          |
    |                            |   expires:60}              |
    |                            |                            |
    |                            |  7. Game displays PIN      |
    |                            |     popup on screen        |
    |                            |     (big digits, 60s timer)|
    |                            |                            |
    |  8. User reads PIN from    |                            |
    |     game screen, types     |                            |
    |     on web form            |                            |
    |                            |                            |
    |  9. POST /auth/verify      |                            |
    |  {token: "abc123",         |                            |
    |   pin: "847291"}           |                            |
    |--------------------------->|                            |
    |                            |  10. Verify PIN + token    |
    |                            |      Delete pending_auth   |
    |                            |      Create session        |
    |  11. Set session cookie    |                            |
    |<---------------------------|                            |
    |  {ok: true, redirect: "/"}|                            |
```

## Server Implementation

### New model: PendingAuth

```python
# maps/models.py
class PendingAuth(models.Model):
    token = models.CharField(max_length=64, unique=True, db_index=True)
    pin = models.CharField(max_length=6)
    api_key_hash = models.CharField(max_length=128)  # link to APIKey
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    attempts = models.IntegerField(default=0)  # brute-force protection
```

### New endpoints

#### 1. Request login (browser → server)

```
POST /auth/request/
Content-Type: application/json
Body: {"api_key_prefix": "abcd1234"}  (first 8 chars of API key)

Response 200: {"token": "<32 hex chars>", "expires": 60}
Response 404: {"error": "No device with this key prefix"}
```

Server:
1. Find APIKey by prefix
2. Generate 6-digit PIN (random, 100000-999999)
3. Generate 32-char hex token
4. Store PendingAuth(token, pin, api_key, expires=now+60s)
5. Return token to browser

#### 2. Poll for pending auth (game → server)

```
GET /api/auth/pending/
Header: X-API-Key: <full 64-char key>

Response 200 (has pending): {"pending": true, "pin": "847291", "token": "abc123...", "expires": 55}
Response 200 (no pending):  {"pending": false}
Response 401: {"error": "Invalid API key"}
```

Server:
1. Verify API key (bcrypt)
2. Find PendingAuth where api_key matches AND not expired AND not used
3. If found: return pin + token + remaining seconds
4. If not: return {pending: false}

#### 3. Verify PIN (browser → server)

```
POST /auth/verify/
Content-Type: application/json
Body: {"token": "abc123...", "pin": "847291"}

Response 200: {"ok": true, "redirect": "/"}  + Set-Cookie session
Response 401: {"error": "Invalid PIN"}
Response 410: {"error": "Token expired"}
Response 429: {"error": "Too many attempts"}
```

Server:
1. Find PendingAuth by token
2. Check not expired, not used, attempts < 3
3. Increment attempts
4. If PIN matches:
   - Mark used=True
   - Find user via api_key_hash
   - Create Django session, set cookie
   - Return success + redirect
5. If PIN wrong: return error (don't delete — allow retries up to 3)

### Login page changes

Replace username/password form with:

```html
<h2>Login with Watch Dogs</h2>
<div id="step1">
  <p>Enter your API key prefix (first 8 characters):</p>
  <input type="text" id="prefix" maxlength="8" placeholder="abcd1234">
  <button onclick="requestLogin()">Send PIN to device</button>
</div>

<div id="step2" style="display:none">
  <p>Check your game — a 6-digit PIN is displayed.</p>
  <p>Enter it here:</p>
  <input type="text" id="pin" maxlength="6" placeholder="000000">
  <button onclick="verifyPin()">Login</button>
  <p id="timer">Expires in 60s</p>
</div>

<script>
let token = null;
let countdown = null;

async function requestLogin() {
    const prefix = document.getElementById('prefix').value;
    const resp = await fetch('/auth/request/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({api_key_prefix: prefix})
    });
    const data = await resp.json();
    if (data.token) {
        token = data.token;
        document.getElementById('step1').style.display = 'none';
        document.getElementById('step2').style.display = 'block';
        startCountdown(data.expires);
    }
}

async function verifyPin() {
    const pin = document.getElementById('pin').value;
    const resp = await fetch('/auth/verify/', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({token: token, pin: pin})
    });
    const data = await resp.json();
    if (data.ok) {
        window.location.href = data.redirect || '/';
    } else {
        alert(data.error);
    }
}

function startCountdown(seconds) {
    let remaining = seconds;
    const el = document.getElementById('timer');
    countdown = setInterval(() => {
        remaining--;
        el.textContent = `Expires in ${remaining}s`;
        if (remaining <= 0) {
            clearInterval(countdown);
            el.textContent = 'Expired — try again';
        }
    }, 1000);
}
</script>
```

## Security Properties

| Property | How |
|----------|-----|
| No passwords stored | Auth is via API key (device possession) |
| PIN brute-force protection | Max 3 attempts per token |
| Replay protection | Token single-use, expires in 60s |
| MITM protection | HTTPS + PIN never sent over network (displayed on local screen) |
| Phishing protection | PIN is generated server-side, shown on YOUR device |
| Session hijacking | Standard Django session with CSRF token |

## Game-side (already implemented)

The wardrive plugin polls `GET /api/auth/pending/` every 5 seconds using the full API key.
When a pending auth is found:
- Large PIN popup appears on game screen (on top of everything)
- Blinking border, 60-second countdown timer
- PIN also shown as HUD message
- After expiry, popup auto-dismisses

## Cleanup cron

Add to server: delete PendingAuth where expires_at < now (run every 5 minutes):

```python
# management/commands/cleanup_auth.py
from django.utils import timezone
from maps.models import PendingAuth

PendingAuth.objects.filter(expires_at__lt=timezone.now()).delete()
```

Or simpler: filter by expires_at in queries, never return expired entries.
