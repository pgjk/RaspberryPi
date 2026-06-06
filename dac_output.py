from dataclasses import dataclass

try:
    import board
    import busio
    import adafruit_mcp4725
    _HARDWARE_AVAILABLE = True
except ImportError:
    _HARDWARE_AVAILABLE = False


@dataclass
class MCP4725Valve:
    address: int = 0x60
    voltage_span: float = 10.0

    def __post_init__(self):
        self._value = 0.0
        if _HARDWARE_AVAILABLE:
            i2c = busio.I2C(board.SCL, board.SDA)
            self._device = adafruit_mcp4725.MCP4725(i2c, address=self.address)
        else:
            self._device = None

    def set_voltage(self, voltage: float) -> None:
        voltage = max(0.0, min(self.voltage_span, voltage))
        self._value = voltage
        if self._device is not None:
            raw_value = int((voltage / self.voltage_span) * 65535)
            self._device.raw_value = raw_value

    @property
    def last_voltage(self) -> float:
        return self._value
