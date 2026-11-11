"""Run isolated native tests and verify the exported patch; never access devices.

Requires a C11 compiler (or Zig) and the external cJSON 1.7.19 source directory.
The pinned reference is read-only. All compilation/apply output is temporary.
"""
import argparse
import hashlib
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import tempfile


ROOT = Path(__file__).resolve().parent
PINNED_MAIN_SHA256 = "839136e2826fdf880c2dce95b77bfed484e0942cecc46b490ad15fe7def9add2"
CJSON_HASHES = {
    "cJSON.c": "298581a04a36c0165da4b0aade235c23088cb2faa58651d720ea2f3706ed0b0d",
    "cJSON.h": "25b0145150d500498e4d209cec69c18c42cf818bffcc54690be3b895a2a16dee",
}


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path)
    parser.add_argument("--cc", required=True, type=Path)
    parser.add_argument("--cjson-dir", required=True, type=Path)
    args = parser.parse_args()
    reference = args.reference.resolve()
    main_source = reference / "ESP32C5/main/main.c"
    if sha256(main_source) != PINNED_MAIN_SHA256:
        raise SystemExit("Pinned main.c hash mismatch; do not apply to another revision")
    for name, expected in CJSON_HASHES.items():
        if sha256(args.cjson_dir / name) != expected:
            raise SystemExit("cJSON 1.7.19 source hash mismatch: " + name)
    spec = importlib.util.spec_from_file_location("solo_export", ROOT / "export_patch.py")
    exporter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(exporter)
    expected_patch = exporter.export(reference)
    patch = ROOT / "projectzero-solo.patch"
    if patch.read_text(encoding="utf-8") != expected_patch:
        raise SystemExit("Stale patch; regenerate projectzero-solo.patch first")
    with tempfile.TemporaryDirectory(prefix="wdg-solo-validate-") as temp:
        work = Path(temp)
        for name in ("main.c", "CMakeLists.txt"):
            destination = work / "ESP32C5/main" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(reference / "ESP32C5/main" / name, destination)
        git = ["git", "-c", "core.autocrlf=false"]
        subprocess.run(git + ["apply", "--check", "--whitespace=error", str(patch)], cwd=work, check=True)
        subprocess.run(git + ["apply", "--whitespace=error", str(patch)], cwd=work, check=True)
        for name in ("solo_core.c", "solo_core.h", "solo_adapter.inc"):
            if (work / "ESP32C5/main" / name).read_bytes() != (ROOT / "src" / name).read_bytes():
                raise SystemExit("Patch/source mismatch: " + name)
        if (work / "ESP32C5/main/main.c").read_text(encoding="utf-8") != exporter.patch_main(main_source.read_text(encoding="utf-8")):
            raise SystemExit("Applied main.c differs from deterministic export")
        print("PASS pinned source, deterministic export and isolated git apply", flush=True)
        compiler = [str(args.cc.resolve())]
        if args.cc.stem.lower() == "zig":
            compiler.append("cc")
        flags = ["-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic", "-I" + str(ROOT / "src")]
        suffix = ".exe" if os.name == "nt" else ""
        for kind in ("core", "adapter"):
            command = compiler + flags + [str(ROOT / "src/solo_core.c"), str(ROOT / ("tests/test_solo_" + kind + ".c"))]
            if kind == "adapter":
                command += ["-I" + str(ROOT / "tests/stubs"), "-I" + str(args.cjson_dir.resolve()), str(args.cjson_dir.resolve() / "cJSON.c")]
            executable = work / ("test_solo_" + kind + suffix)
            subprocess.run(command + ["-o", str(executable)], check=True)
            subprocess.run([str(executable)], check=True)
    if sha256(main_source) != PINNED_MAIN_SHA256:
        raise SystemExit("Reference unexpectedly changed")
    print("PASS reference unchanged; no SDK build, hardware, flash, GPS or SD test performed")


if __name__ == "__main__":
    main()
