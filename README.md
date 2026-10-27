# Raspberry Pi Heating Control Project

This project reads multiple 1-Wire temperature sensors and controls an ESBE ARA639 (12520100) proportional shunt actuator through a PWM-to-0-10 V interface on Raspberry Pi BCM GPIO18 (physical pin 12).

## What is included

- `app.py` - Flask web application with dashboard, history graphs, setpoint control, and email alert support.
- `temperature.py` - Reads DS18B20 sensors from `/sys/bus/w1/devices`.
- `pid_controller.py` - Heating-circuit PID and bounded room-temperature integral correction.
- `history_store.py` - SQLite-backed persistent temperature and valve history.
- `config.json` - Example configuration for sensors, PID tuning, email alerts, and limits.
- `requirements.txt` - Python dependencies.

## Setup

1. Enable 1-Wire on the Raspberry Pi with `dtoverlay=w1-gpio,gpiopin=4` in `/boot/config.txt`.
2. Install the GPIO support libraries:

```bash
sudo apt update
sudo apt install -y swig liblgpio-dev
```

3. Create a virtual environment and install the dependencies:

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

The first command creates the `venv` directory; it will not exist until you run it. Run the dependency-install command separately.

4. Adjust `config.json` with your sensor IDs and email settings. The heating-circuit target starts with `target = -0.6 * outside_temperature + 26`, clamped to 15–44°C. Room-temperature integral correction is added to that target. The heating-circuit PID then controls the actuator output.

5. Start the app:

```bash
./venv/bin/python app.py
```

6. Open the dashboard in your browser:

```
http://<raspberry-pi-ip>:5000
```

## Notes

- `actuator_output` configures the PWM GPIO, frequency, 0–10 V scale, and optional signal inversion. Confirm that the PWM converter accepts 3.3 V GPIO PWM at the configured frequency and verify its voltage and direction with a meter before connecting the actuator. The current configuration enables GPIO18 output immediately when the app starts.
- The ARA639 requires its own correctly rated 24 V supply; do not power it from the Pi. Follow the ESBE wiring diagram, including signal common/isolation requirements.
- The configured room setpoint defaults to 20°C. Tune the room integral gain and heating-circuit PID gains carefully, initially observing operation continuously. The 44°C heating-circuit target maximum is a configured control limit, not a substitute for any lower limit required by the heating emitters or installation.
- Email alerts require SMTP credentials configured in `config.json`.
- If a required room, outdoor, or heating-circuit sensor reading is unavailable, the control loop holds the current PWM command and logs a warning.
- Temperature and valve samples are stored in `heating_history.sqlite3` beside the application. The database retains 90 days of data; the dashboard loads the latest `history.max_points` samples (288 by default) after restart. SQLite is included with Python, so no additional database service or package is needed.
- Use the dashboard's **Control tuning** section to update heating-circuit `kp`, `ki`, `kd`, and outdoor-curve `slope` and `intercept`. Values are applied immediately, the PID state is reset, and the settings are saved to `config.json` for the next restart. PID gains must be non-negative; all values must be finite.
- The dashboard's room setpoint is also saved to `config.json` and applied immediately. It is limited by `room_control.min_setpoint` and `room_control.max_setpoint`.
- The **Shunt valve** section switches between automatic control and manual mode. Manual opening is a 0–100% command (mapped to 0–10 V) and is saved with the selected mode in `config.json`. Verify the actuator/interface direction before relying on the displayed command as a physical opening percentage.
- The Flask dashboard currently has no authentication and listens on all network interfaces. Keep it on a trusted private network and do not expose port 5000 to the public internet; tuning values directly affect heating control.
