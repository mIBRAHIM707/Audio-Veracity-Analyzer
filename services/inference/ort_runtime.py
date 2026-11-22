"""The one way AVA imports ONNX Runtime: with its telemetry off (PRD "no runtime
downloads, telemetry, CDN assets or internet dependency"; §6.3).

ONNX Runtime's official builds turn telemetry on by default; on non-Windows platforms
it goes through the 1DS SDK, and `ORT_DISABLE_TELEMETRY=1` must be set before ONNX
Runtime initialises (onnxruntime docs/Privacy.md, read 17 Nov 2026:
https://raw.githubusercontent.com/microsoft/onnxruntime/main/docs/Privacy.md). Seen in
the Linux image on 17 Nov: onnxruntime 1.32.0 initialises its 1DS client at import
("telemetry_1ds ... device ID"). `onnxruntime()` therefore sets the variable before the
first import, whatever the environment said, and also calls the Python binding's
`disable_telemetry_events()`. Every module that needs ONNX Runtime imports it through
here; the live image also sets the variable, and the Compose network has no egress.

Verified (17 Nov): Linux / 1DS only. With the variable set before import, the 1DS
start-up message no longer appears. On Windows the official wheel uses its
TraceLogging/ETW backend, which the variable does not control; whether that backend
logs an event at import, before `disable_telemetry_events()` runs, was not checked.
Windows use here is development only (exports, benchmarks, augmentation with Silero);
serving runs in the Linux image.
"""
import os

ENV_VAR = 'ORT_DISABLE_TELEMETRY'


def onnxruntime():
    """Import and return the `onnxruntime` module with telemetry disabled."""
    os.environ[ENV_VAR] = '1'  # forced, not defaulted: telemetry is never wanted
    import onnxruntime as ort
    disable = getattr(ort, 'disable_telemetry_events', None)
    if callable(disable):
        disable()
    return ort


def quantization():
    """`onnxruntime.quantization`, imported after `onnxruntime()` has run."""
    import importlib
    onnxruntime()
    return importlib.import_module('onnxruntime.quantization')
