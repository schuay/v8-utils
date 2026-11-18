"""Mach-O headers: the images in a thin or universal file, with the UUID and
__TEXT address that tie a profile's library to the file on disk."""

from __future__ import annotations

import struct
from dataclasses import dataclass
from pathlib import Path

_MH_MAGIC_64 = 0xFEEDFACF
_FAT_MAGIC = 0xCAFEBABE
_FAT_MAGIC_64 = 0xCAFEBABF
_LC_SEGMENT_64 = 0x19
_LC_UUID = 0x1B
_HEADER_SIZE = 32


@dataclass(frozen=True)
class Image:
    uuid: str  # 32 upper-case hex digits, as samply's codeId
    text_vmaddr: int
    offset: int  # extent within the file; a thin file is one image at 0
    size: int
    universal: bool


def images(path: Path) -> list[Image]:
    """The 64-bit Mach-O images in `path`; empty for any other file."""
    with path.open("rb") as f:
        head = f.read(8)
        if len(head) < 8:
            return []
        (magic,) = struct.unpack(">I", head[:4])
        if magic in (_FAT_MAGIC, _FAT_MAGIC_64):
            (count,) = struct.unpack(">I", head[4:])
            wide = magic == _FAT_MAGIC_64
            entry = struct.Struct(">iiQQII" if wide else ">iiIII")
            arches = f.read(entry.size * count)
            found = []
            for i in range(count):
                fields = entry.unpack_from(arches, i * entry.size)
                offset, size = fields[2], fields[3]
                image = _read_image(f, offset, size, universal=True)
                if image is not None:
                    found.append(image)
            return found
        size = path.stat().st_size
        image = _read_image(f, 0, size, universal=False)
        return [image] if image is not None else []


def _read_image(f, offset: int, size: int, *, universal: bool) -> Image | None:
    f.seek(offset)
    header = f.read(_HEADER_SIZE)
    if len(header) < _HEADER_SIZE:
        return None
    magic, _, _, _, ncmds, sizeofcmds, _, _ = struct.unpack("<IiiIIIII", header)
    if magic != _MH_MAGIC_64:
        return None
    commands = f.read(sizeofcmds)
    uuid = None
    text_vmaddr = None
    pos = 0
    for _ in range(ncmds):
        cmd, cmdsize = struct.unpack_from("<II", commands, pos)
        if cmd == _LC_UUID:
            uuid = commands[pos + 8 : pos + 24].hex().upper()
        elif cmd == _LC_SEGMENT_64:
            segname = commands[pos + 8 : pos + 24].rstrip(b"\0")
            if segname == b"__TEXT":
                (text_vmaddr,) = struct.unpack_from("<Q", commands, pos + 24)
        pos += cmdsize
    if uuid is None or text_vmaddr is None:
        return None
    return Image(uuid, text_vmaddr, offset, size, universal)
