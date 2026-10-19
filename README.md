# Raspberry Pi Heating Control Project

This project reads multiple 1-Wire temperature sensors and controls an ESBE ARA639 (12520100) proportional shunt actuator through a PWM-to-0-10 V interface on Raspberry Pi BCM GPIO18 (physical pin 12).

## What is included

- `app.py` - Flask web application with dashboard, history graphs, setpoint control, and email alert support.
- `temperature.py` - Reads DS18B20 sensors from `/sys/bus/w1/devices`.
- `pid_controller.py` - Heating-circuit PID and bounded room-temperature integral correction.
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
