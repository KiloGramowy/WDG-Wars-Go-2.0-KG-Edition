"""Export exact opt-in hooks against the owner's pinned read-only reference.

Reads references, writes ONLY projectzero-solo.patch beside this script. It
neither edits the reference nor initializes/clones/pushes/branches any Git repo.
"""
import argparse
import difflib
from pathlib import Path


def once(text, old, new):
    if text.count(old) != 1:
        raise ValueError("Pinned source mismatch at hook: " + old[:90])
    return text.replace(old, new, 1)


def patch_main(text):
    text = once(text, "static volatile bool operation_stop_requested = false;",
        "static volatile bool operation_stop_requested = false;\n"
        "// Opt-in WDG SOLO owner; legacy behavior is unchanged while false.\n"
        "static volatile bool wdg_solo_active = false;\n"
        "static bool wdg_solo_stop(void);\n"
        "static void wdg_solo_capture_wifi(const uint8_t *, const char *, size_t, wifi_auth_mode_t, uint8_t, int8_t);\n"
        "static void wdg_solo_capture_ble(const uint8_t *, const uint8_t *, size_t, int8_t);")
    for name in ("ensure_wifi_mode", "ensure_ble_mode", "ensure_ieee802154_mode"):
        old = "static bool " + name + "(void)\n{"
        text = once(text, old, old + '\n    if (wdg_solo_active) {\n'
            '        MY_LOG_INFO(TAG, "Wardriving SOLO owns the radio. Use stop first.");\n'
            '        return false;\n    }')
    old = "static int cmd_stop(int argc, char **argv) {\n    (void)argc; (void)argv;"
    text = once(text, old, old + "\n    if (!wdg_solo_stop()) return 1;")
    text = once(text, "static void wdp_promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type) {\n    if (!wardrive_promisc_active) return;",
        "static void wdp_promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type) {\n"
        "    if (!wardrive_promisc_active && !wdg_solo_active) return;")
    text = once(text, "    if (esp_timer_get_time() < wdp_log_gate_until_us) return;  // startup cooldown",
        "    if (!wdg_solo_active && esp_timer_get_time() < wdp_log_gate_until_us) return;  // legacy cooldown")
    begin = text.index("static void wdp_promiscuous_cb(void *buf, wifi_promiscuous_pkt_type_t type) {")
    end = text.index("// Evict up to", begin)
    callback = text[begin:end]
    callback = once(callback, "    char ssid[33] = {0};", "    char ssid[33] = {0};\n    uint8_t observed_ssid_length = 0;")
    callback = once(callback, "            memcpy(ssid, &body[offset + 2], tag_len);",
        "            memcpy(ssid, &body[offset + 2], tag_len);\n            observed_ssid_length = tag_len;")
    callback = once(callback, "    int existing = wdp_find_bssid(ap_bssid);",
        "    if (wdg_solo_active) {\n"
        "        wdg_solo_capture_wifi(ap_bssid, ssid, observed_ssid_length, authmode,\n"
        "                              beacon_channel, (int8_t)pkt->rx_ctrl.rssi);\n"
        "        return;\n    }\n\n    int existing = wdp_find_bssid(ap_bssid);")
    text = text[:begin] + callback + text[end:]
    begin = text.index("static int bt_gap_event_callback(struct ble_gap_event *event, void *arg)\n{")
    end = text.index("static int bt_start_scan(void)", begin)
    callback = text[begin:end]
    marker = "    struct ble_gap_disc_desc *desc = &event->disc;"
    callback = once(callback, marker,
        marker + "\n    if (wdg_solo_active) {\n"
        "        struct ble_hs_adv_fields observed;\n"
        "        if (!wardrive_blacklist_contains(desc->addr.val) &&\n"
        "            ble_hs_adv_parse_fields(&observed, desc->data, desc->length_data) == 0)\n"
        "            wdg_solo_capture_ble(desc->addr.val, observed.name, observed.name_len, desc->rssi);\n"
        "        return 0;\n    }")
    text = text[:begin] + callback + text[end:]
    text = once(text, "    if (g_scan_in_progress) why = \"wifi_scan\";",
        "    if (wdg_solo_active) why = \"wardrive_solo\";\n"
        "    else if (g_scan_in_progress) why = \"wifi_scan\";")
    register = "static void register_commands(void)\n{"
    text = once(text, register,
        '#include "solo_adapter.inc"\n\n' + register + "\n    wdg_solo_register();")
    return text


def export(reference):
    root = Path(__file__).resolve().parent
    patches = []
    main = reference / "ESP32C5/main/main.c"
    old = main.read_text(encoding="utf-8")
    new = patch_main(old)
    patches.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
        "a/ESP32C5/main/main.c", "b/ESP32C5/main/main.c"))
    cmake = reference / "ESP32C5/main/CMakeLists.txt"
    old = cmake.read_text(encoding="utf-8")
    new = once(old, '"main.c"', '"main.c" "solo_core.c"')
    new = once(new, '"solo_core.c" "attack_handshake.c" \n',
               '"solo_core.c" "attack_handshake.c"\n')
    patches.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
        "a/ESP32C5/main/CMakeLists.txt", "b/ESP32C5/main/CMakeLists.txt"))
    for name in ("solo_core.c", "solo_core.h", "solo_adapter.inc"):
        text = (root / "src" / name).read_text(encoding="utf-8")
        patches.extend(difflib.unified_diff([], text.splitlines(True),
            "/dev/null", "b/ESP32C5/main/" + name))
    return "".join(patches)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    args = parser.parse_args()
    target = Path(__file__).resolve().parent / "projectzero-solo.patch"
    target.write_text(export(args.reference), encoding="utf-8", newline="\n")
    print(target)
