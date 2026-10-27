import json
import logging
import math
import os
import smtplib
import tempfile
import threading
import time
from copy import deepcopy
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, jsonify, request

from pid_controller import PIDController, RoomIntegralCorrection
from temperature import TemperatureMonitor
from gpiozero import PWMOutputDevice

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

room_config = config["room_control"]
curve_config = config["heating_curve"]
pid_config = config["heating_circuit_pid"]
output_config = config["actuator_output"]
valve_control_config = config["valve_control"]

monitor = TemperatureMonitor(config["sensors"])
controller = PIDController(
    kp=pid_config["kp"],
    ki=pid_config["ki"],
    kd=pid_config["kd"],
    setpoint=0.0,
    output_min=pid_config["output_min"],
    output_max=pid_config["output_max"],
)
room_integral = RoomIntegralCorrection(
    gain_per_hour=room_config["integral_gain_per_hour"],
    correction_limit=room_config["max_correction"],
)
pwm = PWMOutputDevice(
    output_config["pwm_pin_bcm"],
    frequency=output_config["pwm_frequency_hz"],
    initial_value=0,
)

history = {
    key: deque(maxlen=config["history"]["max_points"]) for key in config["sensors"].keys()
}
history["valve_voltage"] = deque(maxlen=config["history"]["max_points"])

state = {
    "current_temps": {key: None for key in config["sensors"].keys()},
    "setpoint": room_config["setpoint"],
    "valve_voltage": 0.0,
    "base_heating_circuit_target": None,
    "room_correction": 0.0,
    "effective_setpoint": None,
    "valve_control_mode": valve_control_config["mode"],
    "manual_open_percent": valve_control_config["manual_open_percent"],
    "valve_open_percent": 0.0,
    "last_updated": None,
    "alert": None,
}

history_lock = threading.Lock()
email_lock = threading.Lock()
settings_lock = threading.Lock()
email_alerted = False

app = Flask(__name__)


def send_email(subject: str, body: str) -> None:
    if not config["email_alerts"]["enabled"]:
        return

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config["email_alerts"]["from_address"]
    message["To"] = config["email_alerts"]["to_address"]
    message.set_content(body)

    try:
        with smtplib.SMTP(config["email_alerts"]["smtp_host"], config["email_alerts"]["smtp_port"]) as smtp:
            if config["email_alerts"].get("use_tls", True):
                smtp.starttls()
            smtp.login(config["email_alerts"]["smtp_user"], config["email_alerts"]["smtp_password"])
            smtp.send_message(message)
        logging.info("Email alert sent: %s", subject)
    except Exception as exc:
        logging.exception("Failed to send email alert: %s", exc)


def check_alerts(temperatures: dict) -> None:
    global email_alerted
    if not config["email_alerts"]["enabled"]:
        return

    alerts = []
    limits = config.get("alerts", {})
    for label, temp in temperatures.items():
        if temp is None:
            alerts.append(f"Sensor {label} is not returning a temperature.")
            continue
        if label in limits:
            label_limits = limits[label]
            if temp < label_limits["min"] or temp > label_limits["max"]:
                alerts.append(
                    f"{label} temperature {temp:.1f}°C is outside allowed range "
                    f"({label_limits['min']:.1f}–{label_limits['max']:.1f})."
                )

    if alerts:
        alert_text = "\n".join(alerts)
        state["alert"] = alert_text
        if not email_alerted:
            send_email("Heating system alert", alert_text)
            email_alerted = True
    else:
        state["alert"] = None
        email_alerted = False


def add_history(temperatures: dict, valve_voltage: float) -> None:
    timestamp = datetime.utcnow().isoformat()
    with history_lock:
        for key, value in temperatures.items():
            history[key].append({"time": timestamp, "value": value})
        history["valve_voltage"].append({"time": timestamp, "value": valve_voltage})


def save_config(updated_config: dict) -> None:
    temporary_path = None
    try:
        file_descriptor, temporary_path = tempfile.mkstemp(
            prefix=f".{CONFIG_PATH.name}.",
            suffix=".tmp",
            dir=CONFIG_PATH.parent,
            text=True,
        )
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as config_file:
            os.chmod(temporary_path, CONFIG_PATH.stat().st_mode)
            json.dump(updated_config, config_file, indent=2)
            config_file.write("\n")
        os.replace(temporary_path, CONFIG_PATH)
    except OSError:
        if temporary_path is not None and os.path.exists(temporary_path):
            try:
                os.unlink(temporary_path)
            except OSError:
                logging.exception("Could not remove temporary config file")
        raise


def apply_valve_voltage(voltage: float) -> None:
    voltage = max(0.0, min(output_config["voltage_span"], voltage))
    duty_cycle = voltage / output_config["voltage_span"]
    if output_config["inverted"]:
        duty_cycle = 1.0 - duty_cycle
    pwm.value = max(0.0, min(1.0, duty_cycle))
    state["valve_voltage"] = voltage
    state["valve_open_percent"] = 100.0 * voltage / output_config["voltage_span"]


def control_loop() -> None:
    try:
        last_update = time.monotonic()
        while True:
            temperatures = monitor.read_all()
            state["current_temps"] = temperatures
            state["last_updated"] = datetime.utcnow().isoformat()
            now = time.monotonic()
            dt = max(now - last_update, 0.0)
            last_update = now

            check_alerts(temperatures)

            house_temp = temperatures.get("house")
            outside_temp = temperatures.get("outside")
            circuit_temp = temperatures.get("heating_circuit")
            missing_automatic_temperatures = False
            with settings_lock:
                if state["valve_control_mode"] == "manual":
                    manual_voltage = (
                        state["manual_open_percent"]
                        * output_config["voltage_span"]
                        / 100.0
                    )
                    apply_valve_voltage(manual_voltage)
                elif house_temp is not None and outside_temp is not None and circuit_temp is not None:
                    base_target = (
                        curve_config["slope"] * outside_temp + curve_config["intercept"]
                    )
                    base_target = max(
                        curve_config["min_target"],
                        min(curve_config["max_target"], base_target),
                    )
                    circuit_target = room_integral.update(
                        room_error=state["setpoint"] - house_temp,
                        dt=dt,
                        base_target=base_target,
                        target_min=curve_config["min_target"],
                        target_max=curve_config["max_target"],
                    )

                    controller.setpoint = circuit_target
                    output_voltage = controller.update(circuit_temp)
                    output_voltage = max(
                        pid_config["output_min"],
                        min(pid_config["output_max"], output_voltage),
                    )
                    apply_valve_voltage(output_voltage)
                    state["base_heating_circuit_target"] = base_target
                    state["room_correction"] = room_integral.correction
                    state["effective_setpoint"] = circuit_target
                else:
                    missing_automatic_temperatures = True
            if missing_automatic_temperatures:
                logging.warning(
                    "A required house, outside, or heating-circuit temperature is "
                    "unavailable in automatic mode; holding the current actuator command."
                )

            add_history(temperatures, state["valve_voltage"])
            time.sleep(config["loop_interval_seconds"])
    finally:
        pwm.off()
        pwm.close()

@app.route("/")
def index():
    return """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Raspberry Pi Heating Control</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 0; background: #f2f4f9; }
        .page { max-width: 1100px; margin: 0 auto; padding: 20px; }
        .header { display: flex; align-items: center; justify-content: space-between; }
        .card { background: white; border-radius: 12px; padding: 18px; box-shadow: 0 10px 30px rgba(0,0,0,0.08); margin-bottom: 20px; }
        .small { color: #666; font-size: 0.95rem; }
        .value { font-size: 2.4rem; margin: 0; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px,1fr)); gap: 16px; }
        .button { background: #2563eb; border: none; color: white; padding: 12px 18px; border-radius: 8px; cursor: pointer; }
        .button:hover { background: #1d4ed8; }
        input[type=number] { width: 100%; padding: 10px; border: 1px solid #d1d5db; border-radius: 8px; }
        .alert { color: #b91c1c; background: #fee2e2; border: 1px solid #fecaca; border-radius: 10px; padding: 12px; margin-top: 12px; }
        .connection-error { color: #991b1b; background: #fee2e2; border: 1px solid #fca5a5; border-radius: 10px; padding: 12px; margin-bottom: 20px; }
    </style>
</head>
<body>
    <div class="page">
        <div id="connectionError" class="connection-error" role="alert" style="display:none;">
            Lost connection to the heating controller. Displayed values may be out of date; retrying…
        </div>
        <div class="header">
            <div>
                <h1>Heating Control Dashboard</h1>
                <p class="small">House temperature, outside temperature, accumulator tank, heating circuit, PID control and email alerts.</p>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <p class="small">Current house temperature</p>
                <p class="value" id="houseTemp">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Outside temperature</p>
                <p class="value" id="outsideTemp">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Accumulator tank temperature</p>
                <p class="value" id="accumulatorTankTemp">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Heating circuit temperature</p>
                <p class="value" id="heatingCircuitTemp">-- °C</p>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <p class="small">Room target temperature</p>
                <p class="value" id="setpoint">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Heating circuit target</p>
                <p class="value" id="effectiveSetpoint">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Valve output</p>
                <p class="value" id="valveVoltage">-- V</p>
            </div>
            <div class="card" id="alertCard" style="display:none;">
                <p class="small">Alert</p>
                <p class="value" id="alertText">No alerts</p>
            </div>
        </div>

        <div class="card">
            <h2>Control</h2>
            <div class="grid">
                <div>
                    <label for="setpointInput">Setpoint (°C)</label>
                    <input id="setpointInput" type="number" step="0.1" min="5" max="30" />
                </div>
                <div style="align-self:end;">
                    <button class="button" onclick="updateSetpoint()">Update setpoint</button>
                </div>
            </div>
            <p id="setpointStatus" class="small" role="status"></p>
        </div>

        <div class="card">
            <h2>Shunt valve</h2>
            <div class="grid">
                <div>
                    <label for="valveModeInput">Control mode</label>
                    <select id="valveModeInput" onchange="updateManualControls()">
                        <option value="automatic">Automatic</option>
                        <option value="manual">Manual</option>
                    </select>
                </div>
                <div id="manualValveControl" style="display:none;">
                    <label for="manualOpenInput">Manual opening: <span id="manualOpenValue">0%</span></label>
                    <input id="manualOpenInput" type="range" min="0" max="100" step="1" value="0" oninput="updateManualOpenLabel()" />
                </div>
            </div>
            <p class="small">Valve command: <span id="valveOpenPercent">0%</span></p>
            <p id="valveControlStatus" class="small" role="status"></p>
            <button class="button" onclick="saveValveControl()">Apply valve mode</button>
        </div>

        <div class="card">
            <h2>Control tuning</h2>
            <p class="small">Changes are saved to config.json and applied immediately.</p>
            <div class="grid">
                <div>
                    <label for="kpInput">Heating-circuit Kp</label>
                    <input id="kpInput" type="number" step="any" min="0" />
                </div>
                <div>
                    <label for="kiInput">Heating-circuit Ki</label>
                    <input id="kiInput" type="number" step="any" min="0" />
                </div>
                <div>
                    <label for="kdInput">Heating-circuit Kd</label>
                    <input id="kdInput" type="number" step="any" min="0" />
                </div>
                <div>
                    <label for="slopeInput">Outdoor curve slope</label>
                    <input id="slopeInput" type="number" step="any" />
                </div>
                <div>
                    <label for="interceptInput">Outdoor curve intercept (°C)</label>
                    <input id="interceptInput" type="number" step="any" />
                </div>
            </div>
            <p id="controlSettingsStatus" class="small" role="status"></p>
            <button class="button" onclick="saveControlSettings()">Save tuning values</button>
        </div>

        <div class="card">
            <canvas id="historyChart" height="120"></canvas>
        </div>

        <div class="card">
            <h2>Email alert test</h2>
            <button class="button" onclick="sendTestEmail()">Send test email</button>
            <p class="small">Enable email alerts in config.json before using this.</p>
        </div>
    </div>

    <script>
        const labels = [];
        const chartData = {
            labels,
            datasets: [
                { label: 'House', borderColor: '#2563eb', backgroundColor: 'rgba(37,99,235,0.1)', data: [], tension: 0.3 },
                { label: 'Outside', borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,0.1)', data: [], tension: 0.3 },
                { label: 'Accumulator tank', borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.1)', data: [], tension: 0.3 },
                { label: 'Heating circuit', borderColor: '#ef4444', backgroundColor: 'rgba(239,68,68,0.1)', data: [], tension: 0.3 },
                { label: 'Valve voltage', borderColor: '#6b7280', backgroundColor: 'rgba(107,114,128,0.1)', data: [], tension: 0.3, yAxisID: 'B' }
            ]
        };

        const historyChart = new Chart(document.getElementById('historyChart').getContext('2d'), {
            type: 'line',
            data: chartData,
            options: {
                responsive: true,
                scales: {
                    A: { type: 'linear', position: 'left', title: { display: true, text: 'Temperature (°C)' } },
                    B: { type: 'linear', position: 'right', title: { display: true, text: 'Valve voltage (V)' }, min: 0, max: 10, grid: { drawOnChartArea: false } }
                },
                plugins: { legend: { position: 'bottom' } }
            }
        });

        async function updateDashboard() {
            const response = await fetch('/api/status');
            if (!response.ok) {
                throw new Error(`Status request failed (${response.status}).`);
            }
            const data = await response.json();
            document.getElementById('houseTemp').textContent = formatTemp(data.current_temps.house);
            document.getElementById('outsideTemp').textContent = formatTemp(data.current_temps.outside);
            document.getElementById('accumulatorTankTemp').textContent = formatTemp(data.current_temps.accumulator_tank);
            document.getElementById('heatingCircuitTemp').textContent = formatTemp(data.current_temps.heating_circuit);
            document.getElementById('setpoint').textContent = `${data.setpoint.toFixed(1)} °C`;
            document.getElementById('effectiveSetpoint').textContent = formatTemp(data.effective_setpoint);
            document.getElementById('valveVoltage').textContent = `${data.valve_voltage.toFixed(2)} V`;
            document.getElementById('valveOpenPercent').textContent = `${data.valve_open_percent.toFixed(0)}%`;
            const setpointInput = document.getElementById('setpointInput');
            if (document.activeElement !== setpointInput) {
                setpointInput.value = data.setpoint.toFixed(1);
            }

            if (data.alert) {
                document.getElementById('alertCard').style.display = 'block';
                document.getElementById('alertText').textContent = data.alert;
            } else {
                document.getElementById('alertCard').style.display = 'none';
            }
        }

        async function updateHistory() {
            const response = await fetch('/api/history');
            if (!response.ok) {
                throw new Error(`History request failed (${response.status}).`);
            }
            const data = await response.json();
            labels.length = 0;
            chartData.datasets.forEach(dataset => dataset.data.length = 0);

            const history = data.history;
            history.valve_voltage.forEach(entry => {
                labels.push(new Date(entry.time).toLocaleTimeString());
            });

            chartData.datasets[0].data.push(...history.house.map(entry => entry.value));
            chartData.datasets[1].data.push(...history.outside.map(entry => entry.value));
            chartData.datasets[2].data.push(...history.accumulator_tank.map(entry => entry.value));
            chartData.datasets[3].data.push(...history.heating_circuit.map(entry => entry.value));
            chartData.datasets[4].data.push(...history.valve_voltage.map(entry => entry.value));
            historyChart.update();
        }

        function formatTemp(value) {
            if (value === null || value === undefined || value === '') {
                return '-- °C';
            }
            const numericValue = Number(value);
            return Number.isFinite(numericValue) ? `${numericValue.toFixed(1)} °C` : '-- °C';
        }

        async function updateSetpoint() {
            const status = document.getElementById('setpointStatus');
            const value = document.getElementById('setpointInput').value;
            try {
                const response = await fetch('/api/setpoint', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ setpoint: value })
                });
                const result = await response.json();
                if (!response.ok) {
                    throw new Error(result.error || 'Could not save room setpoint.');
                }
                document.getElementById('setpointInput').value = result.setpoint.toFixed(1);
                status.textContent = 'Room setpoint saved and applied.';
                await updateDashboard();
            } catch (error) {
                status.textContent = error.message;
            }
        }

        async function loadControlSettings() {
            const response = await fetch('/api/control-settings');
            if (!response.ok) {
                throw new Error('Could not load control tuning values.');
            }
            const settings = await response.json();
            document.getElementById('kpInput').value = settings.kp;
            document.getElementById('kiInput').value = settings.ki;
            document.getElementById('kdInput').value = settings.kd;
            document.getElementById('slopeInput').value = settings.slope;
            document.getElementById('interceptInput').value = settings.intercept;
        }

        async function saveControlSettings() {
            const status = document.getElementById('controlSettingsStatus');
            const values = {
                kp: document.getElementById('kpInput').value,
                ki: document.getElementById('kiInput').value,
                kd: document.getElementById('kdInput').value,
                slope: document.getElementById('slopeInput').value,
                intercept: document.getElementById('interceptInput').value
            };

            try {
                const response = await fetch('/api/control-settings', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(values)
                });
                const result = await response.json();
                if (!response.ok) {
                    throw new Error(result.error || 'Could not save control tuning values.');
                }
                status.textContent = 'Tuning values saved and applied.';
            } catch (error) {
                status.textContent = error.message;
            }
        }

        function updateManualControls() {
            const manual = document.getElementById('valveModeInput').value === 'manual';
            document.getElementById('manualValveControl').style.display = manual ? 'block' : 'none';
        }

        function updateManualOpenLabel() {
            document.getElementById('manualOpenValue').textContent =
                `${document.getElementById('manualOpenInput').value}%`;
        }

        async function loadValveControl() {
            const response = await fetch('/api/valve-control');
            if (!response.ok) {
                throw new Error('Could not load valve control settings.');
            }
            const settings = await response.json();
            document.getElementById('valveModeInput').value = settings.mode;
            document.getElementById('manualOpenInput').value = settings.manual_open_percent;
            updateManualControls();
            updateManualOpenLabel();
        }

        async function saveValveControl() {
            const status = document.getElementById('valveControlStatus');
            const values = {
                mode: document.getElementById('valveModeInput').value,
                manual_open_percent: document.getElementById('manualOpenInput').value
            };

            try {
                const response = await fetch('/api/valve-control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(values)
                });
                const result = await response.json();
                if (!response.ok) {
                    throw new Error(result.error || 'Could not apply valve control settings.');
                }
                document.getElementById('valveModeInput').value = result.mode;
                document.getElementById('manualOpenInput').value = result.manual_open_percent;
                updateManualControls();
                updateManualOpenLabel();
                status.textContent = result.mode === 'manual'
                    ? `Manual mode active at ${result.manual_open_percent}% command.`
                    : 'Automatic mode active.';
                await updateDashboard();
            } catch (error) {
                status.textContent = error.message;
            }
        }

        async function sendTestEmail() {
            await fetch('/api/email-test', { method: 'POST' });
            alert('Email test request sent. Check logs and inbox.');
        }

        async function refresh() {
            const connectionError = document.getElementById('connectionError');
            try {
                await updateDashboard();
                await updateHistory();
                connectionError.style.display = 'none';
            } catch (error) {
                console.error('Dashboard refresh failed:', error);
                connectionError.style.display = 'block';
            }
        }

        setInterval(refresh, 5000);
        refresh();
        loadControlSettings().catch(error => {
            document.getElementById('controlSettingsStatus').textContent = error.message;
        });
        loadValveControl().catch(error => {
            document.getElementById('valveControlStatus').textContent = error.message;
        });
    </script>
</body>
</html>"""


@app.route("/api/status")
def api_status():
    return jsonify(state)


@app.route("/api/valve-control", methods=["GET", "POST"])
def api_valve_control():
    if request.method == "GET":
        with settings_lock:
            return jsonify(
                {
                    "mode": state["valve_control_mode"],
                    "manual_open_percent": state["manual_open_percent"],
                }
            )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "A JSON object is required."}), 400

    mode = payload.get("mode")
    if mode not in ("automatic", "manual"):
        return jsonify(
            {"success": False, "error": "Mode must be 'automatic' or 'manual'."}
        ), 400
    if isinstance(payload.get("manual_open_percent"), bool):
        return jsonify(
            {"success": False, "error": "Manual opening must be a number from 0 to 100."}
        ), 400

    try:
        manual_open_percent = float(payload["manual_open_percent"])
        if not math.isfinite(manual_open_percent) or not 0.0 <= manual_open_percent <= 100.0:
            raise ValueError("Manual opening must be between 0 and 100 percent.")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    with settings_lock:
        updated_config = deepcopy(config)
        updated_config["valve_control"].update(
            {
                "mode": mode,
                "manual_open_percent": manual_open_percent,
            }
        )
        try:
            save_config(updated_config)
        except OSError:
            logging.exception("Could not save valve control settings to config.json")
            return jsonify(
                {"success": False, "error": "Could not save valve control settings."}
            ), 500

        config["valve_control"].update(updated_config["valve_control"])
        valve_control_config.update(updated_config["valve_control"])
        state["valve_control_mode"] = mode
        state["manual_open_percent"] = manual_open_percent
        if mode == "manual":
            manual_voltage = manual_open_percent * output_config["voltage_span"] / 100.0
            apply_valve_voltage(manual_voltage)
        state["effective_setpoint"] = None
        state["base_heating_circuit_target"] = None
        state["room_correction"] = 0.0
        controller.reset()
        room_integral.reset()

    return jsonify(
        {
            "success": True,
            "mode": mode,
            "manual_open_percent": manual_open_percent,
        }
    )


@app.route("/api/control-settings", methods=["GET", "POST"])
def api_control_settings():
    if request.method == "GET":
        with settings_lock:
            return jsonify(
                {
                    "kp": pid_config["kp"],
                    "ki": pid_config["ki"],
                    "kd": pid_config["kd"],
                    "slope": curve_config["slope"],
                    "intercept": curve_config["intercept"],
                }
            )

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"success": False, "error": "A JSON object is required."}), 400

    try:
        tuning_keys = ("kp", "ki", "kd", "slope", "intercept")
        if any(isinstance(payload.get(key), bool) for key in tuning_keys):
            raise ValueError("Tuning values must be numeric, not booleans.")
        values = {
            key: float(payload[key])
            for key in tuning_keys
        }
        if not all(math.isfinite(value) for value in values.values()):
            raise ValueError("All tuning values must be finite numbers.")
        if any(values[key] < 0 for key in ("kp", "ki", "kd")):
            raise ValueError("PID gains must be zero or greater.")
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    with settings_lock:
        updated_config = deepcopy(config)
        updated_config["heating_circuit_pid"].update(
            {key: values[key] for key in ("kp", "ki", "kd")}
        )
        updated_config["heating_curve"].update(
            {key: values[key] for key in ("slope", "intercept")}
        )

        try:
            save_config(updated_config)
        except OSError:
            logging.exception("Could not save control tuning values to config.json")
            return jsonify(
                {"success": False, "error": "Could not save settings to config.json."}
            ), 500

        pid_config.update(updated_config["heating_circuit_pid"])
        curve_config.update(updated_config["heating_curve"])
        controller.kp = values["kp"]
        controller.ki = values["ki"]
        controller.kd = values["kd"]
        controller.reset()

    return jsonify({"success": True, **values})


@app.route("/api/history")
def api_history():
    with history_lock:
        return jsonify({"history": {k: list(v) for k, v in history.items()}})


@app.route("/api/setpoint", methods=["POST"])
def api_setpoint():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or isinstance(payload.get("setpoint"), bool):
        return jsonify({"success": False, "error": "A numeric setpoint is required."}), 400

    try:
        setpoint = float(payload["setpoint"])
        if not math.isfinite(setpoint):
            raise ValueError("Setpoint must be a finite number.")
        if not room_config["min_setpoint"] <= setpoint <= room_config["max_setpoint"]:
            raise ValueError(
                f"Setpoint must be between {room_config['min_setpoint']} and "
                f"{room_config['max_setpoint']} °C."
            )
    except (KeyError, TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    with settings_lock:
        updated_config = deepcopy(config)
        updated_config["room_control"]["setpoint"] = setpoint
        try:
            save_config(updated_config)
        except OSError:
            logging.exception("Could not save room setpoint to config.json")
            return jsonify(
                {"success": False, "error": "Could not save room setpoint to config.json."}
            ), 500

        config["room_control"]["setpoint"] = setpoint
        room_config["setpoint"] = setpoint
        state["setpoint"] = setpoint
        controller.reset()
        room_integral.reset()

    return jsonify({"success": True, "setpoint": setpoint})


@app.route("/api/email-test", methods=["POST"])
def api_email_test():
    try:
        send_email("Heating system test", "This is a test email from the Raspberry Pi heating controller.")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


def main() -> None:
    worker = threading.Thread(target=control_loop, daemon=True)
    worker.start()
    app.run(host="0.0.0.0", port=config["web_port"], debug=False)


if __name__ == "__main__":
    main()
