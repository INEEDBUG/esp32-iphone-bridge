# Local field OCR

This optional component reads only a cropped synthetic text field on the local host. It creates no network listener and uploads no image. The changing-page marker, visible focus border and native caret are checked separately against the original screenshot. Empty fields use absence of text ink plus the native caret-at-start check; they do not rely on OCR of a blank image.

Install the pinned project dependency and verified English data explicitly:

```powershell
npm ci --prefix scripts/local-ocr --ignore-scripts --no-audit --fund=false
.\.venv\Scripts\python.exe scripts/prepare_text_ocr.py --download
```

`node_modules` and language data are excluded from publication. The data file is stored under the configured `PHONELAB_DATA_ROOT`, outside the source tree. Runtime recognition uses a local Node worker and local language file; initialization happens before selecting an input-authorizing frame. The worker returns only an exact-match boolean and an approved length, never recognized text.

- Engine: [Tesseract.js 7.0.0](https://github.com/naptha/tesseract.js/tree/v7.0.0), Apache-2.0; all npm versions and integrity digests are in `package-lock.json`.
- Data: [tessdata_fast English](https://github.com/tesseract-ocr/tessdata_fast/blob/87416418657359cb625c412a48b6e1d6d41c29bd/eng.traineddata), Apache-2.0.
- Data SHA-256: `7d4322bd2a7749724879683fc3912cb542f19906c83bcc1a52132556427170b2`, 4,113,088 bytes. A mismatch blocks use.

Generated-image checks are host evidence only. Real text entry still requires matching original before/after phone images and separately recorded browser input/state assertions. OCR success alone is not device-input proof.
