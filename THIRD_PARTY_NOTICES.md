# Third-party notices

Unless a file or component is identified below as third-party material, the project’s own code is offered under GNU GPL version 3 or, at your option, any later version. The root `LICENSE` is the verbatim GPL version 3 text. This does not replace the individual license terms that apply to third-party code.

## Source retained in this repository

| Component | Source/version | License and notice |
| --- | --- | --- |
| ESP32KeyBridge | `tanakamasayuki/ESP32KeyBridge`, commit `517adc19f11d1e06d4f4fecc492c7b7e1ebee7e3` | MIT. The vendored source retains its `LICENSE` and `UPSTREAM.md`; local adapter changes are described there. |
| Arduino-ESP32 NVS patch context | `cores/esp32/esp32-hal-misc.c`, Arduino-ESP32 3.3.11 | Copyright 2015-2016 Espressif Systems (Shanghai) PTE LTD; Apache-2.0. The patch directory retains the license and a notice describing the fail-closed modification. The full core and compiled binaries are not bundled. |
| ESP32 iPhone Agent S3 reference firmware | `onehumanbeing/esp32-iphone-agent` | MIT applies to the complete preserved upstream source subtree under `firmware/upstream-s3/`, including its source and accompanying files. The copy retains the upstream `LICENSE` and README attribution. It is a separate reference/build path. |
| ESP32 iPhone Agent reference server | `onehumanbeing/esp32-iphone-agent` | The separate `upstream/esp32-iphone-agent/` source subtree retains its own MIT `LICENSE`, original README and attribution. Its presence does not make it an enabled runtime backend. |

## Pinned tools and runtime dependencies

`requirements.lock.txt` pins 107 Python distributions. The following high-impact copyleft declarations were observed in the installed distribution metadata for that lock:

| Distributions | Declared license information |
| --- | --- |
| `pymobiledevice3==11.29.0`, `pmd-net-addr==0.0.3`, `pmd-net-proto==0.0.3`, `pmd-pytcp==0.3.7` | GPL-3.0-or-later license expression. The `pymobiledevice3` v11.29.0 license is available from its [upstream source](https://github.com/doronz88/pymobiledevice3/tree/v11.29.0). The project-local USB tunnel adapter preserves upstream headers and a copy of the original dependency file. |
| `developer_disk_image==0.3.0`, `ipsw-parser==1.8.2`, `opack2==0.0.1`, `pycrashreport==2.0.0`, `pygnuutils==0.1.1`, `pyiosbackup==0.2.4` | GPLv3-or-later license classifier in distribution metadata. These are installed dependencies, not copied source in this repository. |
| `esptool==5.3.1` | GPLv2-or-later license classifier. |
| `pylzss==0.3.4` | LGPLv3 license classifier. |

The installed `pytun-pmd3==3.0.3` distribution metadata is inconsistent: its `License` field contains MIT text, while a classifier says GPLv3-or-later. The upstream `v3.0.3` [`LICENSE`](https://github.com/doronz88/pytun-pmd3/blob/v3.0.3/LICENSE) is MIT; this notice follows that versioned license file and records the stale/conflicting classifier. PhoneLab does not distribute the dependency package or its virtual environment. The distribution also contains separately licensed Wintun prebuilt binaries; those binaries are not included in this repository and must not be redistributed under the source package's MIT notice.

The remaining lock entries report permissive or public-domain license metadata, including MIT, BSD, Apache, PSF, ISC, 0BSD, and Unlicense variants. MPL-licensed entries are weak copyleft and are classified separately from permissive licenses. `ASGIMiddlewareStaticFile==0.7.0` and `ASGIWebDAV==2.0.2` have no SPDX license expression in metadata; their installed distribution `LICENSE` files state MIT. Metadata is an inventory aid, not a substitute for reviewing source notices. The lock file and installed package metadata used for this inventory are version-specific.

Other build components are not bundled with this project. EspBle 1.3.0 is an MIT-licensed dependency supplied by the project-local build toolchain ([upstream license](https://github.com/tanakamasayuki/EspBle/blob/main/LICENSE); its composite HID example demonstrates the keyboard/mouse profile). UxPlay 1.74 is a GPL-3.0-or-later build-only candidate, pinned by commit `ec203121355220dd90278a369eeabb0b8ba4f14a` in [`scripts/Build-AirPlay.ps1`](scripts/Build-AirPlay.ps1); its source checkout and generated binaries are not included here.

## Not distributed

Optional CPU pointer-mask acceleration pins `opencv-python-headless==4.13.0.92`
and `numpy==2.5.4` in `requirements-vision-fast.txt`. The OpenCV Python packaging
code is MIT; its installed OpenCV binary declares Apache-2.0. NumPy declares
BSD-3-Clause and retains separate notices for bundled components. The wheels'
own license and third-party notice files remain with the installed packages;
no wheels, DLLs, NumPy/OpenCV source copies or virtual environments are included
in this repository. Sources: [OpenCV Python packaging](https://github.com/opencv/opencv-python),
[OpenCV 4.13 morphology documentation](https://docs.opencv.org/4.13.0/d9/d61/tutorial_py_morphological_ops.html),
and [NumPy license](https://numpy.org/doc/stable/license.html).

Optional local field OCR pins [Tesseract.js and its core 7.0.0](https://github.com/naptha/tesseract.js/tree/v7.0.0), Apache-2.0, through `scripts/local-ocr/package-lock.json`. The separately downloaded [tessdata_fast English data](https://github.com/tesseract-ocr/tessdata_fast/tree/87416418657359cb625c412a48b6e1d6d41c29bd) is Apache-2.0. Transitive npm license metadata for the lock is MIT, Apache-2.0 or BSD-2-Clause; each installed package retains its own license. Packages, WASM binaries and language data are not bundled in the public snapshot. Setup runs with lifecycle scripts disabled and does not install Windows language packs. See [local OCR setup](scripts/local-ocr/README.md).

The project does not include its virtual environment, generated firmware or other build binaries, Apple Developer Disk Images, Apple certificates, device pairing records, private captures, board backups, or deployment receipts. Obtain third-party software from its upstream source and follow its license. Do not publish private runtime data or device-specific files.
