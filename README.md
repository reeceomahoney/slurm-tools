# slurm-tools

CLI for submitting SLURM jobs via SSH and a web GUI for monitoring them.

## Install

```bash
uv pip install git+https://github.com/reeceomahoney/slurm-tools.git
```

## Configuration

Place a `configs/slurm.yaml` in your working directory:

```yaml
host: my-cluster
remote_path: /data/user/project
time: 6
gpu: h100
ngpu: 1
cpus: 16
mem: 8G
priority: true
envs:
  - WANDB_API_KEY
  - HF_TOKEN
  - MUJOCO_GL: egl
command: >-
  singularity run --nv container.sif make train
```

If no config file exists, all options fall back to dataclass defaults and can be set entirely via CLI flags.

### Multiple clusters

To work with more than one cluster, keep the shared job settings at the top
level and add a `clusters:` list. Each entry needs a `name`, `host`, and
`remote_path`, and may override any other field — anything it omits is
inherited from the top level. The one exception is `envs`: a cluster's `envs`
are *appended* to the shared list rather than replacing it (a per-cluster
entry with the same name overrides the shared value):

```yaml
time: 6
gpu: h100
ngpu: 1
cpus: 16
mem: 8G
envs:
  - WANDB_API_KEY
command: >-
  singularity run --nv container.sif make train

clusters:
  - name: prod
    host: cluster-a
    remote_path: /scratch/me/project
  - name: dev
    host: cluster-b
    remote_path: /home/me/project
    gpu: l40s          # overrides the shared default
```

The GUI shows one tab per cluster. The CLI submits to the first cluster by
default; pass `--cluster NAME` to target another.

## Usage

### Submit a job

```bash
slurm run                          # uses configs/slurm.yaml
slurm run --cluster dev            # target a specific cluster (see Multiple clusters)
slurm run --gpu l40s --time 3      # override specific fields
slurm run --command "make eval"    # override command
slurm run --dry_run true           # print the sbatch script without submitting
```

This rsyncs the project to the remote host (respecting `.gitignore`), then submits via `sbatch`.

### Sync only

```bash
slurm sync                         # rsync the project without submitting a job
```

### Web GUI

```bash
slurm gui           # start the monitoring server on localhost:5000
slurm gui stop      # stop it
```

The GUI shows GPU availability across nodes, running/completed jobs, log streaming, and supports cancelling jobs. With multiple clusters configured, each gets its own tab. The GUI requires the host is set in `configs/slurm.yaml`. Use your alias from your ssh config and make sure you have an ssh key setup.

![SLURM Monitor GUI](gui_screenshot.png)

### SSH performance

Every `slurm` command — and every GUI poll — opens an SSH connection to the cluster. Enabling connection multiplexing in `~/.ssh/config` makes subsequent calls reuse a single channel, which noticeably reduces CLI latency and GUI refresh times:

```sshconfig
Host *
    ServerAliveInterval 60
    ServerAliveCountMax 30
    ControlMaster auto
    ControlPath ~/.ssh/sockets/%r@%h-%p
    ControlPersist 10m
```

Create the socket directory once: `mkdir -p ~/.ssh/sockets`.

## Config reference

| Field         | Default | Description                        |
| ------------- | ------- | ---------------------------------- |
| `name`        | `""`    | Cluster label shown as a GUI tab and used by `--cluster` |
| `host`        | **required** | SSH host alias for the cluster     |
| `remote_path` | **required** | Absolute path on the remote host   |
| `command`     | **required** | Shell command to run in the job    |
| `time`        | `6`     | Job time limit in hours            |
| `gpu`         | `h100`  | GPU type for typed GRES (e.g. h100, l40s); leave empty for `gpu:N` |
| `ngpu`        | `1`     | Number of GPUs                     |
| `cpus`        | `16`    | CPUs per node                      |
| `mem`         | `8G`    | Memory per CPU                     |
| `priority`    | `false` | Use priority credits (if available)|
| `dry_run`     | `false` | Print sbatch script without submit |
| `envs`        | `[]`    | Env vars to set in the job — bare names are forwarded from local, `KEY: value` entries are set literally (see below) |

### Setting environment variables

`envs` accepts two entry shapes:

- **Bare name** (`- WANDB_API_KEY`) — read from your local shell at submit time
  and exported in the job. Errors out if the variable isn't set locally. Use
  this for secrets you don't want to commit.
- **Key/value** (`- MUJOCO_GL: egl`) — exported in the job with the literal
  value. Use this for static config like `MUJOCO_GL`, `TOKENIZERS_PARALLELISM`,
  etc.

Both forms are prepended to the sbatch script as quoted `export VAR=...`
lines. They aren't interpolated into `command`, so reference them by name
inside the job, not as `${VAR}` in the YAML.
