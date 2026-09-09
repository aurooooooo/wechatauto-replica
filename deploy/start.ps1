$ErrorActionPreference = "Stop"

$DeployDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$EnvFile = Join-Path $DeployDir ".env"
$ExampleFile = Join-Path $DeployDir ".env.example"
$ComposeFile = Join-Path $DeployDir "docker-compose.yml"

if (-not (Test-Path -LiteralPath $EnvFile)) {
    Copy-Item -LiteralPath $ExampleFile -Destination $EnvFile
    throw "Created deploy\.env. Edit PostgreSQL and MinIO passwords, then run again."
}

$EnvContent = Get-Content -LiteralPath $EnvFile -Raw -Encoding UTF8
if ($EnvContent -match "replace-this-") {
    throw "deploy\.env still has example passwords. Change them first."
}

docker compose --env-file $EnvFile -f $ComposeFile up -d
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose failed to start."
}
docker compose --env-file $EnvFile -f $ComposeFile ps