# Remaining-time estimates

The September 2026 repair covers GIF jobs, Test Lab, maintenance scans, combined
scans, queue summaries, preflight estimates, and the global activity strip. It
changes reporting only; media processing and scan/review/apply safety are unchanged.

## Diagnosis

- FFmpeg progress overwrote estimated wall-clock job duration with GIF playback
  duration. These are now separate fields.
- GIF timing extrapolated from rounded display percentages, including arbitrary
  92%/97% rendering weights. Preparation/palette startup distorted the slope, and
  repeated blending prolonged the influence of stale history.
- Optimization and installation inherited rendering's deadline, producing zero
  while work continued. Each stage now learns its own successful duration.
- Scans borrowed previous library sizes and whole-run timings for different
  scopes. Discovery now stays indeterminate until the total is known. A known
  stage can show its own timing while later stages remain unknown.
- Queued jobs retained enqueue-time estimates despite new completed samples.
  Queues and Test Lab now refresh their predictions. Independently polled activity
  and Test Lab views refresh expired timing before serialization.

## Strategy and limits

`time_estimate.py` measures seconds per completed work unit over a bounded recent
30-second window. A live slope needs at least two seconds of observations;
duplicate progress fields and UI polling do not add evidence. Measured throughput
then replaces historical throughput, adapting to both speedups and slowdowns.
Stalls expire predictions after at least ten seconds (or three measured unit
intervals), and resumed progress restores them.

GIF rendering uses output media time as work units, anchored at the first output
to exclude palette startup. Finishing estimates use separately recorded successful
preparation, rendering, optimization/finalization, and installation stages. A first
run may have no whole-job estimate because the optimizer is still unmeasured.
Opaque stages stop predicting once their historical expected duration has elapsed.

GIF history uses the latest twelve comparable successful jobs, separates motion
interpolation and optimization modes, and deduplicates persisted/in-memory copies.
Scan history uses recent per-workflow, per-unit samples. Serial totals require all
remaining components to be known. Paused queues and jobs waiting for library access
cannot promise completion times. Rounding cannot mark an active queue complete.

Historical predictions remain approximate: codecs, source resolutions, item sizes,
original FPS, disk contention, network response, and server load can change costs.
The UI distinguishes early estimates, current-stage timing, unknown work,
recalculation, and completion. Synthetic tests do not establish a real-library
accuracy percentage.

## Evidence

- Deterministic traces check constant throughput, speed changes, stalls/recovery,
  polling independence, bounded memory, invalid totals, and serial sums. At two
  units/second, 120 remaining units predict the actual 60 seconds despite stale
  hardware history.
- The scan regression with 80/100 units finished in 48 seconds predicts the actual
  remaining 12 seconds, rather than the old accepted 20–35 seconds.
- GIF regressions cover playback/wall-time separation, a 50-second startup before
  output, separately timed finishing, an overdue optimizer, and persisted stage
  history. Queue/API tests cover recalibration, pauses, and completion rounding.
- Local qualification: 22 frontend and 63 browser tests passed, including timing
  states at 1280px and 375px. The full Windows Python run covered 720 tests with ten
  platform/media-tool skips; its one outdated clock fixture was corrected and the
  affected tests passed. Coverage was 83.63%, above the unchanged 80% floor.
  Linux CI is the release gate, including actual FFmpeg/gifsicle integration.

Efficiency: one local full Python and one full browser qualification, with focused
checks for repairs. No helper agents. Token usage was unavailable.
