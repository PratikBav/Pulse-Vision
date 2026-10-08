"""Signal collection and processing.

Phase 2: a rolling buffer that keeps the last few seconds of mean RGB values.
"""

from collections import deque

import numpy as np

WINDOW_SECONDS = 12  # how much recent signal to keep (10-15 s is enough for heart rate)


class RGBBuffer:
    """Keeps the most recent samples of (timestamp, R, G, B), one sample per frame."""

    def __init__(self, window_seconds=WINDOW_SECONDS):
        self.window_seconds = window_seconds
        self.samples = deque()

    def add(self, timestamp, rgb):
        """Add one sample and drop any samples older than the window."""
        r, g, b = rgb
        self.samples.append((timestamp, r, g, b))
        while timestamp - self.samples[0][0] > self.window_seconds:
            self.samples.popleft()

    def clear(self):
        self.samples.clear()

    def __len__(self):
        return len(self.samples)

    def duration(self):
        """Time span covered by the buffer, in seconds."""
        if len(self.samples) < 2:
            return 0.0
        return self.samples[-1][0] - self.samples[0][0]

    def sampling_rate(self):
        """Average number of samples per second (roughly the camera frame rate)."""
        duration = self.duration()
        return (len(self.samples) - 1) / duration if duration > 0 else 0.0

    def get_arrays(self):
        """Return (times, rgb): times has shape (N,), rgb has shape (N, 3) with columns R, G, B."""
        data = np.array(self.samples, dtype=float).reshape(-1, 4)
        return data[:, 0], data[:, 1:]
