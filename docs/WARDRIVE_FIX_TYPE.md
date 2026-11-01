# Wardrive — Fix WiFi vs BLE classification

> Inherited upstream integration/reference notes. For this fork’s current receipt handling, GPS provenance and verified limits, see the [feature guide](FEATURE_GUIDE.md) and [validation record](VALIDATION.md). This document alone does not certify the behavior of an independently hosted server.

## Problem

The game uploads networks with a `type` field (`WIFI` or `BLE`) but the server ignores it — all entries are treated as WiFi access points. BLE devices should be stored and displayed differently.

## What the game sends

POST `/api/upload/` payload (after HMAC decode):

```json
{
    "networks": [
        {
            "bssid": "5C:7B:5C:24:A9:B9",
            "ssid": "MyNetwork",
            "auth": "WPA2",
            "channel": 6,
            "rssi": -45,
            "lat": 52.2297700,
            "lon": 21.0117800,
            "alt": 120.0,
            "type": "WIFI",           <-- THIS FIELD
            "first_seen": "2026-03-28 14:30:00"
        },
        {
            "bssid": "04:2B:6F:BE:93:02",
            "ssid": "",
            "auth": "BLE",
            "channel": 0,
            "rssi": -68,
            "lat": 52.2298100,
            "lon": 21.0118200,
            "alt": 120.0,
            "type": "BLE",            <-- THIS FIELD
            "first_seen": "2026-03-28 14:31:00"
        }
    ]
}
```

The `type` field is always present: `"WIFI"` or `"BLE"`.

## What needs to change on the server

### 1. Model — add `type` field to AccessPoint

```python
# maps/models.py
class AccessPoint(models.Model):
    # existing fields...
    mac = models.CharField(...)
    ssid = models.CharField(...)
    auth_mode = models.CharField(...)
    # ADD:
    device_type = models.CharField(max_length=8, default="WIFI", db_index=True)
    # Values: "WIFI" or "BLE"
```

Migration: `python manage.py makemigrations && python manage.py migrate`

### 2. Import logic — save the type

In the `api_upload` view (or wherever networks are processed):

```python
for net in networks:
    device_type = net.get("type", "WIFI")  # default WIFI for old data
    # ... existing dedup/create logic ...
    ap.device_type = device_type
    ap.save()
```

Also update `process_csv.py` management command — the CSV has `Type` as the last column (index 10):

```python
# In process_csv.py, when parsing each row:
device_type = row[10] if len(row) > 10 else "WIFI"
```

### 3. API — include type in response

`GET /api/points/` should include the type:

```python
# In views.py api_points:
data = [{
    "mac": ap.mac,
    "ssid": ap.ssid,
    "auth": ap.auth_mode,
    "ch": ap.channel,
    "rssi": ap.rssi,
    "type": ap.device_type,    # ADD THIS
    "lat": ap.latitude,
    "lng": ap.longitude,
}, ...]
```

### 4. Map display — different icons/colors for WiFi vs BLE

On the Leaflet map (`map.html`):

```javascript
function getMarkerColor(point) {
    if (point.type === "BLE") return "#29ADFF";  // blue
    // WiFi by auth:
    if (point.auth === "OPEN") return "#FF004D";  // red (insecure)
    if (point.auth === "WEP") return "#FFA300";   // orange (weak)
    return "#00E436";  // green (WPA/WPA2)
}

function getMarkerIcon(point) {
    if (point.type === "BLE") return "📶";  // or a BLE icon
    return "📡";  // WiFi icon
}
```

### 5. Stats — separate counts

`GET /api/stats/` should return:

```json
{
    "total_ap": 34627,
    "total_wifi": 30500,
    "total_ble": 4127,
    "total_files": 66,
    "last_processed": "2026-03-30T14:30:00"
}
```

### 6. Filter on map

Add a toggle on the map page:

```html
<label><input type="checkbox" id="show_wifi" checked> WiFi</label>
<label><input type="checkbox" id="show_ble" checked> BLE</label>
```

Filter markers when rendering clusters.

## Existing data

Run a one-time migration to classify existing data:

```python
# In Django shell or management command:
from maps.models import AccessPoint

# BLE entries typically have:
# - No SSID or SSID = MAC-like string
# - auth_mode = "BLE" or empty
# - channel = 0
# But safest: re-import from CSV files which have the Type column

# Or: mark all existing as WIFI (they mostly are),
# future uploads will have correct type
AccessPoint.objects.filter(device_type="").update(device_type="WIFI")
```

## Summary

| What | Change |
|------|--------|
| Model | Add `device_type` CharField (WIFI/BLE) |
| API upload | Read `type` from each network, save to model |
| CSV import | Read column 10 (Type) from WiGLE CSV |
| API points | Include `type` in JSON response |
| API stats | Separate WiFi/BLE counts |
| Map | Different colors for WiFi (green) vs BLE (blue) |
| Map | Toggle filter WiFi/BLE |
