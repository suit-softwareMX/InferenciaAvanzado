param([switch]$ShowKey)

$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
$keyFile = Join-Path $PSScriptRoot 'data\auditaxes-key.dpapi'

if ($ShowKey -and -not (Test-Path -LiteralPath $keyFile)) {
    throw 'Todavía no existe una clave. Inicia el servicio una vez para generarla.'
}

if (-not (Test-Path -LiteralPath $keyFile)) {
    New-Item -ItemType Directory -Path (Split-Path $keyFile) -Force | Out-Null
    $bytes = New-Object byte[] 32
    $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
    try { $generator.GetBytes($bytes) }
    finally { $generator.Dispose() }
    $key = [BitConverter]::ToString($bytes).Replace('-', '')
    ConvertTo-SecureString $key -AsPlainText -Force | ConvertFrom-SecureString | Set-Content -LiteralPath $keyFile
    Write-Host "Clave nueva para INFERENCE_API_KEY en la API de AUDITAXES: $key"
    Write-Host 'Cópiala ahora por un canal seguro. No la subas a Git ni la pegues en un chat.'
} else {
    $secureKey = Get-Content -LiteralPath $keyFile | ConvertTo-SecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try { $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer) }
}

if ($ShowKey) {
    Write-Host "INFERENCE_API_KEY: $key"
    return
}

if (-not (Test-Path -LiteralPath $python)) { throw "Falta Python en $python" }
try { Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 | Out-Null }
catch { throw 'Ollama no responde en 127.0.0.1:11434. Inícialo antes de continuar.' }

$env:INFERENCE_API_KEYS = @{ auditaxes = $key } | ConvertTo-Json -Compress
Remove-Variable key
$env:INFERENCE_DB = Join-Path $PSScriptRoot 'data\jobs.sqlite3'
$env:OLLAMA_URL = 'http://127.0.0.1:11434'
$env:PREFERRED_MODEL = 'qwen3:14b'
$env:FALLBACK_MODEL = 'qwen3:4b-instruct-2507-q4_K_M'

Set-Location $PSScriptRoot
Write-Host 'Iniciando inferencia en 192.168.0.107:4110. Deja esta ventana abierta.'
& $python -m uvicorn server:app --host 192.168.0.107 --port 4110 --workers 1
if ($LASTEXITCODE -ne 0) { throw "El servidor terminó con código $LASTEXITCODE" }
