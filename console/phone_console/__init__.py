"""Python console for the ESP32-S3 HID hand."""

__version__ = "0.1.0"

from .device import Device, DeviceError, DeviceTimeout

__all__ = ["Device", "DeviceError", "DeviceTimeout", "__version__"]
