#!/usr/bin/env python3
"""Stands in for Squeezelite in tests: records its arguments, then plays a loud 440 Hz tone as
stereo 16-bit little-endian PCM at 48 kHz on stdout (what `squeezelite -o - -a 16` does)."""
import array, math, os, sys, time

with open(os.environ["FAKE_ARGS"], "a") as f:
    f.write(" ".join(sys.argv[1:]) + "\n")
n = 0
while True:
    block = array.array("h")
    for _ in range(960):                                  # 20 ms
        v = int(12000 * math.sin(2 * math.pi * 440 * n / 48000))
        block.extend((v, v))
        n += 1
    sys.stdout.buffer.write(block.tobytes())
    sys.stdout.buffer.flush()
    time.sleep(0.02)
