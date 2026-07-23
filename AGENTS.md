# Repository Guidelines

## Project Structure & Module Organization

This repository contains a Windows-to-ESP32 system monitor split into two active modules:

- `firmware/BluetoothSystemMonitor/`: ESP32 Arduino firmware for the TFT display and USB/Bluetooth serial packet handling.
- `agent/windows/`: Windows agent, WebUI, tray app, hardware metric collection, packaged EXE output, and vendor DLLs.
- `firmware/tft_setup_examples/`: TFT_eSPI setup files for supported display wiring.
- `scripts/arduino.ps1`: Arduino CLI helper used for firmware compile/upload workflows.
- `upstream/`: reference copy of the original Bluetooth System Monitor project; do not edit unless intentionally syncing upstream behavior.

Generated or local tool folders such as `.venv/`, `.arduino/`, `.tools/`, `agent/windows/build/`, and `agent/windows/dist/` should be treated as build/runtime artifacts.

## Build, Test, and Development Commands

Install Windows agent dependencies:

```powershell
python -m pip install -r agent\windows\requirements.txt
```

Run the agent WebUI locally:

```powershell
python agent\windows\windows_agent.py --web
```

Check Python syntax before packaging:

```powershell
python -m py_compile agent\windows\windows_agent.py
```

Compile firmware from the short ASCII build workspace:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\arduino.ps1 compile --fqbn esp32:esp32:esp32 C:\tmp\bsmon\firmware\BluetoothSystemMonitor
```

Build the Windows EXE with PyInstaller from `agent/windows/`; include the sensor DLLs next to `windows_agent.py`.

## Coding Style & Naming Conventions

Use 4-space indentation for Python and keep functions focused around one responsibility. Prefer `snake_case` for Python functions/variables and `PascalCase` only for classes. Arduino firmware follows the existing `.ino` style: small helper functions, `camelCase` names, and constants declared near the top.

Keep serial protocol changes backward-aware. The main packet format is:

```text
V2|ip|yyyy-mm-dd HH:mm:ss|label=value
```

## Testing Guidelines

There is no dedicated test suite yet. Validate changes with `py_compile`, firmware compilation, and hardware checks on COM3 or the detected USB serial port. For UI changes, verify `http://127.0.0.1:8765/state` and the WebUI preview. For firmware drawing changes, confirm no full-screen flicker during one-second updates.

## Commit & Pull Request Guidelines

Git history was not available in this workspace, so use short imperative commit messages, for example `Add tray icon for WebUI`. Pull requests should describe firmware vs. agent changes separately, list manual validation steps, mention the tested COM port/board, and include screenshots or photos for display/UI changes.

## Security & Configuration Tips

Do not commit real OpenAI admin keys, local `config.json` secrets, or personal COM-port assumptions. Keep vendor DLL updates explicit and document their source/version when changed.
