import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, Optional


class HistoryStore:
    def __init__(
        self,
        database_path: Path,
        sensor_keys: Iterable[str],
        max_points: int,
        retention_days: int,
    ):
        self.database_path = database_path
        self.sensor_keys = tuple(sensor_keys)
        self.max_points = max_points
        self._lock = threading.Lock()
        self._closed = False
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    timestamp TEXT PRIMARY KEY,
                    temperatures TEXT NOT NULL,
                    valve_voltage REAL NOT NULL
                )
                """
            )
        cutoff = (
            datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(days=retention_days)
        ).isoformat()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM samples WHERE timestamp < ?", (cutoff,)
            )

    def _connect(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("HistoryStore is closed.")
        return sqlite3.connect(self.database_path, timeout=10)

    def append(
        self,
        timestamp: str,
        temperatures: Dict[str, Optional[float]],
        valve_voltage: float,
    ) -> None:
        stored_temperatures = {
            key: temperatures.get(key) for key in self.sensor_keys
        }
        with self._lock:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT OR REPLACE INTO samples (timestamp, temperatures, valve_voltage)
                    VALUES (?, ?, ?)
                    """,
                    (
                        timestamp,
                        json.dumps(stored_temperatures),
                        valve_voltage,
                    ),
                )

    def load_latest(self) -> dict:
        with self._lock:
            with self._connect() as connection:
                rows = connection.execute(
                    """
                    SELECT timestamp, temperatures, valve_voltage
                    FROM samples
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    (self.max_points,),
                ).fetchall()

        result = {key: [] for key in self.sensor_keys}
        result["valve_voltage"] = []
        for timestamp, temperatures_json, valve_voltage in reversed(rows):
            temperatures = json.loads(temperatures_json)
            for key in self.sensor_keys:
                result[key].append(
                    {"time": timestamp, "value": temperatures.get(key)}
                )
            result["valve_voltage"].append(
                {"time": timestamp, "value": valve_voltage}
            )
        return result

    def close(self) -> None:
        with self._lock:
            self._closed = True
