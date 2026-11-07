# Windows USB System Monitor



This project is split into:
- `firmware/BluetoothSystemMonitor`: ESP32 + ILI9488 TFT firmware.
- `agent/windows`: Windows-only agent that sends metrics to the ESP32 over USB serial every second.

The firmware expects one V2 packet per line:

```text
V2|title|yyyy-mm-dd HH:mm:ss|label=value|label=value
```

It also accepts this command to force the screen to redraw the current layout:

```text
CMD|RELOAD
```

Example:

```text
V2|Windows Monitor|2026-07-23 22:30:15|CPU Temp=62 C|Fan=35%|CPU Load=18%|Memory=12.4/31.8 GB
```

Only changed text areas are redrawn, so normal one-second updates do not clear the whole display.

## Firmware

1. Install Arduino IDE or Arduino CLI.
2. Install the ESP32 Arduino core.
3. Install the `TFT_eSPI` library.
4. Copy the matching setup file from `firmware/tft_setup_examples` into the `TFT_eSPI` library as `User_Setup.h`.
5. Open `firmware/BluetoothSystemMonitor/BluetoothSystemMonitor.ino`.
6. Select the ESP32 board and the USB serial port, then upload.

The firmware listens on USB serial at `115200` baud. It also starts Bluetooth serial as `SystemMonitor`.

On Windows, keep the Arduino build path short and ASCII-only. ESP32 toolchains may fail from long or non-ASCII folders. This workspace was verified from `C:\tmp\bsmon` with ESP32 Arduino core `2.0.17`.

Before uploading, the ESP32 must appear as a real USB serial device such as `Silicon Labs CP210x USB to UART Bridge`, `CH340`, or `USB Serial`. Bluetooth COM ports cannot be used for firmware upload.

## Windows Agent

Run from an administrator terminal when you need CPU/GPU temperature and fan data:

```powershell
cd agent\windows
python -m pip install -r requirements.txt
python .\windows_agent.py --port COM5
```

If `--port` is omitted, the agent lists detected serial ports and asks you to select one.

`OpenHardwareMonitorLib.dll` must stay next to `windows_agent.py`. In the packaged EXE it is bundled automatically. If hardware sensors are unavailable, the agent still sends CPU load, memory and C drive usage, but temperatures/fan/GPU may show `0`.

## EXE

The packaged app is built to:

```text
agent\windows\dist\SystemMonitorAgent.exe
```

Double-click it to start the tray agent. The agent starts sending with the saved settings, hosts the browser-based control UI at `http://127.0.0.1:8765`, and shows a system tray icon. Left-click the tray icon to open the WebUI; right-click it for Open WebUI, Restart Agent, Stop Agent, and Quit. If the agent is already running, opening the EXE again only opens the existing WebUI and exits.

The UI can:

- Select the COM port.
- Set refresh frequency.
- Choose which fields are shown.
- Edit field labels and drag fields in the screen preview to change their display positions.
- Add an OpenAI Admin Key and budgets to show weekly/monthly API spend.
- Enable or disable startup with Windows.
- Restart the agent, saving settings and forcing the display to redraw.
- Stop sending data or quit the control UI.

The legacy background mode still works, but it does not show the tray icon:

```powershell
agent\windows\dist\SystemMonitorAgent.exe --background
```

## Sponsors
Sponsored by [ForZTN](https://sponsorship.forztn.com/github.com/baixiaoshengofficial/System-Monitor).

