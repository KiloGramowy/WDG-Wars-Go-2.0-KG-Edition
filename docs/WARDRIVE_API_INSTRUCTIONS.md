# Wardrive API Integration — Instructions for Wardrive Server Instance

> Inherited upstream integration/reference notes. For this fork’s current receipt handling, GPS provenance and verified limits, see the [feature guide](FEATURE_GUIDE.md) and [validation record](VALIDATION.md). This document alone does not certify the behavior of an independently hosted server.

## Context

The ESP32 Watch Dogs game can upload wardriving data to a self-hosted wardrive server.
The server imports data via REST API with HMAC-signed payloads.

## Server Setup

The wardrive server is a Django app with PostgreSQL/SQLite.
See the wardrive repository for deployment instructions.

## What needs to be added

### 1. API Key model

Add to `maps/models.py`:
```python
class APIKey(models.Model):
    key_hash = models.CharField(max_length=128, unique=True)
    key_prefix = models.CharField(max_length=8, db_index=True)
    name = models.CharField(max_length=64)  # e.g. "uConsole-home"
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)
```

### 2. Upload endpoint (signed + encrypted)

Add to `maps/views.py`:
```
POST /api/upload/
Header: X-API-Key: <64-char-hex-key>
Content-Type: application/json
```

**Wire format (envelope):**
```json
{
    "data": "<base64 encoded JSON payload>",
    "nonce": "<16 hex chars, random per request>",
    "sig": "<HMAC-SHA256 hex of (nonce + data) using API key as HMAC key>"
}
```

**Server-side verification:**
```python
import hmac, hashlib, base64, json

def verify_upload(request):
    api_key = request.headers.get("X-API-Key")
    body = json.loads(request.body)
    data_b64 = body["data"]
    nonce = body["nonce"]
    sig = body["sig"]

    # 1. Verify API key exists in DB (bcrypt check)
    # 2. Verify HMAC signature
    expected_sig = hmac.new(
        api_key.encode("utf-8"),
        (nonce + data_b64).encode("utf-8"),
        hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return JsonResponse({"error": "Invalid signature"}, status=403)

    # 3. Decode payload
    payload = json.loads(base64.b64decode(data_b64))
    networks = payload["networks"]
    # ... process networks ...
```

**Decoded payload:**
```json
{
    "networks": [
        {
            "bssid": "AA:BB:CC:DD:EE:FF",
            "ssid": "NetworkName",
            "auth": "WPA2",
            "channel": 6,
            "rssi": -45,
            "lat": 52.2297700,
            "lon": 21.0117800,
            "alt": 120.0,
            "type": "WIFI",
            "first_seen": "2026-03-28 14:30:00"
        }
    ]
}
```

**Responses:**
```
200: {"ok": true, "imported": 150, "duplicates": 30, "total": 180}
401: {"error": "Invalid API key"}
403: {"error": "Invalid signature"}
400: {"error": "Invalid data format"}
```

**Security properties:**
- HTTPS (TLS) encrypts in transit
- HMAC-SHA256 prevents payload tampering (even if TLS is MITM'd)
- Nonce prevents replay attacks (server should reject reused nonces within 5min window)
- API key never appears in payload body (only in header + as HMAC key)

### 3. Auth logic

- Key is 64-char hex (256 bits). Game stores it locally.
- Server stores bcrypt hash + 8-char prefix for lookup.
- Lookup: filter by prefix, then bcrypt verify.
- Update `last_used` on successful auth.
- **Nonce replay protection**: store nonces for 5 minutes, reject duplicates.

### 4. Import logic

Same as `process_csv.py` management command but for JSON input:
- For each network in the POST body:
  - Skip if lat=0, lon=0 (no GPS fix)
  - Deduplicate by BSSID: if exists with weaker RSSI, update; if stronger, skip
  - Create AccessPoint object with all fields
- Return count of imported vs duplicates

### 5. Management command to generate API key

```
python manage.py create_api_key --name "uConsole-home"
```
Outputs: `API Key: a1b2c3d4...` (64 hex chars, shown once)

### 6. Rate limiting

- Max 10 requests per minute per API key
- Max 50,000 networks per request

### 7. CORS (not needed)

Game sends from Python requests, not browser. No CORS needed.

## Data format the game sends

The game's wardriving.csv is WiGLE format:
```
MAC,SSID,AuthMode,FirstSeen,Channel,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type
AA:BB:CC:DD:EE:FF,MyNetwork,[WPA2-PSK],2026-03-28 14:30:00,6,-45,52.2297700,21.0117800,120.0,5.0,WIFI
```

The game plugin converts this to JSON before sending. Auth mode mapping:
- `[WPA2-PSK]` → `WPA2`
- `[WPA-PSK]` → `WPA`
- `[WPA3-SAE]` → `WPA3`
- `[ESS]` or `[OPEN]` → `OPEN`
- `[WEP]` → `WEP`

## Existing API endpoints (for reference)

```
GET /api/points/   → all access points as JSON
GET /api/stats/    → {total_ap, total_files, last_processed}
```

Both require session login. The new upload endpoint uses API key auth instead.

## How to test

After implementing, the game will send:
```bash
curl -X POST https://your-wardrive-server.example/api/upload/ \
  -H "X-API-Key: <key>" \
  -H "Content-Type: application/json" \
  -d '{"networks":[{"bssid":"AA:BB:CC:DD:EE:FF","ssid":"Test","auth":"WPA2","channel":6,"rssi":-45,"lat":52.23,"lon":21.01,"alt":0,"type":"WIFI","first_seen":"2026-03-28 14:00:00"}]}'
```

Expected: `{"ok":true,"imported":1,"duplicates":0,"total":1}`
