# 🛠️ Build and validate JanOS SOLO

This directory extends **LOCOSP/projectZero** at exactly
**`ef35e9bbed28c441f53abdb6e634cf46428f912b`**, using **ESP-IDF v6.0.1**.
The original source is public at:

[projectZero pinned source](https://github.com/LOCOSP/projectZero/tree/ef35e9bbed28c441f53abdb6e634cf46428f912b).

The included patch changes only `ESP32C5/main/main.c`, its CMake source list,
and the three added SOLO sources. Existing commands and firmware version remain.
No device dump, NVS, SD configuration or personal data is a build input.
Preserve [`LICENSE.ProjectZero`](LICENSE.ProjectZero).

## 📌 Exact build inputs

- Official **ESP-IDF v6.0.1**. The verified release archive is
  [esp-idf-v6.0.1.zip](https://github.com/espressif/esp-idf/releases/download/v6.0.1/esp-idf-v6.0.1.zip),
  SHA256 `4f294f44ddcf7b6677ae2347808fc708768a4890fed3c6f7227b7d4c076b9019`.
- Official **riscv32-esp-elf GCC 15.2.0**, `esp-15.2.0_20251204`, as pinned by
  that SDK. The tested build used Python **3.12.16**.
- Target **esp32c5**. The included effective
  [`build-inputs/sdkconfig`](build-inputs/sdkconfig) is the validated XIAO
  configuration after applying upstream's `sdkconfig.xiao`; retain the whole
  file rather than guessing settings from `sdkconfig.defaults`.
- Flash **8 MiB, DIO 80 MHz**; USB Serial/JTAG console; FreeRTOS **100 Hz**.
- [`build-inputs/dependencies.lock`](build-inputs/dependencies.lock) pins
  `espressif/cjson` **1.7.19~2**, `espressif/led_strip` **3.0.3**, and
  `lvgl/lvgl` **9.2.0**. The component manager obtains these public dependencies.
- The unchanged partition layout is in
  [`build-inputs/partitions.csv.example`](build-inputs/partitions.csv.example).

Use an **isolated SDK and build tree**. Upstream CMake intentionally applies
its exFAT and linenoise UTF-8 changes to that SDK. It also exports binaries into
the source tree. Do not point it at an SDK shared by another project or a live
WDG installation. Building does not need a serial device or flash permission.
Install the pinned SDK/tool dependencies explicitly before following these steps;
no WDG tool installs packages automatically.

## 🧱 Reproduce the source and build

Run in a short, new working directory. Here `PACKAGE` is the absolute path to
this `firmware/wardriving-solo` directory. Do not substitute a moving branch for
the pinned commit.

```sh
git clone --no-checkout https://github.com/LOCOSP/projectZero.git projectzero-solo
git -C projectzero-solo checkout --detach ef35e9bbed28c441f53abdb6e634cf46428f912b
git -C projectzero-solo rev-parse HEAD
```

The unpatched `ESP32C5/main/main.c` must have SHA256
`839136e2826fdf880c2dce95b77bfed484e0942cecc46b490ad15fe7def9add2`.
Then apply the distributed patch and copy the validated build inputs:

```sh
git -C projectzero-solo apply --check "$PACKAGE/projectzero-solo.patch"
git -C projectzero-solo apply "$PACKAGE/projectzero-solo.patch"
cp "$PACKAGE/build-inputs/sdkconfig" projectzero-solo/ESP32C5/sdkconfig
cp "$PACKAGE/build-inputs/dependencies.lock" projectzero-solo/ESP32C5/dependencies.lock
cp "$PACKAGE/build-inputs/partitions.csv.example" projectzero-solo/ESP32C5/partitions.csv
```

Activate the installed **v6.0.1** SDK environment using its `export.sh` on Linux
or `export.ps1` on Windows. Confirm `idf.py --version` and
`riscv32-esp-elf-gcc --version`. From `projectzero-solo/ESP32C5`:

```sh
idf.py --preview -D CMAKE_JOB_POOLS=solo_compile=4 -D CMAKE_JOB_POOL_COMPILE=solo_compile build
```

The four-job pool only limits compilation resource use. Do **not** run
`idf.py flash`, `erase-flash`, an OTA installer or an upstream publishing script
as part of this build. If a component cannot resolve to the pinned lock, stop
and investigate; a newer dependency is not the same validated configuration.

The normal build creates `build/projectZerobyLOCOSP.bin` plus bootloader,
partition and metadata files. **Only the application corresponds to this
SOLO package.** The separately distributed application is 2,250,896 bytes,
SHA256 `e3dff6b534ff8d7712d37e181a3d2d1e30d3672877b6925be2a4bf27c8a989c5`.
Its internal ESP image checksum and validation hash were verified with
`esptool image-info`. Its inherited app descriptor is **1.6.9** while the
JanOS CLI reports **1.7.0**; SOLO support is established by capabilities/ACKs.

Build timestamps, paths and OS/toolchain details can change binary identity.
The manifest identifies the actual tested image, not every possible rebuild.
A different hash requires provenance/configuration review and new validation;
do not replace the distributed SHA to hide a mismatch.

## 🧪 Native contract tests without hardware

Use Python 3, Git, a C11 compiler and unmodified
[cJSON 1.7.19](https://github.com/DaveGamble/cJSON/tree/v1.7.19) outside this
package. `validate.py` checks cJSON hashes, the pinned base, deterministic patch
and exact added source bytes, then runs the real scheduler/adapter against fake
SDK/radio/task boundaries and real cJSON:

```sh
python -B validate.py /path/to/projectZero-ef35e9b --cc /path/to/cc --cjson-dir /path/to/cJSON-1.7.19
```

`--cc` also accepts a Zig executable. The reference must be unpatched for this
validation; all patch application and compilation happen in temporary storage.
**10 core + 19 adapter tests pass.** These tests neither open a serial port nor
start RF, GPS, SD, flash or vendor SDK compilation. They do not simulate real
FreeRTOS concurrency. The complete prior vendor build and physical evidence
have separate scopes in [`README.md`](README.md).
