"""CLI for SLURM job submission and GUI management."""

import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import draccus

PROJECT_ROOT = Path.cwd()

PID_FILE = Path("/tmp/slurm-gui.pid")
LOG_FILE = Path("/tmp/slurm-gui.log")


@dataclass
class SlurmConfig:
    host: str = ""
    remote_path: str = ""
    command: str = ""
    time: int = 6
    gpu: str = "h100"
    ngpu: int = 1
    cpus: int = 16
    mem: str = "8G"
    partition: str = "short"
    priority: bool = False
    dry_run: bool = False
    envs: list[str] = field(default_factory=list)


def load_config() -> SlurmConfig:
    config_path = PROJECT_ROOT / "configs" / "slurm.yaml"
    if not config_path.exists():
        return SlurmConfig()
    return draccus.parse(SlurmConfig, config_path=config_path)


def build_sbatch_script(cfg: SlurmConfig) -> str:
    gres = f"gpu:{cfg.gpu}:{cfg.ngpu}" if cfg.gpu else f"gpu:{cfg.ngpu}"
    sbatch_opts = {
        "nodes": 1,
        "ntasks-per-node": cfg.cpus,
        "mem-per-cpu": cfg.mem,
        "time": f"{cfg.time}:00:00",
        "partition": cfg.partition,
        "gres": gres,
        "output": "slurm/slurm-%j.out",
    }
    if cfg.priority:
        sbatch_opts["qos"] = "priority"
    header = "\n".join(f"#SBATCH --{k}={v}" for k, v in sbatch_opts.items())
    if not cfg.command:
        print("Error: no command specified (set in yaml or pass --command)")
        sys.exit(1)
    exports = ""
    for var in cfg.envs:
        if var not in os.environ:
            print(f"Error: envs requested '{var}' but it is not set locally")
            sys.exit(1)
        exports += f"export {var}={shlex.quote(os.environ[var])}\n"
    return f"#!/bin/bash\n{header}\n\nset -euo pipefail\n{exports}{cfg.command}\n"


def sync(cfg: SlurmConfig) -> None:
    subprocess.run(
        [
            "rsync",
            "-avz",
            "--filter=:- .gitignore",
            f"{PROJECT_ROOT}/",
            f"{cfg.host}:{cfg.remote_path}",
        ],
        check=True,
    )


# --- Subcommands ---


def run() -> None:
    config_path = PROJECT_ROOT / "configs" / "slurm.yaml"
    if not config_path.exists():
        config_path = None
    cfg = draccus.parse(SlurmConfig, config_path=config_path)
    script = build_sbatch_script(cfg)
    if cfg.dry_run:
        print(script)
        return

    if not cfg.host or not cfg.remote_path:
        print("Error: host and remote_path must be set (in yaml or via CLI)")
        sys.exit(1)

    print("Syncing...")
    sync(cfg)

    print("Submitting...")
    subprocess.run(
        ["ssh", cfg.host, f"mkdir -p {cfg.remote_path}/slurm && cat > {cfg.remote_path}/submit.sh"],
        input=script,
        text=True,
        check=True,
    )
    subprocess.run(
        ["ssh", cfg.host, f"cd {cfg.remote_path} && sbatch submit.sh"],
        check=True,
    )


def read_pid() -> int | None:
    """Read PID from file and return it if the process is alive, else clean up."""
    if not PID_FILE.exists():
        return None
    pid = int(PID_FILE.read_text().strip())
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        PID_FILE.unlink()
        return None


def gui_start() -> None:
    if read_pid():
        print("GUI already running at http://127.0.0.1:5000")
        return

    pid = os.fork()
    if pid > 0:
        PID_FILE.write_text(str(pid))
        print("GUI started at http://127.0.0.1:5000")
        return

    os.setsid()
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    log = open(LOG_FILE, "w")  # noqa: SIM115
    os.dup2(log.fileno(), sys.stdout.fileno())
    os.dup2(log.fileno(), sys.stderr.fileno())
    os.close(devnull)

    from slurm_tools.gui.app import app

    app.run(host="127.0.0.1", port=5000)


def gui_stop() -> None:
    pid = read_pid()
    if not pid:
        print("GUI not running")
        return
    os.kill(pid, signal.SIGTERM)
    PID_FILE.unlink()
    print("GUI stopped")


def gui() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "stop":
        gui_stop()
    else:
        gui_start()


# --- CLI ---

SUBCOMMANDS = {"run": run, "gui": gui}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in SUBCOMMANDS:
        print(f"Usage: slurm <{'|'.join(SUBCOMMANDS)}> [options]")
        sys.exit(1 if len(sys.argv) >= 2 else 0)

    cmd = sys.argv.pop(1)
    SUBCOMMANDS[cmd]()


if __name__ == "__main__":
    main()
