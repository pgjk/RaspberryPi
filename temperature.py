from pathlib import Path
from typing import Dict, Optional


class TemperatureMonitor:
    def __init__(self, sensor_map: Dict[str, str], device_root: Path = Path("/sys/bus/w1/devices")):
        self.sensor_map = sensor_map
        self.device_root = device_root

    def _read_sensor(self, sensor_id: str) -> Optional[float]:
        sensor_path = self.device_root / sensor_id / "w1_slave"
        try:
            with open(sensor_path, "r", encoding="utf-8") as f:
                lines = f.readlines()

            if len(lines) < 2 or not lines[0].strip().endswith("YES"):
                return None

            temp_pos = lines[1].find("t=")
            if temp_pos == -1:
                return None

            temp_string = lines[1][temp_pos + 2 :].strip()
            return float(temp_string) / 1000.0
        except FileNotFoundError:
            return None
        except Exception:
            return None

    def read_all(self) -> Dict[str, Optional[float]]:
        return {label: self._read_sensor(sensor_id) for label, sensor_id in self.sensor_map.items()}
