"""Web CLI commands for tweetxvault."""

import hashlib
import os
import signal
import subprocess
import sys
import time
from importlib import import_module
from pathlib import Path

import typer
from rich.console import Console

from tweetxvault.config import load_config, save_app_config

web_app = typer.Typer(no_args_is_help=True, help="Manage the background web UI server.")


def _get_pid_file(data_dir: Path) -> Path:
    return data_dir / ".web.pid"


def _read_pid(pid_file: Path) -> int | None:
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None
    return pid if pid > 0 else None


def _is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _positive_pids(output: str) -> list[int]:
    return [int(value) for value in output.split() if value.isdigit() and int(value) > 0]


def _find_pid_by_port(port: int) -> int | None:
    commands = (
        ["lsof", "-t", f"-i:{port}"],
        ["fuser", f"{port}/tcp"],
    )
    for command in commands:
        try:
            output = subprocess.check_output(
                command,
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if pids := _positive_pids(output):
            return pids[0]
    return None


def _require_web_dependencies(console: Console) -> None:
    try:
        import_module("tweetxvault.web.server")
        import_module("uvicorn")
    except ImportError as exc:
        console.print("[red]Web dependencies are missing. Run `uv sync --extra web` first.[/red]")
        raise typer.Exit(1) from exc


def _running_pid(pid_file: Path, port: int) -> int | None:
    pid = _read_pid(pid_file)
    if pid is not None and _is_running(pid):
        return pid
    pid = _find_pid_by_port(port)
    if pid is not None and _is_running(pid):
        return pid
    return None


@web_app.command("start", help="Start the background web server.")
def start_web() -> None:
    console = Console()
    _require_web_dependencies(console)
    config, paths = load_config()

    if not paths.database_path.is_file():
        console.print(
            "[red]Archive database not found. Sync or import an archive before starting "
            "the web server.[/red]"
        )
        raise typer.Exit(1)

    pid_file = _get_pid_file(paths.data_dir)
    recorded_pid = _read_pid(pid_file)
    if recorded_pid is not None and _is_running(recorded_pid):
        console.print(f"[yellow]Web server is already running (PID: {recorded_pid}).[/yellow]")
        raise typer.Exit(0)

    port_pid = _find_pid_by_port(config.web.port)
    if port_pid is not None and _is_running(port_pid):
        console.print(
            f"[red]Cannot start web server: port {config.web.port} is already in use "
            f"(PID: {port_pid}).[/red]"
        )
        raise typer.Exit(1)

    web_config = config.web
    default_password = "password"
    default_password_hash = hashlib.sha256(default_password.encode("utf-8")).hexdigest()

    if not web_config.password_hash:
        web_config.password_hash = default_password_hash
        config.web = web_config
        save_app_config(paths, config)
        console.print(f"[red]WARNING: Starting with default password '{default_password}'.[/red]")
        console.print("[yellow]Please change it using: tweetxvault web set-password[/yellow]")
    elif web_config.password_hash == default_password_hash:
        console.print(f"[red]WARNING: Using default password '{default_password}'.[/red]")
        console.print("[yellow]Please change it using: tweetxvault web set-password[/yellow]")

    console.print(f"Starting web server on http://{web_config.host}:{web_config.port} ...")

    command = [sys.executable, "-m", "tweetxvault", "serve-daemon"]
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        console.print(f"[red]Failed to start web server: {exc}[/red]")
        raise typer.Exit(1) from exc

    try:
        pid_file.write_text(str(process.pid), encoding="utf-8")
    except OSError as exc:
        try:
            os.kill(process.pid, signal.SIGTERM)
        except OSError:
            pass
        console.print(f"[red]Failed to record web server PID: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print(f"[green]Started background server (PID: {process.pid}).[/green]")


@web_app.command("stop", help="Stop the background web server.")
def stop_web() -> None:
    console = Console()
    config, paths = load_config()
    pid_file = _get_pid_file(paths.data_dir)
    pid = _running_pid(pid_file, config.web.port)

    if pid is None:
        console.print("[yellow]Web server is not running.[/yellow]")
        pid_file.unlink(missing_ok=True)
        return

    try:
        console.print(f"Stopping web server (PID: {pid})...")
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _is_running(pid):
                break
            time.sleep(0.1)
        else:
            os.kill(pid, signal.SIGKILL)
            time.sleep(0.5)
        console.print("[green]Server stopped.[/green]")
    except ProcessLookupError:
        console.print("[yellow]Web server is not running.[/yellow]")
    except Exception as exc:
        console.print(f"[red]Error stopping server: {exc}[/red]")
        raise typer.Exit(1) from exc
    finally:
        pid_file.unlink(missing_ok=True)


@web_app.command("status", help="Check if the background web server is running.")
def status_web() -> None:
    console = Console()
    config, paths = load_config()
    pid_file = _get_pid_file(paths.data_dir)
    web_config = config.web
    url = f"http://{web_config.host}:{web_config.port}"
    pid = _running_pid(pid_file, web_config.port)

    if pid is not None:
        console.print(f"Web server is [green]running[/green] (PID: {pid}).")
        console.print(f"URL: {url}")
        return

    console.print("Web server is [red]stopped[/red].")
    console.print(f"Configured URL: {url}")
    pid_file.unlink(missing_ok=True)
    raise typer.Exit(1)


@web_app.command("set-password", help="Set the password for the web server.")
def set_password() -> None:
    console = Console()
    config, paths = load_config()

    password = typer.prompt(
        "Enter new web password",
        hide_input=True,
        confirmation_prompt=True,
    )
    if not password:
        console.print("[red]Password cannot be empty.[/red]")
        raise typer.Exit(1)

    web_config = config.web
    web_config.password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    config.web = web_config
    try:
        save_app_config(paths, config)
    except OSError as exc:
        console.print(f"[red]Failed to save password: {exc}[/red]")
        raise typer.Exit(1) from exc
    console.print("[green]Password updated in config.toml.[/green]")
    console.print(
        "Note: If the server is currently running, you must restart it for the new "
        "password to take effect."
    )
    console.print("Run `tweetxvault web stop` and `tweetxvault web start`.")
