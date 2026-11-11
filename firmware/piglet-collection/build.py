#!/usr/bin/env python3
"""Verify and compile the included candidate. Never installs or flashes anything."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cli', default='arduino-cli')
    parser.add_argument('--config', type=Path)
    parser.add_argument('--build-path', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    manifest = json.loads((root / 'manifest.json').read_text(encoding='utf-8'))
    for row in manifest['sources']:
        data = (root / row['path']).read_bytes()
        if len(data) != row['bytes'] or hashlib.sha256(data).hexdigest() != row['sha256']:
            raise SystemExit('Source integrity mismatch: ' + row['path'])
    for path in (args.build_path, args.output):
        path = path.resolve()
        if path == root or root in path.parents:
            raise SystemExit('Build/output directories must be outside the firmware package.')
    command = [args.cli]
    if args.config:
        command += ['--config-file', str(args.config.resolve())]
    info = subprocess.run(command + ['version'], check=True, capture_output=True, text=True)
    if manifest['build']['arduino_cli'] not in info.stdout:
        raise SystemExit('Use the pinned Arduino CLI version recorded in manifest.json.')
    env = dict(os.environ, SOURCE_DATE_EPOCH=str(manifest['build']['source_date_epoch']))
    subprocess.run(command + ['compile', '--fqbn', manifest['build']['fqbn'],
                             '--export-binaries=false',
                             '--build-path', str(args.build_path.resolve()),
                             '--output-dir', str(args.output.resolve()),
                             str(root / 'source' / 'Piglet')], check=True, env=env)
    image = args.output / 'Piglet.ino.bin'
    data = image.read_bytes()
    print(json.dumps(dict(image=str(image.resolve()), bytes=len(data),
                          sha256=hashlib.sha256(data).hexdigest(),
                          matches_packaged_image=hashlib.sha256(data).hexdigest() ==
                          manifest['image']['sha256']), indent=2))


if __name__ == '__main__':
    main()
