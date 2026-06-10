"""Web CLI commands for tweetxvault."""

import hashlib
import os
import signal
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from tweetxvault.config import WebConfig, load_config, update_web_config

web_app = typer.Typer(no_args_is_help=True, help="Manage the background web UI server.")

def _get_pid_file(data_dir: Path) -> Path:
    return data_dir / ".web.pid"

def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

@web_app.command("start", help="Start the background web server.")
def start_web() -> None:
    console = Console()
    try:
        from tweetxvault.web.server import run_server
    except ImportError:
        console.print("[red]Web dependencies are missing. Run `uv sync --extra web` first.[/red]")
        raise typer.Exit(1)

    config, paths = load_config()
    
    pid_file = _get_pid_file(paths.data_dir)
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if _is_running(old_pid):
                console.print(f"[yellow]Web server is already running (PID: {old_pid}).[/yellow]")
                raise typer.Exit(0)
        except ValueError:
            pass

    # Ensure a password is set, and warn if the default password is used
    web_config = config.web
    default_password = "password"

    if not web_config.password_hash:
        web_config.password_hash = hashlib.sha256(default_password.encode("utf-8")).hexdigest()
        update_web_config(paths, web_config)
        console.print(f"[red]WARNING: Starting with default password '{default_password}'.[/red]")
        console.print("[yellow]Please change it using: tweetxvault web set-password[/yellow]")
    elif web_config.password_hash == hashlib.sha256(default_password.encode("utf-8")).hexdigest():
        console.print(f"[red]WARNING: Using default password '{default_password}'.[/red]")
        console.print("[yellow]Please change it using: tweetxvault web set-password[/yellow]")

    console.print(f"Starting web server on http://{web_config.host}:{web_config.port} ...")
    
    # Spawn background process
    # We call tweetxvault serve-daemon so it runs the actual uvicorn logic
    cmd = [sys.executable, "-m", "tweetxvault", "serve-daemon"]
    
    # Using start_new_session to detach the process
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    
    pid_file.write_text(str(process.pid))
    console.print(f"[green]Started background server (PID: {process.pid}).[/green]")

@web_app.command("stop", help="Stop the background web server.")
def stop_web() -> None:
    console = Console()
    _, paths = load_config()
    pid_file = _get_pid_file(paths.data_dir)
    
    if not pid_file.exists():
        console.print("[yellow]Web server is not running (no PID file found).[/yellow]")
        return
        
    try:
        pid = int(pid_file.read_text().strip())
        if _is_running(pid):
            console.print(f"Stopping web server (PID: {pid})...")
            os.kill(pid, signal.SIGTERM)
            console.print("[green]Server stopped.[/green]")
        else:
            console.print("[yellow]Server is not running (stale PID file).[/yellow]")
    except ValueError:
        console.print("[red]Invalid PID file.[/red]")
    except ProcessLookupError:
        console.print("[yellow]Server is not running.[/yellow]")
    except Exception as e:
        console.print(f"[red]Error stopping server: {e}[/red]")
    finally:
        if pid_file.exists():
            pid_file.unlink()

@web_app.command("status", help="Check if the background web server is running.")
def status_web() -> None:
    console = Console()
    config, paths = load_config()
    pid_file = _get_pid_file(paths.data_dir)
    
    web_config = config.web
    url = f"http://{web_config.host}:{web_config.port}"
    
    if not pid_file.exists():
        console.print(f"Web server is [red]stopped[/red].")
        console.print(f"Configured URL: {url}")
        return
        
    try:
        pid = int(pid_file.read_text().strip())
        if _is_running(pid):
            console.print(f"Web server is [green]running[/green] (PID: {pid}).")
            console.print(f"URL: {url}")
        else:
            console.print(f"Web server is [red]stopped[/red] (stale PID file).")
            console.print(f"Configured URL: {url}")
            pid_file.unlink()
    except ValueError:
        console.print(f"Web server is [red]stopped[/red] (invalid PID file).")
        pid_file.unlink()

@web_app.command("set-password", help="Set the password for the web server.")
def set_password() -> None:
    console = Console()
    config, paths = load_config()
    
    password = typer.prompt("Enter new web password", hide_input=True, confirmation_prompt=True)
    if not password:
        console.print("[red]Password cannot be empty.[/red]")
        raise typer.Exit(1)
        
    web_config = config.web
    web_config.password_hash = hashlib.sha256(password.encode("utf-8")).hexdigest()
    update_web_config(paths, web_config)
    console.print("[green]Password updated in config.toml.[/green]")
    console.print("Note: If the server is currently running, you must restart it for the new password to take effect.")
    console.print("Run `tweetxvault web stop` and `tweetxvault web start`.")
