"""Fetch hash-pinned Debian OpenSSL 3.6.5 packages without enabling a new apt suite.

Debian 13 has no packaged CVE-2026-84782 fix yet. These official Debian packages
retain libssl.so.3 and their dependencies are satisfied by our pinned Trixie base.
Remove this bridge once the normal base supplies a verified patched version.
"""
import hashlib
import platform
import sys
import urllib.request
from pathlib import Path

VERSION = '3.6.5-1'
HASHES = {
    'amd64': {
        'libssl3t64': '0a0ec84fe860c5c93c74d53cbb071aea0793327a3761b483080e9d7b73c21b37',
        'openssl': '6f79a040c684879a5ea444ad215616247d26ce2ee068d9c463f00597fdcd6a3d',
        'openssl-provider-legacy': '82b7c056f7dfd91e4eedb1a0738d655867b6e13bd25ce03406e9cf464e9345ad',
    },
    'arm64': {
        'libssl3t64': '76770c099cfb06cd48cb0764ac3a7771c8b38e17d25780010d55772dce6b36f8',
        'openssl': '48cbf3c762c8446a6f82603c972ac3c8ae50cf54e7293f8a59f0aa98d9bf7c84',
        'openssl-provider-legacy': 'd20c164b2d206a1295df5c8b5cefe9b84846b1e3cfa057639b632245f867d426',
    },
}


def fetch(destination, architecture):
    if architecture not in HASHES:
        raise ValueError('Unsupported OpenSSL patch architecture')
    destination.mkdir(parents=True, exist_ok=True)
    for package, checksum in HASHES[architecture].items():
        name = f'{package}_{VERSION}_{architecture}.deb'
        with urllib.request.urlopen(f'https://deb.debian.org/debian/pool/main/o/openssl/{name}', timeout=60) as response:
            data = response.read(20 * 1024 * 1024 + 1)
        if hashlib.sha256(data).hexdigest() != checksum:
            raise ValueError(f'OpenSSL package checksum mismatch: {name}')
        (destination / name).write_bytes(data)


if __name__ == '__main__':
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(platform.machine())
    fetch(Path(sys.argv[1]), arch)
