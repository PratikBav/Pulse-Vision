"""Signal collection and processing.

Phase 2: a rolling buffer that keeps the last few seconds of mean RGB values.
Phase 4: resampling, band-pass filter, FFT, heart rate, signal quality and smoothing.
"""

from collections import deque

import numpy as np
from scipy.signal import butter, sosfiltfilt

WINDOW_SECONDS = 12  # how much recent signal to keep (10-15 s is enough for heart rate)
SAMPLE_RATE = 30.0   # the signal is resampled to this fixed rate (samples per second)

# Heart rates we search for
MIN_BPM = 40
MAX_BPM = 180
LOW_HZ = MIN_BPM / 60    # 0.67 Hz
HIGH_HZ = MAX_BPM / 60   # 3.0 Hz

# The filter is slightly wider than the search range (36-198 BPM), because a
# Butterworth filter already weakens frequencies close to its cut-off points
FILTER_LOW_HZ = 0.6
FILTER_HIGH_HZ = 3.3

# Signal quality. These values were chosen by testing with synthetic signals:
# pure noise stayed below 0.45, while clear pulses scored about 0.7-0.9.
PEAK_WIDTH_HZ = 0.1      # power within +/-0.1 Hz (+/-6 BPM) of the peak counts as "heart rate"
GOOD_QUALITY = 0.60      # peak power ratio needed for "Good"
FAIR_QUALITY = 0.45      # peak power ratio needed for "Fair"; below this is "Poor"


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


def resample(times, rgb, fs=SAMPLE_RATE):
    """Resample the RGB signal onto evenly spaced times.

    Webcam frames do not arrive at perfectly regular intervals, but filtering
    and the FFT assume they do. Linear interpolation fixes this.
    """
    uniform_times = np.arange(times[0], times[-1], 1.0 / fs)
    uniform_rgb = np.column_stack([np.interp(uniform_times, times, rgb[:, c]) for c in range(3)])
    return uniform_times, uniform_rgb


def bandpass_filter(signal, fs=SAMPLE_RATE, low_hz=FILTER_LOW_HZ, high_hz=FILTER_HIGH_HZ, order=3):
    """Keep only heart-rate frequencies using a Butterworth band-pass filter.

    sosfiltfilt runs the filter forwards and backwards, so the waveform is not delayed.
    """
    sos = butter(order, [low_hz, high_hz], btype="bandpass", fs=fs, output="sos")
    return sosfiltfilt(sos, signal)


def power_spectrum(signal, fs=SAMPLE_RATE):
    """Return (freqs, power) of the signal using the FFT.

    A Hann window reduces leakage between frequencies, and zero padding to
    60 * fs points gives one FFT bin per BPM, so the peak is located precisely.
    """
    n_fft = max(len(signal), int(60 * fs))
    windowed = signal * np.hanning(len(signal))
    power = np.abs(np.fft.rfft(windowed, n=n_fft)) ** 2
    freqs = np.fft.rfftfreq(n_fft, d=1.0 / fs)
    return freqs, power


def find_heart_rate(freqs, power):
    """Find the dominant frequency between 40 and 180 BPM.

    Returns (bpm, peak_ratio):
      bpm        -- dominant frequency x 60, or None if the result is not valid
      peak_ratio -- share of the 40-180 BPM power that lies close to the peak (0 to 1).
                    A clean pulse puts most of its power in one peak, noise spreads it out.
    """
    band = (freqs >= LOW_HZ) & (freqs <= HIGH_HZ)
    band_freqs, band_power = freqs[band], power[band]
    total = band_power.sum()
    if total <= 0:
        return None, 0.0

    peak = int(np.argmax(band_power))
    peak_hz = band_freqs[peak]
    near_peak = np.abs(band_freqs - peak_hz) <= PEAK_WIDTH_HZ
    peak_ratio = float(band_power[near_peak].sum() / total)

    # A peak on the edge of the range is leakage from outside it, not a heart rate
    if peak == 0 or peak == len(band_power) - 1:
        return None, peak_ratio
    return peak_hz * 60, peak_ratio


def signal_quality(peak_ratio):
    """Convert the peak power ratio into Good / Fair / Poor."""
    if peak_ratio >= GOOD_QUALITY:
        return "Good"
    if peak_ratio >= FAIR_QUALITY:
        return "Fair"
    return "Poor"


class BPMSmoother:
    """Moving average of the most recent BPM estimates, so the displayed value is stable."""

    def __init__(self, size=5):
        self.values = deque(maxlen=size)

    def add(self, bpm):
        self.values.append(bpm)

    def value(self):
        """Smoothed BPM, or None if there are no estimates yet."""
        if not self.values:
            return None
        return sum(self.values) / len(self.values)

    def clear(self):
        self.values.clear()
