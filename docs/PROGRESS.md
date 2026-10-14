# Public progress

**Updated:** 2026-10-14

**State:** experimental; not a complete device-control release

## Verified so far

- On one Windows/iPhone 11 iOS 17.5.1 setup, USB screenshot capture and a local synthetic PhoneLab page have been exercised. Historical trials recorded clicks on three distinct synthetic targets across multiple runs and one small vertical scroll. This is a small sample, not a completed matrix.
- The image guard now resolves a shared-edge outline ambiguity with a unique pale-fill region anchored at the OCR label. It accepted the archived synthetic Probe 12 geometry; the same image still failed the final alignment check because its pointer was not aligned. Exact gray masks with bounded morphological cleanup also separated a gray pointer from nearby text in host tests while retaining uniqueness checks.
- Two preserved resumed trials stopped before clicking: one after six moves when the cursor occluded the summary, and one after 24 moves when short border fragments were discarded before cursor-gap bridging. The corrections retain the expected summary total across unreadable frames and reapply the original edge-length minimum after the existing bounded bridge. A fresh Probe 12 trial then verified exactly one click: target count 0 to 1, total 0 to 1, matching trusted down/up/click events and a delayed real USB screenshot. This is one-target evidence, not full acceptance.
- One renewed image-guarded wheel sample independently matched document movement and a six-pixel screenshot displacement. Follow-up bidirectional batches stopped: one request was rejected before dispatch, and another retained an unknown result when the final image no longer showed the pointer. Neither batch passed; the unknown request was not replayed.
- Full image/target checks now run once after the serial hello and before the requested action; early page/session/freshness checks remain. The final source-image age limit is still two seconds. A new one-wheel diagnostic recorded the final guard passing before the wheel write and independently matched document movement with screenshot displacement. This does not establish that pointer auto-hide caused the earlier unknown result.
- A new alternating small-wheel batch verified six bidirectional samples with unchanged click counts, then stopped on the seventh request before any wheel write. The full ten-sample batch did not pass. A separately invoked `image-page-scroll` experiment retains the existing image/session checks with a bounded magnitude up to 16; the original `image-scroll` limit stays at 3. One +16 sample still moved only three CSS pixels, so larger arguments have not established faster or broader scrolling. A separate +16 control with a cancellation-aware 200ms post-ACK observation dwell had the same displacement; it does not support adding a production delay.
- Pointer connected components now use an eight-connected scanline traversal; morphology, thresholds, geometry, uniqueness and freshness are unchanged. An independent pixel traversal matched all 512 binary 3x3 masks, 100 seeded random masks and edge/diagonal/hole/nonzero-byte fixtures. Eight synthetic detector cases and three authorized archived real frames preserved the old results, including rejection. On one accepted archived frame, full guard time was about 1.66s before and 1.14s after; this is a local sample, not a timing guarantee.
- After an accidental Windows shutdown, the frozen phone metadata and existing pairing were verified again. The board initially returned no serial data; one user USB reconnect allowed a complete read-only identification, matching the experimental image outside NVS. A baseline click was rejected before dispatch and a later small wake had an unknown result; both were retained without replay. A new finite trial with stricter image alignment then verified one Probe 12 click from 0 to 1 and total 0 to 1 through serial ordering, trusted down/up/click events and a delayed real USB image. This is one observed recovery, not the three-round reconnect or full fault-matrix acceptance.
- Text-scope API behavior has host tests only. No real text guard, CLI text action, or text-entry effect has been demonstrated.
- Image validation can outlast the firmware's 1.5-second session watchdog. Bounded same-session heartbeats keep preparation alive; an earlier UART trace recorded heartbeat acknowledgements and a click acknowledgement, followed by a matching browser event and delayed USB screenshot. Cancellation and the original frame-expiry limit still apply.
- The approved board received a serial `hello` session handshake, followed by shared cancellation. The UART trace contained only `hello` and `stop`, with zero HID action writes; release behavior remains unconfirmed. No keyboard key was tested.
- Firmware builds have been checked locally. Build success does not verify radio behavior or iOS response.

## Still open

- The complete 25-target input/scroll matrix, repeatable pointer calibration, and the full visual loop are not complete.
- Real text entry, drag, broader scrolling, reconnect behavior, and sustained input stability remain unverified. The historical click and one-scroll samples do not establish these behaviors.
- Cross-device, cross-version, accessibility, and lock-state behavior are not established. `UNKNOWN` remains the lock-state result when the project cannot verify it; pairing, screen capture, and browser events do not establish unlock state.
- The 22-pixel pointer-disc assumption can be unknown, while the observed lab pointer is about 38 pixels across. Missing labels, unexplained border gaps and ambiguous geometry remain rejections. The two-second frame-age limit has not been extended.
- Earlier guard validation took about 1.688 seconds in one local run. The newer paired archived-frame result above is likewise specific to its image and host; the two-second source-age limit remains unchanged.
- Full board-backup restoration has not been tested. Experimental firmware must not be treated as recoverable solely because a backup file exists.
- The reviewed changes passed 467 host tests plus 66 subtests, with one Windows symlink-creation test skipped, in 85.39 seconds. This includes temporary-summary-occlusion, consecutive-click, cursor-covered edge-fragment and text-dispatch isolation regressions; it does not establish real-device effects.

## Reproducing a bounded visual experiment

After separately completing the private device binding, approved firmware deployment and synthetic-page preparation, an explicit image-based trial can target a chosen visible probe:

```powershell
.\.venv\Scripts\python.exe -m phonelab.visual_trials --execute --probes 10 --rounds 1 --image-clicks
```

Default invocation observes only. The explicit mode requires fresh real screenshots, a unique detected pointer, the selected target's unchanged image/count, exclusive input ownership and the shared stop generation. A rejected or uncertain action stops the trial without replay. Passing this bounded experiment does not complete the full acceptance matrix.

No private screenshots, run identifiers, pairing records, firmware images, board backups, tokens, or machine-specific paths are included in this progress note.
