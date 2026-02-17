# iLO Control & Scheduler

A robust Python-based toolset for managing HPE servers via iLO 5/6 Redfish API. This project includes an interactive controller for manual operations and a scheduler for automated power management using cron.

## Features

### Interactive Controller (`ilo.py`)
- **Robust Connection Handling**: Handles TLS/SSL errors, timeouts, and transient disconnects gracefully.
- **Session Management**: Prefers Redfish session tokens over Basic Auth for better performance and compatibility.
- **Power Operations**:
    - Power On
    - Graceful Shutdown (ACPI)
    - Force Off (Hard Shutdown)
- **Status Monitoring**:
    - Real-time power state, system health, and model info.
    - Fan speeds (RPM, %, status).
- **Multi-Host Support**: Manage multiple servers from a single configuration.

### Scheduler (`time.py`)
- **Safety Preflight**: Enforces time synchronization (chrony/timedatectl/ntp) before allowing schedule creation to prevent unintended power events.
- **Cron Integration**: Manages user crontabs directly from the CLI.
- **Startup & Shutdown Schedules**: Easily create recurring power-on and power-off jobs.
- **Interactive Menu**: Simple text-based UI for managing schedules.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/alexpitcher/ilo-control.git
    cd ilo-control
    ```

2.  **Install dependencies:**
    ```bash
    pip install requests
    ```

3.  **Configure environment:**
    Create a `.env` file in the project root. You can copy the structure below:

    ```env
    # .env
    VERIFY_TLS=false
    REQUEST_TIMEOUT=10
    REQUEST_RETRIES=2
    SHUTDOWN_GRACE_PERIOD_SEC=600

    # JSON list of hosts. Multi-line JSON is supported.
    ILO_HOSTS=[
      {
        "name": "Server 1",
        "host": "192.168.1.100",
        "username": "admin",
        "password": "password123"
      },
      {
        "name": "Server 2",
        "host": "192.168.1.101",
        "username": "admin",
        "password": "password123"
      }
    ]
    ```

## Usage

### Interactive Controller
Run the controller to manage servers manually:

```bash
./ilo.py
```

Follow the on-screen menu to select a host or all hosts, and perform power or status actions.

### Scheduler
Run the scheduler to manage automated jobs:

```bash
./time.py
```

Options:
- **Time sync preflight**: Checks if your system clock is synchronized.
- **Create STARTUP/SHUTDOWN schedule**: interactive wizard to add cron jobs.
- **List/Delete jobs**: Manage existing schedules.
- **Test-run**: Execute a schedule immediately to verify behavior.

## Requirements
- Python 3.6+
- `requests` library
- Network access to iLO interfaces
