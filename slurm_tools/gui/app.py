"""SLURM experiment monitor — lightweight web GUI."""

import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from flask import Flask, Response, render_template, request

from slurm_tools.slurm import SlurmConfig, load_clusters

CLUSTERS: list[tuple[str, SlurmConfig]] = [
    (c.name or c.host or f"cluster-{i + 1}", c)
    for i, c in enumerate(load_clusters())
]
CLUSTER_MAP = dict(CLUSTERS)
CLUSTER_NAMES = [name for name, _ in CLUSTERS]

GPU_MEM_RE = re.compile(r"gpu_mem:(\d+)GB", re.IGNORECASE)

dir = Path(__file__).parent
app = Flask(
    __name__,
    template_folder=str(dir),
    static_folder=str(dir),
    static_url_path="/static",
)


def ssh(cluster: SlurmConfig, cmd: str, *, timeout: int = 10) -> str:
    result = subprocess.run(
        ["ssh", "-q", cluster.host, cmd],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.stdout


def ssh_many(cluster: SlurmConfig, *cmds: str, timeout: int = 10) -> list[str]:
    """Run several SSH commands against one cluster concurrently."""
    with ThreadPoolExecutor(max_workers=len(cmds)) as pool:
        return list(pool.map(lambda c: ssh(cluster, c, timeout=timeout), cmds))


def current_cluster() -> SlurmConfig:
    """Resolve the cluster for this request from the `?cluster=` query param."""
    name = request.args.get("cluster")
    if name and name in CLUSTER_MAP:
        return CLUSTER_MAP[name]
    return CLUSTERS[0][1]


def parse_table(raw: str) -> tuple[list[str], list[list[str]]]:
    lines = [ln for ln in raw.strip().splitlines() if ln.strip()]
    if not lines:
        return [], []
    return lines[0].split(), [ln.split() for ln in lines[1:]]


# -- Routes ----------------------------------------------------------------


@app.route("/")
def index():
    return render_template("index.html", clusters=CLUSTER_NAMES)


GRES_RE = re.compile(r"gpu:(?:[^:(]+:)?(\d+)", re.IGNORECASE)
GRES_TYPE_RE = re.compile(r"gpu:([^:(]+):(\d+)", re.IGNORECASE)
FEATURE_GPU_RE = re.compile(r"NVIDIA_(\w+)", re.IGNORECASE)
DOWN = {"down", "down*", "drain", "drain*", "drng", "maint"}
NODELIST_RE = re.compile(r"^(.+)\[(.+)]$")


def gpu_count(gres: str) -> int:
    m = GRES_RE.search(gres)
    return int(m.group(1)) if m else 0


def gpu_type_and_count(gres: str, features: str = "") -> tuple[str, int] | None:
    m = GRES_TYPE_RE.search(gres)
    if m:
        return (m.group(1).upper(), int(m.group(2)))
    n = gpu_count(gres)
    if not n:
        return None
    feat = FEATURE_GPU_RE.search(features)
    return (feat.group(1).upper() if feat else "GPU", n)


def expand_nodelist(s: str) -> list[str]:
    m = NODELIST_RE.match(s)
    if not m:
        return [s]
    prefix, ranges = m.group(1), m.group(2)
    out: list[str] = []
    for part in ranges.split(","):
        if "-" in part:
            lo, hi = part.split("-", 1)
            out += [
                f"{prefix}{str(i).zfill(len(lo))}" for i in range(int(lo), int(hi) + 1)
            ]
        else:
            out.append(f"{prefix}{part}")
    return out


@app.route("/nodes")
def nodes():
    cluster = current_cluster()
    raw_nodes, raw_jobs = ssh_many(
        cluster,
        "sinfo -N -h -o '%N|%t|%G|%f'",
        "squeue -h --states=running,completing -o '%N|%b'",
    )

    # Per-node allocated GPU count from running jobs
    node_alloc: dict[str, int] = {}
    for line in raw_jobs.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 2 or not parts[0].strip() or parts[1] in ("", "N/A"):
            continue
        for node in expand_nodelist(parts[0]):
            node_alloc[node] = node_alloc.get(node, 0) + gpu_count(parts[1])

    # Aggregate by (gpu_type, vram_gb) — same type can have different VRAM sizes
    seen: set[str] = set()
    totals: dict[tuple, int] = {}
    free_map: dict[tuple, int] = {}
    for line in raw_nodes.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 4 or parts[0] in seen:
            continue
        seen.add(parts[0])
        parsed = gpu_type_and_count(parts[2], parts[3])
        if not parsed:
            continue
        gpu_type, total = parsed
        state = parts[1]
        m = GPU_MEM_RE.search(parts[3])
        vram_gb = int(m.group(1)) if m else 0
        key = (gpu_type, vram_gb)

        allocated = node_alloc.get(parts[0], 0)
        if not allocated and state in ("alloc", "resv"):
            allocated = total
        node_free = max(0, total - allocated) if state not in DOWN else 0

        totals[key] = totals.get(key, 0) + total
        free_map[key] = free_map.get(key, 0) + node_free

    gpus = []
    for key in sorted(totals):
        gpu_type, vram_per_gpu = key
        t, f = totals[key], free_map[key]
        vram_total = t * vram_per_gpu
        vram_used = (t - f) * vram_per_gpu
        vram_free = f * vram_per_gpu
        vram_pct = round(vram_used / vram_total * 100) if vram_total else 0
        gpus.append(
            {
                "type": gpu_type,
                "total": t,
                "used": t - f,
                "free": f,
                "pct": round((t - f) / t * 100) if t else 0,
                "vram_per_gpu": vram_per_gpu,
                "vram_total": vram_total,
                "vram_used": vram_used,
                "vram_free": vram_free,
                "vram_pct": vram_pct,
            }
        )
    return render_template("nodes.html", gpus=gpus)


@app.route("/jobs")
def jobs():
    cluster = current_cluster()
    raw, raw_closed = ssh_many(
        cluster,
        "squeue -u $USER -o '%.12i %.30j %.8T %.10M %.20b'",
        "sacct -u $USER -S now-7days --noheader --parsable2 -X "
        "-o 'JobID,JobName,State,Elapsed,AllocTRES' "
        "| grep -vE 'RUNNING|PENDING'",
    )
    headers, rows = parse_table(raw)
    # Rename ugly TRES_PER_NODE header
    headers = ["GPU" if "TRES" in h else h for h in headers]
    # Format GPU column: "gpu:l40s:1" -> "l40s"
    gpu_idx = headers.index("GPU") if "GPU" in headers else None
    if gpu_idx is not None:
        for row in rows:
            if gpu_idx < len(row):
                m = GRES_TYPE_RE.search(row[gpu_idx])
                row[gpu_idx] = (
                    f"{m.group(1).upper()}:{m.group(2)}" if m else row[gpu_idx]
                )
    # Recent completed/failed/cancelled jobs from the last 7 days
    closed_lines = [ln for ln in raw_closed.strip().splitlines() if ln.strip()]
    closed_rows = [ln.split("|") for ln in reversed(closed_lines)]
    for row in closed_rows:
        # Normalise e.g. "CANCELLED by 12345" or "CANCELLED+" to "CANCELLED"
        if len(row) > 2:
            row[2] = row[2].split()[0].rstrip("+")
        tres = row[4] if len(row) > 4 else ""
        gpu = ""
        for part in tres.split(","):
            if "gres/gpu:" in part:
                rest = part.split("gres/gpu:")[1]
                name, _, count = rest.partition("=")
                gpu = f"{name.upper()}:{count}" if count else name.upper()
                break
        row[4:] = [gpu]
    closed_headers = ["JOBID", "NAME", "STATE", "ELAPSED", "GPU"] if closed_rows else []
    return render_template(
        "jobs.html",
        headers=headers,
        rows=rows,
        closed_headers=closed_headers,
        closed_rows=closed_rows,
    )


@app.route("/cancel/<job_id>", methods=["POST"])
def cancel(job_id):
    if not job_id.isdigit():
        return "Bad job id", 400
    ssh(current_cluster(), f"scancel {job_id}")
    return "", 204



@app.route("/logs/<job_id>/history")
def logs_history(job_id):
    """Stream the existing log file as chunked plain text (fast initial load)."""
    if not job_id.isdigit():
        return "Bad job id", 400

    cluster = current_cluster()
    cmd = f"cat {cluster.remote_path}/slurm/slurm-{job_id}.out"

    def stream():
        proc = subprocess.Popen(
            ["ssh", "-q", cluster.host, cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        try:
            assert proc.stdout is not None
            while chunk := proc.stdout.read(65536):
                yield chunk
        finally:
            proc.terminate()
            proc.wait()

    return Response(stream(), mimetype="text/plain; charset=utf-8")


@app.route("/logs/<job_id>")
def logs(job_id):
    """SSE stream of newly-appended log lines only (use /history for backlog)."""
    if not job_id.isdigit():
        return "Bad job id", 400

    cluster = current_cluster()
    cmd = f"tail -n 0 -f {cluster.remote_path}/slurm/slurm-{job_id}.out"

    def stream():
        proc = subprocess.Popen(
            ["ssh", "-q", cluster.host, cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            for line in proc.stdout or []:
                yield f"data: {line.rstrip()}\n\n"
        finally:
            proc.terminate()
            proc.wait()

    return Response(stream(), mimetype="text/event-stream")


if __name__ == "__main__":
    app.run(debug=True, host="127.0.0.1", port=5000, threaded=True)
