# Ready, Set, Lecture!: licence and third-party notices

Ready, Set, Lecture! is free software: you can redistribute it and/or modify it under the terms of the
GNU General Public License as published by the Free Software Foundation, either version 3 of the
License, or (at your option) any later version. The full text is in [`LICENSE`](LICENSE).

Ready, Set, Lecture! is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without
even the implied warranty of MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the GNU
General Public License for more details.

Copyright (C) 2026 **[COPYRIGHT HOLDER: to be filled in; see "Before you distribute", item 1]**

This file lists the software Ready, Set, Lecture! uses or ships, under what licence, and where its source
code is. It was drafted on 2026-09-20 from the development environment the launchers use
(`envs\readysetlecture-qtmm`) and must be re-checked against what is actually packaged. It is not legal
advice.

## 1. How Ready, Set, Lecture! uses other software (and why the whole program is GPL)

* **Run as separate programs:** `ffmpeg.exe` and `ffprobe.exe` do the recording, export, waveform,
  device listing and microphone test. Ready, Set, Lecture! starts them and talks to them through command-line
  arguments and pipes.
* **Loaded into the program's own process:** Qt / PySide6 (the interface), **OpenCV** (colour scanner),
  **libmpv** through python-mpv (video playback) and NumPy. In the conda environment, importing OpenCV
  loads the same FFmpeg libraries (`avcodec-63.dll` and others) together with `libx264` and
  `libx265`. That is GPL code inside the program's process, so Ready, Set, Lecture! as a whole is offered under the GPL,
  version 3 (the FFmpeg build is "GPL version 3 or later" and `qt6-multimedia` is declared
  GPL-3.0-only).

## 2. Summary

| Component | Version | Licence (as declared / built) | How it is used | Licence text |
|---|---|---|---|---|
| **FFmpeg** | 9.0.2 (conda-forge build `gpl_h928aae7_900`) | GPL-3.0-or-later (built with `--enable-gpl --enable-version3`) | `ffmpeg.exe`, `ffprobe.exe`; libav* loaded by OpenCV | `LICENSE`, `third_party_licenses/FFmpeg/` |
| **x264** | 1!164.3095 (commit `baee400f`) | GPL-2.0-or-later | H.264 encoder in FFmpeg: **used for recording and export** | `third_party_licenses/x264/` |
| **x265** | 3.5 | GPL-2.0-or-later | HEVC encoder in FFmpeg (present, not used by Ready, Set, Lecture!) | `third_party_licenses/x265/` |
| **OpenH264** | 2.6.0 | BSD-2-Clause | H.264 encoder in FFmpeg: fallback when x264 is absent | `third_party_licenses/OpenH264/` |
| **libmpv** (mpv) | v0.41.0-1012-ge8673660a (`packaging/vendor/win-64/libmpv-2.dll`) | GPL-2.0-or-later by default; **confirm for this build** | video playback in the editor, loaded in-process | see section 4 |
| **python-mpv** | 1.0.8 | GPLv2+ or LGPLv2.1+ (used here under the GPL) | Python wrapper for libmpv | (on PyPI) |
| **Qt** | 6.11.2 | LGPL-3.0-only (Qt Multimedia: GPL-3.0-only) | user interface, camera / audio | `third_party_licenses/Qt/`, `Qt-Multimedia/` |
| **PySide6** (with shiboken6) | 6.11.2 | LGPL-3.0-only | Python bindings for Qt | `third_party_licenses/PySide6-Qt/` |
| **OpenCV** | 5.0.0 (conda-forge) | Apache-2.0 | colour scanning, in-process | `third_party_licenses/OpenCV/` |
| **NumPy** | 2.4.6 | BSD-3-Clause | arrays | `third_party_licenses/NumPy/` |
| **Intel oneMKL** | 2026.1.0 | Intel Simplified Software License | NumPy's maths backend in the conda build | `third_party_licenses/Intel-MKL/` |
| **Python** | 3.11.16 | Python-2.0 (PSF) | interpreter | `third_party_licenses/Python/` |

All 159 packages of the development environment, with their declared licences, are listed in
[`third_party_licenses/environment-inventory.csv`](third_party_licenses/environment-inventory.csv)
(generated from the conda metadata).

## 3. FFmpeg and the libraries built into it

### Corresponding source

* **FFmpeg 9.0.2**: <https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.gz>
  (sha256 `84960df915059e8754fef2cd7c9afeb614062b1b5458ec471eecee619ee04e98`), with the conda-forge build
  recipe and its patches (`0001` to `0007`: Windows pkg-config, `rc.exe` options, `windres`, private
  requirements, Chromium patches, `lahs`, an NVENC double-free fix). The recipe and patches are inside the
  conda package (`info/recipe/`) and in the conda-forge `ffmpeg-feedstock`.
* **Build configuration** (from `ffmpeg -version`): `--enable-gpl --enable-version3 --enable-openssl
  --enable-libx264 --enable-libx265 --enable-libopenh264 --enable-libdav1d --enable-libaom --enable-libsvtav1
  --enable-libmp3lame --enable-libopus --enable-libvorbis --enable-libwebp --enable-libjxl --enable-librsvg
  --enable-libfreetype --enable-libharfbuzz --enable-libfontconfig --enable-libxml2 --enable-zlib
  --enable-vulkan --enable-shared`. Not built with `--enable-nonfree`.
* The FFmpeg library binaries report their licence as "GPL version 3 or later".

### Libraries inside that build

| Library | Version | Declared licence | Source |
|---|---|---|---|
| x264 | 1!164.3095 | GPL-2.0-or-later | <https://code.videolan.org/videolan/x264/-/archive/baee400fa9ced6f5481a728138fed6e867b0ff7f/x264-baee400fa9ced6f5481a728138fed6e867b0ff7f.tar.gz> (sha256 `436a2be54d8bc0cb05dd33ecbbcb7df9c3b57362714fcdaa3a5991189a33319b`) |
| x265 | 3.5 | GPL-2.0-or-later (also sold under a commercial licence by MulticoreWare) | <https://bitbucket.org/multicoreware/x265_git/downloads/x265_3.5.tar.gz> (sha256 `e70a3335cacacbba0b3a20ec6fecd6783932288ebc8163ad74bcc9606477cae8`) |
| OpenH264 | 2.6.0 | BSD-2-Clause | <https://github.com/cisco/openh264/archive/v2.6.0.tar.gz> |
| libaom | 3.14.1 | BSD-2-Clause | <https://aomedia.googlesource.com/aom> |
| dav1d | 1.5.4 | BSD-2-Clause | <https://code.videolan.org/videolan/dav1d> |
| SVT-AV1 | 4.2.0 | BSD-2-Clause (see its `LICENSE.md`) | <https://gitlab.com/AOMediaCodec/SVT-AV1/-/archive/v4.2.0/SVT-AV1-v4.2.0.tar.gz> |
| LAME | 4.0 | LGPL-2.0-only | <https://downloads.sourceforge.net/sourceforge/lame/lame-4.0.tar.gz> |
| Opus | 1.6.1 | BSD-3-Clause | <https://opus-codec.org> |
| libvorbis / libogg | 1.3.7 / 1.3.5 | BSD-3-Clause | <https://xiph.org> |
| libwebp | 1.6.0 | BSD-3-Clause | <http://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-1.6.0.tar.gz> |
| libjxl | 0.12.0 | BSD-3-Clause | <https://github.com/libjxl/libjxl> |
| FreeType | 2.14.3 | GPL-2.0-only OR FTL | <https://download.savannah.gnu.org/releases/freetype/freetype-2.14.3.tar.gz> |
| HarfBuzz | 14.4.0 | MIT | <https://github.com/harfbuzz/harfbuzz/archive/14.4.0.tar.gz> |
| Fontconfig | 2.18.3 | MIT | <https://gitlab.freedesktop.org/api/v4/projects/890/packages/generic/fontconfig/2.18.3/fontconfig-2.18.3.tar.xz> |
| libxml2 | 2.15.4 | MIT | <https://gitlab.gnome.org/GNOME/libxml2/-/archive/v2.15.4/libxml2-v2.15.4.tar.gz> |
| librsvg | 2.62.3 | LGPL-2.1-or-later | <https://download.gnome.org/sources/librsvg/2.62/librsvg-2.62.3.tar.xz> |
| OpenSSL | 3.6.4 | Apache-2.0 | <https://github.com/openssl/openssl/releases/download/openssl-3.6.4/openssl-3.6.4.tar.gz> |
| zlib | 1.3.2 | Zlib | <https://zlib.net> |

The licence texts of all of these are in `third_party_licenses/<name>/`. Where a source link above is
a project home page rather than an archive, the exact archive is recorded in that package's conda
recipe (`info/recipe/meta.yaml` inside the package).

## 4. libmpv and python-mpv

The editor plays video with `libmpv-2.dll` (mpv v0.41.0-1012-ge8673660a), kept in
`packaging/vendor/win-64/`. mpv's source is at <https://github.com/mpv-player/mpv> (commit `e8673660a`).
The DLL statically contains its own FFmpeg (a development snapshot, `N-126314-g3386acd2f`, libavcodec
63.8.101) and other libraries, whose licences depend on how that DLL was built. **The build's provenance
and exact licence still have to be confirmed** (see "Before you distribute", item 2). mpv is GPL-2.0-or-later
unless built as LGPL. python-mpv is GPLv2+ or LGPLv2.1+ and is used here under the GPL.

## 5. Qt and PySide6

Qt 6.11.2 and PySide6 6.11.2 are used as shared libraries, unmodified. They are available under the
LGPL-3.0 (Qt Multimedia under the GPL-3.0); Ready, Set, Lecture!, being GPL-3.0-or-later, uses them under those
terms. Source: <https://download.qt.io/official_releases/qt/> (Qt) and
<https://code.qt.io/cgit/pyside/pyside-setup.git/> (PySide6), or the conda-forge recipes.

## 6. OpenCV

OpenCV 5.0.0 is Apache-2.0 (compatible with GPL version 3). Source:
<https://github.com/opencv/opencv/archive/5.0.0.tar.gz>. The conda-forge build links the FFmpeg libraries
of section 3. `pyproject.toml` asks for the PyPI package `opencv-python`; if Ready, Set, Lecture! is packaged with that
wheel instead, the wheel bundles its own (LGPL) FFmpeg, and sections 1 and 3 must be revised.

## 7. Patents

Copyright licences do not grant patent rights. H.264 (x264, OpenH264) and H.265 (x265) are covered by
patents held by others, and AAC (FFmpeg's built-in encoder) likewise. Free personal, internal or academic use is
generally not what patent pools charge for, but commercial distribution may need licences. Cisco's
patent arrangement for OpenH264 covers only the binaries Cisco itself builds and distributes; it does not
cover copies built by others, such as the conda-forge build.

## 8. Before you distribute

1. **Who owns the copyright?** If this code was written as part of university work, UCF may own it, and
   its Office of Technology Transfer would need to agree to a GPL release. Put the correct holder in the
   copyright line at the top of this file.
2. **Confirm libmpv:** where `libmpv-2.dll` came from, its licence (GPL or LGPL build), and the list and
   licences of what is statically linked into it. Add its build scripts or source pointers here.
3. **Host the source:** GPL requires giving recipients the complete corresponding source of everything GPL
   that you ship: Ready, Set, Lecture! itself plus FFmpeg, x264, x265 and mpv, with the build configuration. Keep the exact
   archives listed above on a server you control, next to the download, for as long as you offer the binaries.
4. **Ship the notices:** include `LICENSE`, this file and `third_party_licenses/` with every download.
5. **Re-check at packaging time:** which libraries actually end up in the package (a PyInstaller build
   can pull in far more or fewer than this list) and their versions. Note that NumPy in the conda environment
   brings Intel MKL under the Intel Simplified Software License; either accept and ship its terms or use a
   NumPy build with a different maths backend.
6. **Do not add non-free components** (for example `fdk-aac`, or a `--enable-nonfree` FFmpeg): they make the
   combination impossible to redistribute.
7. **Keep recipients' rights intact:** any terms of use or installer agreement must not restrict copying,
   modifying or redistributing the GPL parts.
