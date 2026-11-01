# Wardrive — ADS-B Aircraft & MeshCore Nodes on Map

> Inherited upstream integration/reference notes. For this fork’s current receipt handling, GPS provenance and verified limits, see the [feature guide](FEATURE_GUIDE.md) and [validation record](VALIDATION.md). This document alone does not certify the behavior of an independently hosted server.

## New data types in upload payload

The game plugin now sends 3 data types in one request:

```json
{
    "networks": [...],           // existing WiFi + BLE
    "aircraft": [...],           // NEW: ADS-B tracked aircraft
    "meshcore_nodes": [...]      // NEW: LoRa mesh nodes
}
```

`networks` stays the same. New arrays may be empty `[]` if no data.

## Aircraft format

```json
{
    "aircraft": [
        {
            "icao": "471F6E",
            "callsign": "LOT523",
            "lat": 50.686500,
            "lon": 18.120500,
            "alt_ft": 18300,
            "speed_kt": 354,
            "heading": 98,
            "first_seen": "1774857678",
            "type": "ADSB"
        }
    ]
}
```

Fields:
- `icao`: 6-char hex, unique aircraft transponder ID (like MAC for planes)
- `callsign`: flight number (may be empty)
- `lat/lon`: position when first tracked
- `alt_ft`: altitude in feet
- `speed_kt`: ground speed in knots
- `heading`: degrees (0=north, 90=east)
- `first_seen`: Unix timestamp
- `type`: always `"ADSB"`

## MeshCore nodes format

```json
{
    "meshcore_nodes": [
        {
            "node_id": "48fab00f",
            "node_type": "Client",
            "name": "WatchDogs_a3f1",
            "lat": 50.670000,
            "lon": 17.905000,
            "rssi": -48.0,
            "first_seen": "2026-03-17T21:36:55",
            "type": "MESHCORE"
        }
    ]
}
```

Fields:
- `node_id`: 8-char hex, unique node identity
- `node_type`: `Client`, `Repeater`, `Room`, `Sensor`
- `name`: node display name
- `lat/lon`: GPS position (only nodes with valid GPS, -90..90, -180..180)
- `rssi`: signal strength when discovered
- `first_seen`: ISO timestamp
- `type`: always `"MESHCORE"`

## Server implementation

### 1. New models

```python
# maps/models.py

class TrackedAircraft(models.Model):
    icao = models.CharField(max_length=6, db_index=True)
    callsign = models.CharField(max_length=16, blank=True, default="")
    latitude = models.FloatField()
    longitude = models.FloatField()
    altitude_ft = models.IntegerField(default=0)
    speed_kt = models.IntegerField(default=0)
    heading = models.IntegerField(default=0)
    first_seen = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("icao",)  # one entry per aircraft, update if seen again


class MeshCoreNode(models.Model):
    node_id = models.CharField(max_length=8, db_index=True)
    node_type = models.CharField(max_length=16, default="Client")
    name = models.CharField(max_length=64, blank=True, default="")
    latitude = models.FloatField()
    longitude = models.FloatField()
    rssi = models.FloatField(default=0)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ("node_id",)
```

### 2. Update upload endpoint

In `api_upload` view, after processing `networks`:

```python
# Aircraft
aircraft_list = payload.get("aircraft", [])
imported_ac = 0
for ac in aircraft_list:
    icao = ac.get("icao", "")
    lat = ac.get("lat", 0)
    lon = ac.get("lon", 0)
    if not icao or lat == 0:
        continue
    obj, created = TrackedAircraft.objects.update_or_create(
        icao=icao,
        defaults={
            "callsign": ac.get("callsign", ""),
            "latitude": lat,
            "longitude": lon,
            "altitude_ft": ac.get("alt_ft", 0),
            "speed_kt": ac.get("speed_kt", 0),
            "heading": ac.get("heading", 0),
        }
    )
    if created:
        imported_ac += 1

# MeshCore nodes
mc_list = payload.get("meshcore_nodes", [])
imported_mc = 0
for node in mc_list:
    nid = node.get("node_id", "")
    lat = node.get("lat", 0)
    lon = node.get("lon", 0)
    if not nid or lat == 0:
        continue
    obj, created = MeshCoreNode.objects.update_or_create(
        node_id=nid,
        defaults={
            "node_type": node.get("node_type", "Client"),
            "name": node.get("name", ""),
            "latitude": lat,
            "longitude": lon,
            "rssi": node.get("rssi", 0),
        }
    )
    if created:
        imported_mc += 1
```

Add to response:
```python
return JsonResponse({
    "ok": True,
    "imported": imported_nets,
    "updated": updated_nets,
    "duplicates": dup_nets,
    "aircraft_imported": imported_ac,
    "meshcore_imported": imported_mc,
    "total": AccessPoint.objects.count(),
})
```

### 3. API endpoints for map data

```python
# maps/urls.py — add:
path("api/aircraft/", views.api_aircraft),
path("api/meshcore/", views.api_meshcore),
```

```python
# maps/views.py
@login_required
def api_aircraft(request):
    data = list(TrackedAircraft.objects.values(
        "icao", "callsign", "latitude", "longitude",
        "altitude_ft", "speed_kt", "heading"))
    return JsonResponse(data, safe=False)

@login_required
def api_meshcore(request):
    data = list(MeshCoreNode.objects.values(
        "node_id", "node_type", "name",
        "latitude", "longitude", "rssi"))
    return JsonResponse(data, safe=False)
```

### 4. Map display — icons and colors

On the Leaflet map, add layers for each data type:

#### Color scheme

| Type | Circle fill | Circle border | Icon | Description |
|------|------------|---------------|------|-------------|
| WiFi (WPA2/WPA3) | `#00E436` (green) | `#00A825` | none (filled dot) | Secure WiFi |
| WiFi (WEP) | `#FFA300` (orange) | `#CC8200` | none (filled dot) | Weak encryption |
| WiFi (Open) | `#FF004D` (red) | `#CC003D` | none (filled dot) | No encryption |
| BLE | `#29ADFF` (blue) | `#1E82CC` | none (filled dot) | Bluetooth LE |
| ADS-B Aircraft | `#00E5FF` (cyan) | `#00B8CC` | ✈ (plane emoji) | Tracked aircraft |
| MeshCore Node | `#1B3A5C` (dark blue) | `#0D1F33` | ◆ (diamond) | LoRa mesh node |

#### Leaflet marker implementation

```javascript
// For WiFi/BLE — filled circle (existing):
L.circleMarker([lat, lng], {
    radius: 5,
    fillColor: color,
    fillOpacity: 0.8,
    color: borderColor,
    weight: 1
});

// For ADS-B — circle with plane icon inside:
L.marker([lat, lng], {
    icon: L.divIcon({
        className: 'adsb-marker',
        html: '<div class="marker-ring adsb"><span>✈</span></div>',
        iconSize: [20, 20],
        iconAnchor: [10, 10]
    })
});

// For MeshCore — circle with diamond icon inside:
L.marker([lat, lng], {
    icon: L.divIcon({
        className: 'mesh-marker',
        html: '<div class="marker-ring mesh"><span>◆</span></div>',
        iconSize: [18, 18],
        iconAnchor: [9, 9]
    })
});
```

#### CSS for marker icons

```css
.marker-ring {
    width: 100%;
    height: 100%;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 10px;
    border: 2px solid;
}

.marker-ring.adsb {
    background: rgba(0, 229, 255, 0.15);
    border-color: #00E5FF;
    color: #00E5FF;
    font-size: 12px;
}

.marker-ring.mesh {
    background: rgba(27, 58, 92, 0.3);
    border-color: #4488BB;
    color: #88BBEE;
    font-size: 9px;
}

/* Popup styling */
.adsb-popup {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
}
.adsb-popup .callsign { color: #00E5FF; font-weight: bold; }
.adsb-popup .altitude { color: #FFA300; }

.mesh-popup {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
}
.mesh-popup .node-name { color: #88BBEE; font-weight: bold; }
.mesh-popup .node-type { color: #4488BB; }
```

#### Popups on click

**ADS-B Aircraft:**
```html
<div class="adsb-popup">
    <div class="callsign">✈ LOT523</div>
    <div>ICAO: 471F6E</div>
    <div class="altitude">Alt: 18,300 ft</div>
    <div>Speed: 354 kt | Heading: 98°</div>
</div>
```

**MeshCore Node:**
```html
<div class="mesh-popup">
    <div class="node-name">◆ WatchDogs_a3f1</div>
    <div class="node-type">Type: Client</div>
    <div>RSSI: -48 dBm</div>
    <div>ID: 48fab00f</div>
</div>
```

### 5. Layer controls

Add layer toggle to map:

```javascript
var wifiLayer = L.markerClusterGroup({...});
var bleLayer = L.markerClusterGroup({...});
var adsbLayer = L.layerGroup();      // no clustering for aircraft
var meshLayer = L.layerGroup();      // no clustering for mesh

L.control.layers(null, {
    "📡 WiFi": wifiLayer,
    "📶 BLE": bleLayer,
    "✈ ADS-B": adsbLayer,
    "◆ MeshCore": meshLayer
}).addTo(map);
```

Aircraft and MeshCore don't need clustering (few points, want each visible).

### 6. Updated stats

```json
{
    "total_ap": 34627,
    "total_wifi": 30500,
    "total_ble": 4127,
    "total_aircraft": 15,
    "total_meshcore": 8,
    "total_files": 66
}
```

Show in top bar: `📡 30,500 WiFi | 📶 4,127 BLE | ✈ 15 Aircraft | ◆ 8 Mesh Nodes`
