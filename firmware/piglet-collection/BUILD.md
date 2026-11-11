# 🛠️ Build the Piglet Collection candidate

**0.9.20 DEV / unreleased.** This builds the separate **XIAO ESP32-C5 Piglet**;
it does not build or flash the embedded uConsole JanOS firmware.

The complete sketch is in [`source/Piglet/`](source/Piglet/Piglet.ino). No local device
configuration, SD files, credentials or flash dump is used to build the image.
Source provenance, individual hashes and exact board options are recorded in
[`manifest.json`](manifest.json). Preserve the included upstream license.

## Pinned tools

- Arduino CLI **1.5.1**.
- Espressif Arduino core **esp32:esp32@3.3.11**.
- TinyGPSPlus **1.0.3**; Adafruit SSD1306 **2.5.17**;
  Adafruit GFX Library **1.13.0**; Adafruit BusIO **1.17.4**;
  ArduinoJson **6.21.5**.
- Board **XIAO_ESP32C5**, USB CDC on boot, CPU **240 MHz**, QIO flash at
  **80 MHz**, **8 MiB** flash, `default_8MB`, PSRAM enabled, erase disabled.
  The full FQBN is in the manifest. Upload-speed board option is **921600**;
  the separately validated flashing procedure uses **460800**.

`build.py` never installs packages or flashes a device. In a new environment,
install the pinned tools explicitly, using Espressif's Arduino package index
`https://espressif.github.io/arduino-esp32/package_esp32_index.json`:

```sh
arduino-cli core update-index --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.11 --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install 'TinyGPSPlus@1.0.3' 'Adafruit SSD1306@2.5.17' 'Adafruit GFX Library@1.13.0' 'Adafruit BusIO@1.17.4' 'ArduinoJson@6.21.5'
```

Inspect `arduino-cli core list` and `arduino-cli lib list` before building.
Do not silently substitute newer libraries. An existing isolated Arduino CLI
configuration can be supplied with `--config`.

## Compile and verify

Run from this directory, with build/output paths **outside** the package:

```sh
python build.py --build-path /path/to/piglet-build --output /path/to/piglet-output
```

Windows example (use paths appropriate to your own machine):

```powershell
python .\build.py --cli C:\Tools\arduino-cli.exe --build-path C:\Temp\piglet-build --output C:\Temp\piglet-output
```

The script validates every included sketch source against the manifest, sets the
pinned `SOURCE_DATE_EPOCH`, compiles the complete sketch and reports the output
SHA256. Compare `Piglet.ino.bin` with the packaged image. Compiler paths, OS and
toolchain can affect binary identity; the manifest identifies the tested image,
not an untested rebuild. Do not overwrite the packaged checksum after a mismatch.

The compiler also creates bootloader/partition/merged images. **They are not
installation inputs for this candidate.** Use only the application image and
the verified procedure in the [installation guide](../../docs/PIGLET_COLLECTION_INSTALL.md).
