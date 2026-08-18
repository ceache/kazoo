# Idempotently ensure a Linux dockerd runs inside WSL2 and is reachable from
# the Windows docker CLI over tcp://localhost:2375.
#
# Callable from any job step (PowerShell via `pwsh -File`, or bash with
# `pwsh -NoProfile -NonInteractive -File`). Safe to run repeatedly: if the
# daemon is already reachable it is a fast-path no-op; otherwise it writes the
# WSL idle settings, (re)installs the distro if missing and (re)launches
# dockerd, then verifies the *Windows-side* view the test harness actually
# uses.
#
# Background: the hosted Windows engine (Moby) only serves Windows containers,
# while the official ZooKeeper image is linux-only, so the ensemble suite runs
# against a Linux daemon in WSL2 (FR-011). WSL reaps an idle VM (default ~60s)
# once its last console exits, killing any dockerd it launched; job steps are
# separated by runs of the pip/test commands, so the daemon must survive the
# gap (vmIdleTimeout) and be re-established at the start of the step that uses
# it. Bind-mount sources are translated to the daemon's /mnt/<drive> layout by
# kazoo_ensemble.py, so /mnt/d:/... is handled there, not here.

$ErrorActionPreference = 'Stop'
$Distro = 'Ubuntu'
$env:DOCKER_HOST = 'tcp://localhost:2375'

function Test-WindowsDocker {
    docker info *> $null
    if ($LASTEXITCODE -ne 0) { return $false }
    $os = (docker info --format '{{.OSType}}' 2>$null | Out-String).Trim()
    return ($os -eq 'linux')
}

# Fast path: daemon already up from an earlier invocation in the same job.
if (Test-WindowsDocker) {
    Write-Host "dockerd already reachable at $env:DOCKER_HOST"
    exit 0
}

# Keep the WSL VM resident across steps so the background dockerd survives the
# gap between job steps (default ~60s idle reap would kill it). Applied once;
# `wsl --shutdown` forces the next VM boot to pick the config up.
$wslConfig = Join-Path $env:USERPROFILE '.wslconfig'
if (-not (Test-Path $wslConfig) -or
    -not (Get-Content $wslConfig -Raw -ErrorAction SilentlyContinue |
          Select-String -Quiet 'vmIdleTimeout')) {
    Set-Content -Path $wslConfig -Encoding ascii -Value @'
[wsl2]
# Keep the VM (and the background dockerd) alive across job steps.
vmIdleTimeout=2147483647
'@
    wsl.exe --shutdown
    Write-Host "wrote $wslConfig (vmIdleTimeout), reset WSL"
}

# Make sure the distro exists (first call in a job performs the install).
# -u root keeps the probe consistent with how dockerd is started (no reliance
# on the distro's default user being configured yet).
wsl.exe -d $Distro -u root -e true 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "installing $Distro (first boot on this runner)"
    wsl.exe --install $Distro --no-launch
}

# (Re)start dockerd inside WSL. Piped via stdin rather than a command line so
# wsl.exe cannot mangle shell specials; CRLF is normalized to LF so checkout
# line endings never leak into bash. The trailing `sleep` keeps the VM (and
# thus dockerd) resident for the rest of the job even if the idle reaper would
# otherwise fire; dockerd itself detaches via nohup.
$dockerdSetup = @'
set -eux
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq docker.io
# Ubuntu may already run a systemd-managed dockerd on the unix socket; stop it
# so our manually-launched daemon -- which also listens on tcp://0.0.0.0:2375
# for the Windows client -- can bind.
service docker stop >/dev/null 2>&1 || true
pkill -x dockerd >/dev/null 2>&1 || true
nohup dockerd --host=unix:///var/run/docker.sock --host=tcp://0.0.0.0:2375 >/tmp/dockerd.log 2>&1 </dev/null &
nohup sleep 21600 >/dev/null 2>&1 </dev/null &
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
# Trailing comment so any CRLF appended by the local pipe lands on a comment
# line instead of becoming a stray command.
# end
'@.Replace("`r`n", "`n").Replace("`r", "`n")

$dockerdSetup | wsl.exe -d $Distro -u root -- bash -s
if ($LASTEXITCODE -ne 0) {
    wsl.exe -d $Distro -u root -- cat /tmp/dockerd.log
    throw "WSL2 dockerd (re)start failed"
}

# Wait for the Windows client -> WSL daemon TCP path (localhost relay) to come
# up; this is the exact path the test harness uses, so failing here is fatal.
$deadline = (Get-Date).AddSeconds(90)
while (-not (Test-WindowsDocker)) {
    if ((Get-Date) -gt $deadline) {
        wsl.exe -d $Distro -u root -- cat /tmp/dockerd.log
        throw "Windows docker CLI cannot reach the WSL dockerd at $env:DOCKER_HOST"
    }
    Start-Sleep -Seconds 2
}
Write-Host "dockerd ready at $env:DOCKER_HOST (OSType=linux)"
exit 0