"""KG-owned JanOS SOLO packages and fail-closed application-only preflight.

Metadata/download functions never open a device. USB discovery and ROM reads
are separate, explicit functions used only by the existing manual installer.
"""
from dataclasses import dataclass, field
import binascii
import hashlib
import json
from pathlib import Path
import re
import stat
import struct
import time
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .esp32_flash import FlashError


REPOSITORY_ID = 1442123109
REPOSITORY = "KiloGramowy/WDG-Wars-Go-2.0-KG-Edition"
REPO_URL = f"https://api.github.com/repositories/{REPOSITORY_ID}"
API_PREFIX = f"https://api.github.com/repos/{REPOSITORY}"
LATEST_URL = API_PREFIX + "/releases/latest"
MANIFEST_NAME = "janos-solo-manifest.json"
LEGACY_MANIFEST_SHA256 = "7a2332f0bb8325e4984e458dcd78e8443d4d39fdfb2b6b1db24223679f3f9741"
LEGACY_IMAGE_SHA256 = "e3dff6b534ff8d7712d37e181a3d2d1e30d3672877b6925be2a4bf27c8a989c5"
APP_OFFSET = 0x20000
APP_BYTES = 0x3F0000
FLASH_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}\Z")
_VERSION = re.compile(r"(?:v)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
_TARGET = {
    "board": "Seeed XIAO ESP32-C5", "chip": "ESP32-C5",
    "revision_tested": "v1.0", "flash_bytes": FLASH_BYTES,
    "flash_mode": "DIO", "flash_frequency_mhz": 80,
    "tested_write_offset": "0x20000", "tested_partition": "ota_0",
    "app_partition_bytes": APP_BYTES,
}


class KGFirmwareError(FlashError):
    pass


@dataclass(frozen=True)
class KGPackage:
    version: str
    release_tag: str
    image_name: str
    image_bytes: int
    image_sha256: str
    image_url: str
    manifest_sha256: str


@dataclass(frozen=True)
class ImageInfo:
    chip_id: int
    min_revision: int
    max_revision: int


@dataclass(frozen=True)
class USBIdentity:
    vid: int
    pid: int
    serial_number: str = field(repr=False)
    location: str = field(repr=False)


def version_tuple(value):
    match = _VERSION.fullmatch(value) if isinstance(value, str) else None
    if not match or any(len(part) > 6 for part in match.groups()):
        raise KGFirmwareError("KG firmware has an invalid numeric version")
    return tuple(int(part) for part in match.groups())


def _positive_int(value, maximum):
    return type(value) is int and 0 < value <= maximum


def _trusted_redirect(url, initial):
    if url == initial:
        return True
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname in
            ("release-assets.githubusercontent.com", "objects.githubusercontent.com")
            and parsed.port is None and not parsed.username and not parsed.password
            and not parsed.fragment
            and re.fullmatch(
                rf"/github-production-release-asset(?:-2e65be)?/{REPOSITORY_ID}/[A-Za-z0-9_-]+",
                parsed.path) is not None)


class _AssetRedirect(HTTPRedirectHandler):
    def __init__(self, initial):
        self.initial = initial

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _trusted_redirect(newurl, self.initial):
            raise KGFirmwareError("KG download redirected outside its approved repository")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_url(url, limit, *, open_url=None, asset=False):
    if open_url is None:
        opener = build_opener(_AssetRedirect(url))
        open_url = opener.open
    request = Request(url, headers={"User-Agent": "ESP32-Watch-Dogs",
                                   "Accept": "application/vnd.github+json" if not asset else "application/octet-stream"})
    started = time.monotonic()
    try:
        with open_url(request, timeout=10) as response:
            final = response.geturl() if callable(getattr(response, "geturl", None)) else url
            if (not asset and final != url) or (asset and not _trusted_redirect(final, url)):
                raise KGFirmwareError("KG response came from an unexpected source")
            chunks, count = [], 0
            read = getattr(response, "read1", None) or response.read
            while True:
                if time.monotonic() - started > 30:
                    raise KGFirmwareError("KG download exceeded its 30-second budget")
                block = read(min(65536, limit + 1 - count))
                if time.monotonic() - started > 30:
                    raise KGFirmwareError("KG download exceeded its 30-second budget")
                if not block:
                    break
                if not isinstance(block, bytes):
                    raise KGFirmwareError("KG download is not binary data")
                count += len(block)
                if count > limit:
                    raise KGFirmwareError("KG download exceeds its declared size limit")
                chunks.append(block)
            return b"".join(chunks)
    except KGFirmwareError:
        raise
    except Exception as exc:
        # Do not echo signed redirect URLs or transport details into the GUI.
        raise KGFirmwareError("Cannot read the KG firmware source; retry with R") from exc


def _json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise KGFirmwareError("KG metadata contains duplicate fields")
            result[key] = value
        return result
    try:
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError()))
    except KGFirmwareError:
        raise
    except (UnicodeError, ValueError, TypeError) as exc:
        raise KGFirmwareError("KG firmware metadata is invalid") from exc
    if not isinstance(result, dict):
        raise KGFirmwareError("KG firmware metadata must be an object")
    return result


def _asset(release, tag, name, maximum):
    assets = release.get("assets")
    if not isinstance(assets, list) or len(assets) > 128:
        raise KGFirmwareError("KG release assets are invalid")
    matches = [item for item in assets if isinstance(item, dict)
               and isinstance(item.get("name"), str) and item['name'].casefold() == name.casefold()]
    if not matches:
        raise KGFirmwareError("No compatible KG/SOLO package in the latest KG release")
    if len(matches) != 1 or matches[0]['name'] != name:
        raise KGFirmwareError("KG release has ambiguous firmware assets")
    item = matches[0]
    expected_url = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{name}"
    digest = item.get("digest", "")
    if (not _positive_int(item.get("id"), 2**63 - 1)
            or not _positive_int(item.get("size"), maximum)
            or item.get("state") != "uploaded"
            or item.get("url") != f"{API_PREFIX}/releases/assets/{item['id']}"
            or item.get("browser_download_url") != expected_url
            or not isinstance(digest, str) or not digest.startswith("sha256:")
            or not _SHA.fullmatch(digest[7:])):
        raise KGFirmwareError("KG firmware asset identity, size or digest is invalid")
    return item


def fetch_latest_package(*, open_url=None):
    """Read only this fork's latest public release and its exact SOLO manifest."""
    repo = _json(_read_url(REPO_URL, 65536, open_url=open_url))
    if type(repo.get("id")) is not int or repo['id'] != REPOSITORY_ID or repo.get("full_name") != REPOSITORY:
        raise KGFirmwareError("KG firmware repository identity does not match")
    release = _json(_read_url(LATEST_URL, 1024 * 1024, open_url=open_url))
    release_id, tag = release.get("id"), release.get("tag_name")
    if (not _positive_int(release_id, 2**63 - 1)
            or not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", tag)
            or release.get("draft") is not False or release.get("prerelease") is not False
            or release.get("url") != f"{API_PREFIX}/releases/{release_id}"
            or release.get("html_url") != f"https://github.com/{REPOSITORY}/releases/tag/{tag}"):
        raise KGFirmwareError("KG firmware release identity is invalid")
    manifest_asset = _asset(release, tag, MANIFEST_NAME, 65536)
    raw = _read_url(manifest_asset['browser_download_url'], manifest_asset['size'], open_url=open_url, asset=True)
    digest = hashlib.sha256(raw).hexdigest()
    if len(raw) != manifest_asset['size'] or digest != manifest_asset['digest'][7:]:
        raise KGFirmwareError("KG manifest size or SHA256 does not match its release asset")
    manifest = _json(raw)
    schema = manifest.get("schema")
    if type(schema) is not int or schema not in (1, 2):
        raise KGFirmwareError("KG firmware manifest schema is unsupported")
    if schema == 1:
        if digest != LEGACY_MANIFEST_SHA256:
            raise KGFirmwareError("Unapproved legacy KG firmware manifest")
        build = manifest.get("build")
        version = build.get("firmware_cli_version_unchanged") if isinstance(build, dict) else None
    else:
        if type(manifest.get("solo_contract")) is not int or manifest['solo_contract'] != 1:
            raise KGFirmwareError("KG firmware does not declare SOLO contract 1")
        version = manifest.get("firmware_version")
    version = ".".join(str(part) for part in version_tuple(version))
    target = manifest.get("target")
    if not isinstance(target, dict) or any(type(target.get(key)) is not type(value) or target[key] != value
                                           for key, value in _TARGET.items()):
        raise KGFirmwareError("KG package is not for the approved XIAO C5 revision1.0 / 8MiB ota_0 layout")
    if schema == 2 and target.get("write_scope") != "application_only":
        raise KGFirmwareError("KG firmware package is not application-only")
    image = manifest.get("image")
    name = f"janos-{version}-solo-xiao-c5-app.bin"
    if (not isinstance(image, dict) or image.get("path") != name
            or not _positive_int(image.get("bytes"), APP_BYTES)
            or not isinstance(image.get("sha256"), str) or not _SHA.fullmatch(image['sha256'])):
        raise KGFirmwareError("KG application image metadata is invalid")
    if schema == 1 and (version != "1.7.0" or image['bytes'] != 2250896 or image['sha256'] != LEGACY_IMAGE_SHA256):
        raise KGFirmwareError("Unapproved legacy KG application image")
    binary = _asset(release, tag, name, APP_BYTES)
    if binary['size'] != image['bytes'] or binary['digest'] != "sha256:" + image['sha256']:
        raise KGFirmwareError("KG image does not match its own release asset")
    return KGPackage(version, tag, name, image['bytes'], image['sha256'],
                     binary['browser_download_url'], digest)


def validate_image_bytes(raw):
    """Validate a plain C5 application image without changing any header byte."""
    if not isinstance(raw, bytes) or not 24 <= len(raw) <= APP_BYTES:
        raise KGFirmwareError("KG image has an invalid application size")
    if raw[0] != 0xE9 or not 1 <= raw[1] <= 16 or raw[2] != 2 or raw[3] != 0x3F or raw[23] != 1:
        raise KGFirmwareError("KG image is not an approved DIO80 / 8MiB application image")
    chip = struct.unpack_from('<H', raw, 12)[0]
    minimum, maximum = struct.unpack_from('<HH', raw, 15)
    if chip != 23 or not 100 <= minimum <= maximum <= 199:
        raise KGFirmwareError("KG image chip or revision range is incompatible with XIAO C5 revision1")
    offset, checksum = 24, 0xEF
    for index in range(raw[1]):
        if offset + 8 > len(raw):
            raise KGFirmwareError("KG image segment header is truncated")
        _address, size = struct.unpack_from('<II', raw, offset)
        offset += 8
        if size == 0 or offset + size > len(raw):
            raise KGFirmwareError("KG image segment is truncated")
        if index == 0 and (size < 256 or struct.unpack_from('<I', raw, offset)[0] != 0xABCD5432):
            raise KGFirmwareError("KG image does not contain a valid application descriptor")
        for value in raw[offset:offset + size]:
            checksum ^= value
        offset += size
    checksum_offset = ((offset + 16) // 16) * 16 - 1
    if checksum_offset + 33 != len(raw) or raw[checksum_offset] != checksum:
        raise KGFirmwareError("KG image checksum or image length is invalid")
    if hashlib.sha256(raw[:checksum_offset + 1]).digest() != raw[checksum_offset + 1:]:
        raise KGFirmwareError("KG image internal validation hash is invalid")
    return ImageInfo(chip, minimum, maximum)


def _validate_package(package):
    if (not isinstance(package, KGPackage)
            or not isinstance(package.release_tag, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,80}", package.release_tag)
            or not _positive_int(package.image_bytes, APP_BYTES)
            or not isinstance(package.image_sha256, str) or not _SHA.fullmatch(package.image_sha256)
            or not isinstance(package.manifest_sha256, str) or not _SHA.fullmatch(package.manifest_sha256)):
        raise KGFirmwareError("No validated KG firmware package")
    version = '.'.join(map(str, version_tuple(package.version)))
    name = f'janos-{version}-solo-xiao-c5-app.bin'
    if package.version != version or package.image_name != name or package.image_url != (
            f'https://github.com/{REPOSITORY}/releases/download/{package.release_tag}/{name}'):
        raise KGFirmwareError("KG image is outside its approved repository asset scope")


def verify_image(package, path):
    _validate_package(package)
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size != package.image_bytes:
            raise KGFirmwareError("KG image file type or size does not match")
        raw = path.read_bytes()
    except OSError as exc:
        raise KGFirmwareError("Cannot read the prepared KG image") from exc
    if len(raw) != package.image_bytes or hashlib.sha256(raw).hexdigest() != package.image_sha256:
        raise KGFirmwareError("KG image SHA256 does not match its approved package")
    return validate_image_bytes(raw)


def download_image(package, destination, *, open_url=None):
    _validate_package(package)
    raw = _read_url(package.image_url, package.image_bytes, open_url=open_url, asset=True)
    if len(raw) != package.image_bytes or hashlib.sha256(raw).hexdigest() != package.image_sha256:
        raise KGFirmwareError("KG image download size or SHA256 does not match")
    validate_image_bytes(raw)
    destination = Path(destination)
    try:
        with destination.open('xb') as output:
            output.write(raw)
    except OSError as exc:
        raise KGFirmwareError("Cannot create the private KG image file") from exc
    return destination


def _ports(ports):
    if ports is None:
        from serial.tools.list_ports import comports
        ports = comports()
    return list(ports)


def _identity(port):
    serial = getattr(port, 'serial_number', None)
    location = getattr(port, 'location', None)
    if (getattr(port, 'vid', None) != 0x303A or getattr(port, 'pid', None) != 0x1001
            or not isinstance(serial, str) or not 6 <= len(serial) <= 128
            or not isinstance(location, str) or not 1 <= len(location) <= 128
            or not serial.strip() or not location.strip()
            or serial.casefold() in ('unknown', 'none', 'n/a')
            or location.casefold() in ('unknown', 'none', 'n/a')
            or not serial.isascii() or not location.isascii()
            or any(ord(char) < 32 for char in serial + location)
            or set(re.sub('[^A-Za-z0-9]', '', serial).lower()) <= {'0', 'f'}):
        raise KGFirmwareError("Cannot establish a unique native USB identity for the KG device")
    return USBIdentity(port.vid, port.pid, serial, location)


def bind_usb_identity(port, *, ports=None):
    found = _ports(ports)
    matches = [item for item in found if getattr(item, 'device', None) == port]
    if len(matches) != 1:
        raise KGFirmwareError("The selected KG USB device is missing or ambiguous")
    identity = _identity(matches[0])
    if sum(getattr(item, 'serial_number', None) == identity.serial_number
           and getattr(item, 'vid', None) == identity.vid for item in found) != 1:
        raise KGFirmwareError("The KG USB identity is ambiguous; firmware installation was not started")
    return identity


def find_bound_port(identity, *, ports=None):
    if not isinstance(identity, USBIdentity):
        raise KGFirmwareError("No bound KG USB identity")
    available = _ports(ports)
    if sum(getattr(item, 'serial_number', None) == identity.serial_number
           and getattr(item, 'vid', None) == identity.vid for item in available) > 1:
        raise KGFirmwareError("The returning KG USB identity is ambiguous")
    found = []
    for item in available:
        try:
            if _identity(item) == identity:
                found.append(getattr(item, 'device', None))
        except KGFirmwareError:
            continue
    if len(found) > 1:
        raise KGFirmwareError("The returning KG USB device is ambiguous")
    return found[0] if len(found) == 1 and isinstance(found[0], str) else None


_PARTITIONS = {
    'nvs': (1, 2, 0x9000, 0x6000, 0),
    'otadata': (1, 0, 0xF000, 0x2000, 0),
    'phy_init': (1, 1, 0x11000, 0x1000, 0),
    'ota_0': (0, 0x10, APP_OFFSET, APP_BYTES, 0),
    'ota_1': (0, 0x11, 0x410000, APP_BYTES, 0),
}


def validate_partition_table(raw):
    if not isinstance(raw, bytes) or len(raw) != 4096:
        raise KGFirmwareError("Cannot verify the KG device partition table")
    rows, offset = {}, 0
    while offset + 32 <= len(raw):
        entry = raw[offset:offset + 32]
        if entry[:2] == b'\xeb\xeb':
            if entry[2:16] != b'\xff' * 14 or hashlib.md5(raw[:offset]).digest() != entry[16:]:
                raise KGFirmwareError("KG partition table MD5 is invalid")
            if raw[offset + 32:] != b'\xff' * (len(raw) - offset - 32):
                raise KGFirmwareError("KG partition table has unexpected trailing entries")
            break
        if entry[:2] != b'\xaa\x50':
            raise KGFirmwareError("KG partition table is missing its verified MD5 marker")
        _magic, kind, subtype, start, size, label, flags = struct.unpack('<HBBII16sI', entry)
        try:
            name = label.rstrip(b'\0').decode('ascii')
        except UnicodeError as exc:
            raise KGFirmwareError("KG partition label is invalid") from exc
        if '\0' in name or name in rows:
            raise KGFirmwareError("KG partition table has duplicate or invalid labels")
        rows[name] = (kind, subtype, start, size, flags)
        offset += 32
    else:
        raise KGFirmwareError("KG partition table MD5 is missing")
    if rows != _PARTITIONS:
        raise KGFirmwareError("Device layout does not match the approved KG/SOLO ota_0 layout; no write")


def validate_active_ota(raw):
    if not isinstance(raw, bytes) or len(raw) != 8192:
        raise KGFirmwareError("Cannot verify both OTA selection sectors")
    sequences = []
    for offset in (0, 4096):
        sector = raw[offset:offset + 4096]
        if sector == b'\xff' * 4096:
            continue
        if sector[32:] != b'\xff' * (4096 - 32):
            raise KGFirmwareError("OTA selection has unexpected data")
        sequence, _label, state, crc = struct.unpack('<I20sII', sector[:32])
        if sequence in (0, 0xFFFFFFFF) or crc != binascii.crc32(struct.pack('<I', sequence), 0xFFFFFFFF) & 0xFFFFFFFF:
            raise KGFirmwareError("OTA selection CRC or sequence is invalid")
        if state not in (2, 0xFFFFFFFF):
            raise KGFirmwareError("OTA selection is pending, aborted or invalid; no firmware write")
        sequences.append(sequence)
    # The exact approved table has no factory partition; erased OTA data
    # therefore boots its first OTA application. Never modify this selection.
    slot = (max(sequences) - 1) % 2 if sequences else 0
    if slot != 0:
        raise KGFirmwareError("Active app is ota_1; an ota_0-only write cannot update it without changing OTA selection")


def preflight_device(package, image, python, port, attempt, *, run_tool,
                     on_process=None, on_line=None, board='xiao'):
    """Read ROM identity/table/OTA only, under the installer's existing mutex."""
    if board != 'xiao':
        raise KGFirmwareError("This KG package supports only the selected XIAO C5 revision1.0 profile")
    info = verify_image(package, image)
    attempt = Path(attempt)
    table, ota = attempt / 'kg-partition-table.bin', attempt / 'kg-otadata.bin'
    if any(path.exists() or path.is_symlink() for path in (table, ota)):
        raise KGFirmwareError("KG preflight requires fresh private readback files")
    output = []
    def capture(line):
        text = str(line)
        if sum(map(len, output)) + len(text) > 65536:
            raise KGFirmwareError("KG ROM preflight output exceeds its limit")
        output.append(text)
        if on_line is not None:
            private = re.search(r'(?i)\b(?:MAC|serial|unique|efuse|chip\s+(?:id|uid))\s*[:=]', text)
            on_line('KG ROM identity read (private identifier hidden)' if private else text)
    def command(args, *, first=False):
        output.clear()
        run_tool([python, '-m', 'esptool', '--chip', 'esp32c5', '-p', port,
                  '--before', 'default-reset' if first else 'no-reset',
                  '--after', 'no-reset', '--no-stub', *args], capture,
                 on_process=on_process, total_timeout=30, idle_timeout=10)
        return '\n'.join(output)
    chip = command(['chip-id'], first=True)
    match = re.search(r'(?i)ESP32-C5[^\n]*revision\s+v?(\d+)\.(\d+)', chip)
    if not match:
        raise KGFirmwareError("ROM did not confirm an ESP32-C5 revision")
    revision = int(match.group(1)) * 100 + int(match.group(2))
    if revision != 100 or not info.min_revision <= revision <= info.max_revision:
        raise KGFirmwareError("ROM chip revision is incompatible with this KG image")
    flash = command(['flash-id'])
    if not re.search(r'(?i)(?:detected\s+)?flash\s+size:\s*8\s*MB\b', flash):
        raise KGFirmwareError("ROM did not confirm the required 8MiB flash")
    security = command(['get-security-info'])
    for label in ('Secure Boot', 'Flash Encryption'):
        values = re.findall(r'(?im)^\s*' + label + r'\s*:\s*(\S+)\s*$', security)
        if len(values) != 1 or values[0].casefold() != 'disabled':
            raise KGFirmwareError("Unsigned KG application requires Secure Boot and Flash Encryption explicitly disabled")
    command(['read-flash', '0x8000', '0x1000', str(table)])
    try:
        if not stat.S_ISREG(table.lstat().st_mode) or table.stat().st_size != 4096:
            raise KGFirmwareError("KG partition readback type or size is invalid")
        validate_partition_table(table.read_bytes())
    except OSError as exc:
        raise KGFirmwareError("Cannot read the device partition-table result") from exc
    command(['read-flash', '0xf000', '0x2000', str(ota)])
    try:
        if not stat.S_ISREG(ota.lstat().st_mode) or ota.stat().st_size != 8192:
            raise KGFirmwareError("KG OTA readback type or size is invalid")
        validate_active_ota(ota.read_bytes())
    except OSError as exc:
        raise KGFirmwareError("Cannot read the device OTA-selection result") from exc
    verify_image(package, image)
