# Raspberry Pi Heating Control Project

This project reads multiple 1-Wire temperature sensors and controls an ESBE-style 0-10V shunt valve using an MCP4725 DAC.

## What is included

- `app.py` - Flask web application with dashboard, history graphs, setpoint control, and email alert support.
- `temperature.py` - Reads DS18B20 sensors from `/sys/bus/w1/devices`.
- `pid_controller.py` - Simple PID controller for house temperature.
- `dac_output.py` - MCP4725 output wrapper for 0-10V control.
- `config.json` - Example configuration for sensors, PID tuning, email alerts, and limits.
- `requirements.txt` - Python dependencies.

## Setup

1. Enable 1-Wire on the Raspberry Pi with `dtoverlay=w1-gpio,gpiopin=4` in `/boot/config.txt`.
2. Enable I2C in `raspi-config`.
3. Install SWIG and the `lgpio` development library, which are needed to build the Raspberry Pi GPIO dependency:

```bash
sudo apt update
sudo apt install -y swig liblgpio-dev
```

4. Create a virtual environment and install the dependencies:

```bash
python3 -m venv venv
./venv/bin/python -m pip install -r requirements.txt
```

The first command creates the `venv` directory; it will not exist until you run it. Run the dependency-install command separately.

5. Adjust `config.json` with your sensor IDs and email settings.

6. Start the app:

```bash
./venv/bin/python app.py
```

7. Open the dashboard in your browser:

```
http://<raspberry-pi-ip>:5000
```

## Notes

- The MCP4725 driver uses the I2C bus from `board.SCL` / `board.SDA`.
- Email alerts require SMTP credentials configured in `config.json`.
- The PID loop runs continuously and updates the valve every few seconds.
