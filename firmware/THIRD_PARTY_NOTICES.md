# Firmware dependency notices

The game, JanOS and Piglet retain their upstream authorship. Firmware application
images include third-party runtime components; the main repository MIT license
does not replace those component licenses.

## JanOS / WARDRIVING SOLO

- Original **LOCOSP/projectZero**: MIT, preserved in
  [`wardriving-solo/LICENSE.ProjectZero`](wardriving-solo/LICENSE.ProjectZero),
  including the upstream **Copyright (c) 2025 Janek** notice. SOLO source changes
  and exact upstream commit are identified in that package's manifest/BUILD guide.
- **ESP-IDF 6.0.1**: [Apache 2.0 text](licenses/esp-idf-Apache-2.0.txt), with
  separately licensed bundled components. The copied notices below retain those
  distinctions rather than labeling the whole image MIT.
- **cJSON 1.7.19**, **LVGL 9.2.0**, **led_strip 3.0.3**:
  [cJSON](licenses/cJSON.txt), [component wrapper](licenses/cJSON-component.txt),
  [LVGL](licenses/LVGL.txt), [LED strip](licenses/LED-Strip.txt).
- Shared SDK/runtime notices: [FreeRTOS](licenses/FreeRTOS.txt),
  [NimBLE](licenses/NimBLE.txt), [TinyCrypt](licenses/TinyCrypt.txt),
  [lwIP](licenses/lwIP.txt), [Mbed TLS](licenses/mbedTLS.txt),
  [TF-PSA-Crypto](licenses/mbedTLS-TF-PSA.txt),
  [linenoise](licenses/linenoise.txt), [argtable3](licenses/argtable3.txt),
  [HTTP parser](licenses/http-parser.txt), [SPIFFS](licenses/SPIFFS.txt),
  [FatFs](licenses/FatFs-header-notice.txt), [newlib](licenses/newlib.txt),
  [picolibc](licenses/picolibc.txt).
- Espressif binary radio components retain their own notices:
  [Wi-Fi](licenses/esp-wifi.txt), [PHY](licenses/esp-phy.txt),
  [coexistence](licenses/esp-coex.txt), [BT controller](licenses/esp-bt-controller.txt).

## Piglet KG / UConsole Piglet

- Original **Piglet KG Edition**: MIT, preserved in
  [`piglet-collection/LICENSE`](piglet-collection/LICENSE). Full sketch sources,
  comparison baseline, versions and board options are included in its manifest.
- **Arduino-ESP32 3.3.11** retains its upstream mixed-component licenses. See the
  [Arduino core header notice](licenses/Arduino-core-header-notice.txt),
  [LGPL 2.1](licenses/LGPL-2.1.txt), [BLE library notice](licenses/Arduino-BLE.txt)
  and [libb64 notice](licenses/Arduino-libb64.txt). The SDK and runtime libraries
  retain their own licenses, rather than becoming MIT through the sketch.
- **TinyGPSPlus 1.0.3**: [Mikal Hart's header notice](licenses/TinyGPSPlus-header-notice.txt)
  and [LGPL 2.1](licenses/LGPL-2.1.txt).
- **Adafruit SSD1306 2.5.17**, **Adafruit GFX 1.13.0**, **Adafruit BusIO 1.17.4**:
  [SSD1306](licenses/Adafruit-SSD1306.txt), [GFX](licenses/Adafruit-GFX.txt),
  [BusIO](licenses/Adafruit-BusIO.txt).
- **ArduinoJson 6.21.5**: [Benoit Blanchon's MIT notice](licenses/ArduinoJson.txt).

The public source dependencies can be obtained and rebuilt using the pinned
procedures in each package's BUILD guide. Preserve their source headers and
licenses when rebuilding or redistributing. Do not claim exclusive authorship
of these libraries or distribute SDK/compiler binaries as part of this source
package.

## Compiler runtime

The compiler itself is a build dependency, not a bundled executable. Runtime
parts retain the [GCC GPL 3 text](licenses/GCC-GPL-3.0.txt) and applicable
[GCC Runtime Library Exception](licenses/GCC-runtime-exception.txt); other runtime
parts retain their individual notices above. This is a notice collection, not
a new license or a declaration that every tool/dependency is MIT.
