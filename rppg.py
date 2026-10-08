"""POS rPPG method: turns the mean RGB signal of the skin into a pulse signal.

POS = "Plane-Orthogonal-to-Skin" (Wang, den Brinker, Stuijk and de Haan, 2017,
"Algorithmic Principles of Remote PPG", IEEE Trans. Biomedical Engineering).

Idea in three steps, repeated on short overlapping windows (~1.6 s):
  1. Temporal normalisation: divide each colour channel by its own average,
     so the skin colour and the brightness of the light no longer matter.
  2. Projection: combine the channels into two signals, S1 = G - B and
     S2 = -2R + G + B. Both cancel changes that affect R, G and B equally
     (such as light flicker or slow brightness changes), but both keep the
     pulse, because blood changes the colour channels by different amounts.
  3. Alpha tuning: add the two signals as h = S1 + (std(S1) / std(S2)) * S2.
     Noise mostly appears in the two signals in opposite directions, so it
     cancels, while the pulse adds up.
The pulses from all windows are added together (overlap-add) to give one
continuous pulse signal.
"""

import numpy as np

WINDOW_SECONDS = 1.6  # about one heartbeat at the slowest heart rate we want (40 BPM)

# Projection matrix from the POS paper: row 1 gives S1, row 2 gives S2
PROJECTION = np.array([[0.0, 1.0, -1.0],
                       [-2.0, 1.0, 1.0]])


def pos(rgb, fps):
    """Return the POS pulse signal for an (N, 3) array of mean R, G, B values.

    rgb -- one row per frame, columns R, G, B
    fps -- sampling rate in samples per second
    The result has N values. It is all zeros if there are too few samples.
    """
    rgb = np.asarray(rgb, dtype=float)
    n = len(rgb)
    window = int(round(WINDOW_SECONDS * fps))
    pulse = np.zeros(n)
    if window < 2 or n < window:
        return pulse

    for start in range(n - window + 1):
        end = start + window
        segment = rgb[start:end]

        # 1. Temporal normalisation
        normalised = segment / segment.mean(axis=0)

        # 2. Projection onto the plane orthogonal to the skin tone
        s1, s2 = PROJECTION @ normalised.T

        # 3. Alpha tuning
        std_s2 = s2.std()
        alpha = s1.std() / std_s2 if std_s2 > 0 else 0.0
        h = s1 + alpha * s2

        # Overlap-add the zero-mean result into the full pulse signal
        pulse[start:end] += h - h.mean()

    return pulse
