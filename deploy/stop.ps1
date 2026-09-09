$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
docker compose `
    --env-file (Join-Path $DeployDir ".env") `
    -f (Join-Path $DeployDir "docker-compose.yml") `
    down

if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to stop."
}