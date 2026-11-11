"""Arduino sensor input over USB serial.

The Arduino sends one reading per line. Two formats are understood:

  1. Newline-delimited JSON (firmware/pulsevision_sensors), for example
       {"t_ms":12345,"max30100":{"ok":true,"hr_bpm":72.4,"spo2_pct":97},
        "mpu":{"ok":true,"ax":-0.02,"ay":0.01,"az":0.99,"a_g":1.00,"motion_g":0.02,"gx":0.4,"gy":-0.2,"gz":0.1}}
  2. The older raw text sketch (sketch_nov1c):  "IR: 51234   RED: 40321"
     Raw values carry no heart rate or SpO2, so those stay unavailable.

A single SerialReader runs in a background thread, so waiting for serial data
never blocks the camera loop or the UI. Only one reader may own a port at a time.
"""

import json
import logging
import math
import random
import re
import threading
import time
from dataclasses import dataclass, field

import serial
from serial.tools import list_ports

log = logging.getLogger("pulsevision.hardware")

DEFAULT_BAUD = 115200
MOCK_PORT = "MOCK (simulated)"
ARDUINO_VID = 0x2341              # Arduino SA USB vendor ID (UNO R4 WiFi uses it)
RECONNECT_SECONDS = 2.0
MAX_LINE_LENGTH = 512

# Plausibility limits - values outside these are rejected as malformed, not
# treated as patient data. They are sensor limits, NOT clinical alert thresholds.
HR_VALID_RANGE = (20.0, 250.0)       # BPM
SPO2_VALID_RANGE = (50.0, 100.0)     # %
ACCEL_AXIS_RANGE = (-16.0, 16.0)     # g (MPU9250/6500/9255 max +/-16g)
ACCEL_MAG_RANGE = (0.0, 28.0)        # g
GYRO_AXIS_RANGE = (-2000.0, 2000.0)  # deg/s
RAW_VALID_RANGE = (0, 65535)         # MAX30100 16-bit ADC counts
# MLX90614 plausibility limits (sensor spec: Ambient -40–85 °C, Object -70–380 °C).
# Restricted to reasonable values for patient monitoring; not clinical thresholds.
AMBIENT_TEMP_RANGE = (-40.0, 85.0)   # °C  (full MLX90614 ambient spec)
OBJECT_TEMP_RANGE = (-70.0, 200.0)   # °C  (restricted from 380 °C spec for safety)

LEGACY_RE = re.compile(r"^\s*IR:\s*(\d+)\s+RED:\s*(\d+)\s*$")

# Port names currently owned by a SerialReader in this process
_open_ports = set()
_open_ports_lock = threading.Lock()


class MalformedLine(ValueError):
    """A serial line that cannot be turned into a valid reading."""


@dataclass
class HardwareReading:
    """One validated reading from the Arduino. Missing values are None, never guessed."""

    received_at: float                 # laptop time.time() when the line arrived
    device_ms: int | None = None       # Arduino millis(), if sent
    # Sensor 1: MAX30100 pulse oximeter
    hr_bpm: float | None = None
    spo2_pct: float | None = None
    sensor_ok: bool | None = None      # MAX30100 initialised and responding
    ir_raw: int | None = None          # legacy raw format only
    red_raw: int | None = None
    # Sensor 2: MPU9250 / MPU6500 / MPU9255 IMU
    mpu_ok: bool | None = None
    accel_g: float | None = None       # total acceleration magnitude sqrt(ax^2+ay^2+az^2) in g
    motion_g: float | None = None      # dynamic motion |accel_g - 1.0g| (0g at rest)
    gyro_dps: float | None = None      # angular velocity magnitude sqrt(gx^2+gy^2+gz^2) in deg/s
    sensor2: dict = field(default_factory=dict)  # raw MPU dict (ax, ay, az, gx, gy, gz, ...)
    # Sensor 3: MLX90614 infrared skin surface temperature (NOT core body temperature)
    mlx_ok: bool | None = None
    ambient_temp_c: float | None = None   # MLX90614 ambient temperature in °C
    object_temp_c: float | None = None    # MLX90614 object (skin surface) temperature in °C
    source_format: str = "json"        # "json" or "legacy_raw"
    simulated: bool = False


def _number(value, valid_range, name):
    """Return value as float if it is a number inside valid_range, None if null."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MalformedLine(f"{name} is not a number")
    if math.isnan(value) or math.isinf(value):
        raise MalformedLine(f"{name} is NaN or infinite")
    low, high = valid_range
    if not low <= value <= high:
        raise MalformedLine(f"{name} out of sensor range")
    return float(value)


def _parse_mpu(mpu_data):
    """Validate MPU9250/6500/9255 object and return (mpu_ok, accel_g, motion_g, gyro_dps, cleaned_dict)."""
    if not mpu_data:
        return None, None, None, None, {}
    if not isinstance(mpu_data, dict):
        raise MalformedLine("mpu/s2 is not an object")

    mpu_ok = mpu_data.get("ok")
    if mpu_ok is not None and not isinstance(mpu_ok, bool):
        raise MalformedLine("mpu.ok is not true/false")

    ax = _number(mpu_data.get("ax"), ACCEL_AXIS_RANGE, "mpu.ax")
    ay = _number(mpu_data.get("ay"), ACCEL_AXIS_RANGE, "mpu.ay")
    az = _number(mpu_data.get("az"), ACCEL_AXIS_RANGE, "mpu.az")
    a_g = _number(mpu_data.get("a_g"), ACCEL_MAG_RANGE, "mpu.a_g")
    motion_g = _number(mpu_data.get("motion_g"), ACCEL_MAG_RANGE, "mpu.motion_g")
    gx = _number(mpu_data.get("gx"), GYRO_AXIS_RANGE, "mpu.gx")
    gy = _number(mpu_data.get("gy"), GYRO_AXIS_RANGE, "mpu.gy")
    gz = _number(mpu_data.get("gz"), GYRO_AXIS_RANGE, "mpu.gz")

    if mpu_ok is False:
        return False, None, None, None, dict(mpu_data)

    if a_g is None and (ax is not None and ay is not None and az is not None):
        a_g = round(math.sqrt(ax * ax + ay * ay + az * az), 3)
    if motion_g is None and a_g is not None:
        motion_g = round(abs(a_g - 1.0), 3)

    gyro_dps = None
    if gx is not None and gy is not None and gz is not None:
        gyro_dps = round(math.sqrt(gx * gx + gy * gy + gz * gz), 1)

    return mpu_ok, a_g, motion_g, gyro_dps, dict(mpu_data)


def _parse_mlx(mlx_data):
    """Validate MLX90614 object and return (mlx_ok, ambient_temp_c, object_temp_c).

    Missing key (old firmware without MLX) returns (None, None, None) — backwards-compatible.
    ok=false returns (False, None, None).
    """
    if not mlx_data:
        return None, None, None
    if not isinstance(mlx_data, dict):
        raise MalformedLine("mlx90614 is not an object")

    mlx_ok = mlx_data.get("ok")
    if mlx_ok is not None and not isinstance(mlx_ok, bool):
        raise MalformedLine("mlx90614.ok is not true/false")

    if mlx_ok is False:
        return False, None, None

    ambient = _number(mlx_data.get("ambient_temp_c"), AMBIENT_TEMP_RANGE, "mlx90614.ambient_temp_c")
    obj = _number(mlx_data.get("object_temp_c"), OBJECT_TEMP_RANGE, "mlx90614.object_temp_c")
    return mlx_ok, ambient, obj


def parse_line(line, received_at=None):
    """Parse one serial line into a HardwareReading.

    Returns None for status/info lines (e.g. "MAX30100 initialized!").
    Raises MalformedLine for lines that look like data but are invalid.
    """
    received_at = time.time() if received_at is None else received_at
    line = line.strip()
    if not line:
        return None
    if len(line) > MAX_LINE_LENGTH:
        raise MalformedLine("line too long")

    if line.startswith(("{", "[")):
        try:
            data = json.loads(line)
        except json.JSONDecodeError as exc:
            raise MalformedLine("invalid JSON") from exc
        if not isinstance(data, dict):
            raise MalformedLine("JSON is not an object")

        device_ms = data.get("t_ms")
        if device_ms is not None and (isinstance(device_ms, bool) or not isinstance(device_ms, int)):
            raise MalformedLine("t_ms is not an integer")

        max_data = data.get("max30100", {})
        if not isinstance(max_data, dict):
            raise MalformedLine("max30100 is not an object")
        sensor_ok = max_data.get("ok")
        if sensor_ok is not None and not isinstance(sensor_ok, bool):
            raise MalformedLine("max30100.ok is not true/false")

        hr = _number(max_data.get("hr_bpm"), HR_VALID_RANGE, "hr_bpm")
        spo2 = _number(max_data.get("spo2_pct"), SPO2_VALID_RANGE, "spo2_pct")
        if sensor_ok is False:
            hr = spo2 = None  # a failed sensor's numbers are meaningless

        raw_mpu = data.get("mpu") if "mpu" in data else data.get("s2", {})
        mpu_ok, accel_g, motion_g, gyro_dps, sensor2 = _parse_mpu(raw_mpu)

        mlx_ok, ambient_temp_c, object_temp_c = _parse_mlx(data.get("mlx90614", {}))

        return HardwareReading(
            received_at=received_at, device_ms=device_ms, hr_bpm=hr, spo2_pct=spo2,
            sensor_ok=sensor_ok, mpu_ok=mpu_ok, accel_g=accel_g, motion_g=motion_g,
            gyro_dps=gyro_dps, sensor2=sensor2,
            mlx_ok=mlx_ok, ambient_temp_c=ambient_temp_c, object_temp_c=object_temp_c,
            source_format="json",
            simulated=data.get("simulated") is True,
        )

    match = LEGACY_RE.match(line)
    if match:
        ir, red = int(match.group(1)), int(match.group(2))
        if not (RAW_VALID_RANGE[0] <= ir <= RAW_VALID_RANGE[1] and RAW_VALID_RANGE[0] <= red <= RAW_VALID_RANGE[1]):
            raise MalformedLine("raw value out of range")
        return HardwareReading(received_at=received_at, ir_raw=ir, red_raw=red,
                               sensor_ok=True, source_format="legacy_raw")

    if line.startswith(("IR", "RED")) or line[0].isdigit() or any(c in line for c in "{}[]"):
        raise MalformedLine("unrecognised data line")
    return None  # free-text status message from the firmware


def list_serial_ports():
    """Return [(device, description)] for all serial ports, Arduino boards first."""
    ports = sorted(list_ports.comports(), key=lambda p: (p.vid != ARDUINO_VID, p.device))
    return [(p.device, p.description or p.device) for p in ports]


def detect_arduino_port():
    """Return the first port that looks like an Arduino, or None."""
    for p in list_ports.comports():
        if p.vid == ARDUINO_VID or "arduino" in (p.description or "").lower():
            return p.device
    return None


class MockSerial:
    """Stand-in for serial.Serial that produces clearly SIMULATED readings.

    For software testing only - its values are never real patient data.
    """

    def __init__(self, port=MOCK_PORT, baudrate=DEFAULT_BAUD, timeout=1.0, **_):
        self.port, self.baudrate, self.timeout = port, baudrate, timeout
        self.is_open = True
        self._start = time.time()
        self._next = self._start

    def readline(self):
        if not self.is_open:
            raise serial.SerialException("mock port closed")
        wait = self._next - time.time()
        if wait > 0:
            time.sleep(min(wait, self.timeout))
            if time.time() < self._next:
                return b""
        self._next += 1.0
        t_ms = int((time.time() - self._start) * 1000)
        ax = round(random.uniform(-0.03, 0.03), 2)
        ay = round(random.uniform(-0.03, 0.03), 2)
        az = round(1.0 + random.uniform(-0.03, 0.03), 2)
        a_g = round(math.sqrt(ax * ax + ay * ay + az * az), 2)
        motion_g = round(abs(a_g - 1.0), 2)
        line = {
            "t_ms": t_ms,
            "simulated": True,
            "max30100": {
                "ok": True,
                "hr_bpm": round(72 + random.uniform(-3, 3), 1),
                "spo2_pct": random.choice([96, 97, 98]),
            },
            "mpu": {
                "ok": True,
                "ax": ax,
                "ay": ay,
                "az": az,
                "a_g": a_g,
                "motion_g": motion_g,
                "gx": round(random.uniform(-1.5, 1.5), 1),
                "gy": round(random.uniform(-1.5, 1.5), 1),
                "gz": round(random.uniform(-1.5, 1.5), 1),
            },
            "mlx90614": {
                "ok": True,
                "ambient_temp_c": round(28 + random.uniform(-2, 2), 1),
                "object_temp_c": round(33 + random.uniform(-1, 1), 1),
            },
        }
        return (json.dumps(line) + "\n").encode()

    def reset_input_buffer(self):
        pass

    def close(self):
        self.is_open = False


def _open_serial(port, baud):
    """Open a serial port configured for native USB CDC boards (Arduino UNO R4 WiFi).

    On Windows (`usbser.sys`), native USB CDC devices (like the Renesas RA4M1 / ESP32-S3
    on UNO R4 WiFi) require DTR=True so the firmware's `Serial` interface knows a host
    is listening, and we open the port with explicit DTR/RTS configuration to avoid
    spurious resets or stalled transfers.
    """
    if port == MOCK_PORT:
        return MockSerial(port, baud, timeout=1.0)
    conn = serial.Serial()
    conn.port = port
    conn.baudrate = int(baud)
    conn.timeout = 1.0
    conn.write_timeout = 1.0
    conn.xonxoff = False
    conn.rtscts = False
    conn.dsrdtr = False
    conn.dtr = True
    conn.rts = False
    conn.open()
    return conn


class SerialReader:
    """Reads the Arduino in a background thread and keeps the latest valid reading.

    Connection status ("disconnected", "connecting", "connected", "reconnecting", "error")
    tracks only the physical/serial link and is never modified by stale-data checks.
    """

    def __init__(self, serial_factory=_open_serial, reconnect_seconds=RECONNECT_SECONDS,
                 port_detector=detect_arduino_port):
        self._factory = serial_factory
        self._reconnect_seconds = reconnect_seconds
        self._port_detector = port_detector
        self._lock = threading.Lock()
        self._thread = None
        self._stop = threading.Event()
        self.port = None
        self.baud = DEFAULT_BAUD
        self.status = "disconnected"
        self.last_error = None
        self.latest = None            # most recent HardwareReading
        self.valid_count = 0
        self.malformed_count = 0
        self.reconnect_count = 0

    def is_running(self):
        """Return True if the background reader thread is alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    # ---- control ----
    def connect(self, port, baud=DEFAULT_BAUD):
        """Start reading from port. No-op if already active on the same port and baud."""
        baud = int(baud)
        with self._lock:
            same_target = (self.port == port and self.baud == baud)
            active_status = self.status in ("connecting", "connected", "reconnecting")
        if same_target and active_status and self.is_running():
            log.info("SerialReader already active on %s at %d baud; keeping existing connection", port, baud)
            return True

        self.disconnect()
        with _open_ports_lock:
            if port in _open_ports:
                self._set_status("error", f"{port} is already in use by PulseVision")
                return False
            _open_ports.add(port)
        with self._lock:
            self.port, self.baud = port, baud
            self.latest = None
            self.valid_count = self.malformed_count = self.reconnect_count = 0
            self.last_error = None
        self._stop.clear()
        self._set_status("connecting")
        self._thread = threading.Thread(target=self._run, name=f"serial-{port}", daemon=True)
        self._thread.start()
        return True

    def disconnect(self):
        """Stop reading and release the port."""
        thread = self._thread
        if thread is not None:
            self._stop.set()
            if thread is not threading.current_thread():
                thread.join(timeout=3)
            self._thread = None
        with self._lock:
            port, self.port = self.port, None
        if port is not None:
            with _open_ports_lock:
                _open_ports.discard(port)
        self._set_status("disconnected")

    # ---- state ----
    def snapshot(self):
        """Return a consistent copy of the reader's state for the UI and alert rules."""
        with self._lock:
            return {
                "port": self.port, "baud": self.baud, "status": self.status,
                "last_error": self.last_error, "latest": self.latest,
                "valid_count": self.valid_count, "malformed_count": self.malformed_count,
                "reconnect_count": self.reconnect_count,
                "thread_alive": self.is_running(),
            }

    def _set_status(self, status, error=None):
        with self._lock:
            self.status = status
            if error is not None:
                self.last_error = error

    def _switch_port_if_enumerated_elsewhere(self, current_port):
        """If Windows re-enumerated an unplugged/reset Arduino on a new COM port, switch to it."""
        if current_port == MOCK_PORT or self._port_detector is None:
            return current_port
        try:
            detected = self._port_detector()
        except Exception:  # noqa: BLE001
            return current_port
        if not detected or detected == current_port:
            return current_port
        with _open_ports_lock:
            if detected in _open_ports:
                return current_port
            _open_ports.discard(current_port)
            _open_ports.add(detected)
        with self._lock:
            self.port = detected
        log.info("Arduino re-enumerated on %s (was %s); switching port", detected, current_port)
        return detected

    # ---- background thread ----
    def _run(self):
        port, baud = self.port, self.baud
        log.info("Serial reader thread started for %s at %d baud", port, baud)
        first_attempt = True
        try:
            while not self._stop.is_set():
                conn = None
                try:
                    conn = self._factory(port, baud)
                    if hasattr(conn, "reset_input_buffer"):
                        try:
                            conn.reset_input_buffer()
                        except (serial.SerialException, OSError):
                            pass
                    with self._lock:
                        if not first_attempt:
                            self.reconnect_count += 1
                        self.status = "connected"
                    first_attempt = False
                    log.info("Connected to %s at %d baud", port, baud)
                    self._read_lines(conn)
                except (serial.SerialException, OSError) as exc:
                    if self._stop.is_set():
                        break
                    first_attempt = False
                    # Message only - never the data, which may be patient readings
                    log.warning("Serial error on %s: %s", port, exc)
                    self._set_status("reconnecting", str(exc))
                    if isinstance(exc, FileNotFoundError) or "cannot find the file" in str(exc).lower():
                        port = self._switch_port_if_enumerated_elsewhere(port)
                    self._stop.wait(self._reconnect_seconds)
                except Exception as exc:  # noqa: BLE001 - never let an unexpected exception kill the thread silently
                    if self._stop.is_set():
                        break
                    first_attempt = False
                    log.exception("Unexpected error in serial reader on %s: %s", port, exc)
                    self._set_status("reconnecting", f"Unexpected error: {exc}")
                    self._stop.wait(self._reconnect_seconds)
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:  # noqa: BLE001 - closing a dead port can fail
                            pass
        finally:
            reason = "stop requested" if self._stop.is_set() else "unexpected loop exit"
            log.info("Serial reader thread for %s terminated (%s)", port, reason)

    def _read_lines(self, conn):
        """Read from conn in chunks and yield complete newline-delimited lines.

        Chunked reading drains any backlog buffered by the OS when `in_waiting > 0`
        and avoids losing partial lines if a single `read()` times out mid-line.
        """
        buf = bytearray()
        while not self._stop.is_set():
            if hasattr(conn, "in_waiting"):
                waiting = conn.in_waiting
                chunk = conn.read(waiting if waiting > 0 else 1)
            else:
                chunk = conn.readline()
            if not chunk:
                continue  # timeout - lets us check the stop flag
            buf.extend(chunk)
            if len(buf) > MAX_LINE_LENGTH * 4 and b"\n" not in buf:
                buf.clear()
                with self._lock:
                    self.malformed_count += 1
                log.debug("Rejected oversized unterminated serial buffer")
                continue

            while b"\n" in buf:
                raw_line, _, remainder = buf.partition(b"\n")
                buf = bytearray(remainder)
                try:
                    reading = parse_line(raw_line.decode("utf-8", errors="strict"))
                except (UnicodeDecodeError, MalformedLine) as exc:
                    with self._lock:
                        self.malformed_count += 1
                    log.debug("Rejected malformed line (%d bytes): %s", len(raw_line), exc)
                    continue
                if reading is not None:
                    with self._lock:
                        self.latest = reading
                        self.valid_count += 1
