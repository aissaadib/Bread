# Manual highlight evaluation

Synthetic recordings in the test suite check event extraction, timestamps, cancellation, and result bounds. They are not evidence of useful real-world highlight detection.

The current 3-hour synthetic check generated a 160×90, 1 fps MP4 with one combined event at 90:00. Analysis took 22.6 seconds on a 20-logical-processor Windows system with 15.7 GiB available memory, returning one candidate at 90:00.500 (0.5-second peak error). Sampled process-family peak RSS was 204.2 MiB. This measures a low-complexity synthetic file with no optional speech models; it is not a real-game or accuracy result. Repeat it with `python worker/tests/benchmark_synthetic.py`.

For each game/streamer evaluation:

1. Select several recordings (including quiet play, normal speech, loud reactions, and microphone noise) with permission to analyze them.
2. Have a reviewer label highlight intervals before looking at Luna's results. Record start, peak, end, and a short reason.
3. Run the same local analysis settings and keep every returned candidate; do not tune on the held-out set.
4. Match a candidate to a label when their peak timestamps are within a predeclared tolerance (start with ±10 seconds). Count unmatched candidates as false positives and unmatched labels as false negatives.
5. Report precision, recall, F1, mean absolute peak error, start/end boundary error, processing wall time divided by recording duration, and peak process memory.
6. Repeat across multiple sessions and report sample counts and per-game/per-streamer ranges. Do not present synthetic results as accuracy.

Suggested formulas: precision = matched candidates / all candidates; recall = matched labels / all labels. Report both micro averages and recording-level values so one long recording does not hide poor performance on the rest.
