import json
import logging
import smtplib
import threading
import time
from collections import deque
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

from flask import Flask, jsonify, request

from dac_output import MCP4725Valve
from pid_controller import PIDController
from temperature import TemperatureMonitor

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

with open(CONFIG_PATH, "r", encoding="utf-8") as f:
    config = json.load(f)

monitor = TemperatureMonitor(config["sensors"])
controller = PIDController(
    kp=config["pid"]["kp"],
    ki=config["pid"]["ki"],
    kd=config["pid"]["kd"],
    setpoint=config["pid"]["setpoint"],
    output_min=config["pid"]["output_min"],
    output_max=config["pid"]["output_max"],
)
valve = MCP4725Valve(address=config["dac"]["address"], voltage_span=config["dac"]["voltage_span"])

history = {
    key: deque(maxlen=config["history"]["max_points"]) for key in config["sensors"].keys()
}
history["valve_voltage"] = deque(maxlen=config["history"]["max_points"])

state = {
    "current_temps": {key: None for key in config["sensors"].keys()},
    "setpoint": config["pid"]["setpoint"],
    "valve_voltage": 0.0,
    "last_updated": None,
    "alert": None,
}

history_lock = threading.Lock()
email_lock = threading.Lock()
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


def control_loop() -> None:
    while True:
        temperatures = monitor.read_all()
        state["current_temps"] = temperatures
        state["last_updated"] = datetime.utcnow().isoformat()

        check_alerts(temperatures)

        house_temp = temperatures.get("house")
        if house_temp is not None:
            controller.setpoint = state["setpoint"]
            output_voltage = controller.update(house_temp)
            valve.set_voltage(output_voltage)
            state["valve_voltage"] = output_voltage
        else:
            logging.warning("House temperature is unavailable; valve output is held at current value.")

        add_history(temperatures, state["valve_voltage"])
        time.sleep(config["loop_interval_seconds"])


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
    </style>
</head>
<body>
    <div class="page">
        <div class="header">
            <div>
                <h1>Heating Control Dashboard</h1>
                <p class="small">House temperature, outside temperature, heat exchanger, heating circuit, PID control and email alerts.</p>
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
                <p class="small">Heat exchanger temperature</p>
                <p class="value" id="heatExchangerTemp">-- °C</p>
            </div>
            <div class="card">
                <p class="small">Heating circuit temperature</p>
                <p class="value" id="heatingCircuitTemp">-- °C</p>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <p class="small">Target temperature</p>
                <p class="value" id="setpoint">-- °C</p>
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
                    <input id="setpointInput" type="number" step="0.1" min="0" max="80" />
                </div>
                <div style="align-self:end;">
                    <button class="button" onclick="updateSetpoint()">Update setpoint</button>
                </div>
            </div>
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
                { label: 'Heat exchanger', borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.1)', data: [], tension: 0.3 },
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
            const data = await response.json();
            document.getElementById('houseTemp').textContent = formatTemp(data.current_temps.house);
            document.getElementById('outsideTemp').textContent = formatTemp(data.current_temps.outside);
            document.getElementById('heatExchangerTemp').textContent = formatTemp(data.current_temps.heat_exchanger);
            document.getElementById('heatingCircuitTemp').textContent = formatTemp(data.current_temps.heating_circuit);
            document.getElementById('setpoint').textContent = `${data.setpoint.toFixed(1)} °C`;
            document.getElementById('valveVoltage').textContent = `${data.valve_voltage.toFixed(2)} V`;
            document.getElementById('setpointInput').value = data.setpoint.toFixed(1);

            if (data.alert) {
                document.getElementById('alertCard').style.display = 'block';
                document.getElementById('alertText').textContent = data.alert;
            } else {
                document.getElementById('alertCard').style.display = 'none';
            }
        }

        async function updateHistory() {
            const response = await fetch('/api/history');
            const data = await response.json();
            labels.length = 0;
            chartData.datasets.forEach(dataset => dataset.data.length = 0);

            const history = data.history;
            history.valve_voltage.forEach(entry => {
                labels.push(new Date(entry.time).toLocaleTimeString());
            });

            chartData.datasets[0].data.push(...history.house.map(entry => entry.value));
            chartData.datasets[1].data.push(...history.outside.map(entry => entry.value));
            chartData.datasets[2].data.push(...history.heat_exchanger.map(entry => entry.value));
            chartData.datasets[3].data.push(...history.heating_circuit.map(entry => entry.value));
            chartData.datasets[4].data.push(...history.valve_voltage.map(entry => entry.value));
            historyChart.update();
        }

        function formatTemp(value) {
            return value === null ? '-- °C' : `${value.toFixed(1)} °C`;
        }

        async function updateSetpoint() {
            const value = Number(document.getElementById('setpointInput').value);
            await fetch('/api/setpoint', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ setpoint: value })
            });
            updateDashboard();
        }

        async function sendTestEmail() {
            await fetch('/api/email-test', { method: 'POST' });
            alert('Email test request sent. Check logs and inbox.');
        }

        async function refresh() {
            await updateDashboard();
            await updateHistory();
        }

        setInterval(refresh, 5000);
        refresh();
    </script>
</body>
</html>"""


@app.route("/api/status")
def api_status():
    return jsonify(state)


@app.route("/api/history")
def api_history():
    with history_lock:
        return jsonify({"history": {k: list(v) for k, v in history.items()}})


@app.route("/api/setpoint", methods=["POST"])
def api_setpoint():
    payload = request.get_json(force=True)
    try:
        state["setpoint"] = float(payload["setpoint"])
        controller.reset()
        return jsonify({"success": True, "setpoint": state["setpoint"]})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400


@app.route("/api/email-test", methods=["POST"])
def api_email_test():
    try:
        send_email("Heating system test", "This is a test email from the Raspberry Pi heating controller.")
        return jsonify({"success": True})
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 500


if __name__ == "__main__":
    worker = threading.Thread(target=control_loop, daemon=True)
    worker.start()
    app.run(host="0.0.0.0", port=config["web_port"], debug=False)
