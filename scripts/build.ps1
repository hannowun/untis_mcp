<#
.SYNOPSIS
    Build the untis-mcp container image for the Raspberry Pi from a Windows machine.

.DESCRIPTION
    Development happens on Windows/amd64, the server runs on ARM (NFR-05), so the image is
    cross-built with docker buildx under QEMU emulation. All dependencies are pure Python,
    which is what makes emulated builds fast enough to be worth it (ADR-008).

    Two ways to get the result onto the Pi, both supported here:
      -Output registry  push to a registry, then `docker compose pull` on the Pi
      -Output tarball   write a .tar to copy across with scp, then `docker load`

.PARAMETER Tag
    Image tag. With -Output registry this must include the registry and repository,
    e.g. ghcr.io/hannowun/untis-mcp:latest

.PARAMETER Platform
    Target platforms, comma separated. Defaults to linux/arm64 (the Pi). Add
    linux/amd64 to test the same image locally — note that a multi-platform build
    cannot be exported as a tarball or loaded into the local daemon.

.PARAMETER Output
    registry | tarball | load. Defaults to tarball.

.PARAMETER File
    Where to write the tarball. Defaults to untis-mcp-<arch>.tar in the repository root.

.EXAMPLE
    ./scripts/build.ps1
    Cross-build for the Pi and write untis-mcp-arm64.tar.

.EXAMPLE
    ./scripts/build.ps1 -Tag ghcr.io/hannowun/untis-mcp:latest -Output registry

.EXAMPLE
    ./scripts/build.ps1 -Platform linux/amd64 -Output load
    Build for this machine and load it into the local Docker, to try it out.
#>
[CmdletBinding()]
param(
    [string]$Tag = "untis-mcp:latest",
    [string]$Platform = "linux/arm64",
    [ValidateSet("registry", "tarball", "load")]
    [string]$Output = "tarball",
    [string]$File,
    [string]$Builder = "untis-mcp-builder"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot

try {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        throw "docker is not on PATH. Install Docker Desktop, or build on the Pi with scripts/deploy.sh -Build."
    }

    docker info --format '{{.ServerVersion}}' | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "The Docker engine is not reachable. Start Docker Desktop and try again."
    }

    # @() matters: a single-element -split returns a bare string, and indexing that would
    # take its first character rather than the platform.
    $platforms = @($Platform -split "\s*,\s*" | Where-Object { $_ })
    if ($platforms.Count -gt 1 -and $Output -ne "registry") {
        throw "A multi-platform build can only go to a registry; use -Output registry, or build one platform at a time."
    }

    # A dedicated builder keeps QEMU emulation and the build cache out of the default one.
    $existing = docker buildx ls --format '{{.Name}}' 2>$null
    if ($existing -notcontains $Builder) {
        Write-Host "Creating buildx builder '$Builder'..." -ForegroundColor Cyan
        docker buildx create --name $Builder --driver docker-container --bootstrap | Out-Null
    }

    if ($platforms -notcontains "linux/amd64") {
        # Register the QEMU handlers this host needs to run arm64 build steps.
        Write-Host "Enabling QEMU emulation for $Platform..." -ForegroundColor Cyan
        docker run --privileged --rm tonistiigi/binfmt --install all | Out-Null
    }

    $buildArgs = @(
        "buildx", "build",
        "--builder", $Builder,
        "--platform", $Platform,
        "--tag", $Tag,
        "--file", "Dockerfile"
    )

    switch ($Output) {
        "registry" { $buildArgs += "--push" }
        "load" { $buildArgs += "--load" }
        "tarball" {
            if (-not $File) {
                $arch = ($platforms[0] -split "/")[-1]
                $File = Join-Path $repoRoot "untis-mcp-$arch.tar"
            }
            $buildArgs += "--output"
            $buildArgs += "type=docker,dest=$File"
        }
    }

    $buildArgs += "."

    Write-Host "Building $Tag for $Platform ($Output)..." -ForegroundColor Cyan
    & docker @buildArgs
    if ($LASTEXITCODE -ne 0) { throw "docker buildx build failed with exit code $LASTEXITCODE." }

    switch ($Output) {
        "registry" {
            Write-Host "Pushed $Tag." -ForegroundColor Green
            Write-Host "On the Pi:       ./scripts/deploy.sh --pull"
            Write-Host "Its .env needs:  UNTIS_MCP_IMAGE=$Tag"
        }
        "tarball" {
            $size = [math]::Round((Get-Item $File).Length / 1MB, 1)
            Write-Host "Wrote $File ($size MB)." -ForegroundColor Green
            Write-Host "Copy it across:  scp `"$File`" pi@raspberrypi:~/untis-mcp/"
            Write-Host "Then on the Pi:  ./scripts/deploy.sh --tarball ~/untis-mcp/$(Split-Path -Leaf $File)"
            Write-Host "Its .env needs:  UNTIS_MCP_IMAGE=$Tag  (see .env.example)"
        }
        "load" {
            Write-Host "Loaded $Tag into the local Docker." -ForegroundColor Green
        }
    }
}
finally {
    Pop-Location
}
