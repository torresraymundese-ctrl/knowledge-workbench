[CmdletBinding()]
param(
    [switch]$Rebind
)

$ErrorActionPreference = "Stop"
$RepositoryRoot = Split-Path -Parent $PSScriptRoot
$KnowledgeLauncher = Join-Path $RepositoryRoot ".venv\Scripts\knowledge.exe"
$WorkspaceRoot = Join-Path $RepositoryRoot "workspace"

if (-not (Test-Path -LiteralPath $KnowledgeLauncher -PathType Leaf)) {
    throw "Launcher not found: $KnowledgeLauncher"
}

$ExistingDeepSeekKey = [Environment]::GetEnvironmentVariable(
    "DEEPSEEK_API_KEY",
    "User"
)
if (-not $Rebind -and -not [string]::IsNullOrWhiteSpace($ExistingDeepSeekKey)) {
    $env:DEEPSEEK_API_KEY = $ExistingDeepSeekKey
    Remove-Variable ExistingDeepSeekKey -ErrorAction SilentlyContinue
    Write-Host "Using the DeepSeek API Key already bound to this Windows user."
} else {
    $DeepSeekSecret = Read-Host "Paste DeepSeek API Key (input is hidden)" `
        -AsSecureString
    $DeepSeekPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR(
        $DeepSeekSecret
    )
    try {
        $DeepSeekPlainText = (
            [Runtime.InteropServices.Marshal]::PtrToStringBSTR($DeepSeekPointer)
        )
        if ([string]::IsNullOrWhiteSpace($DeepSeekPlainText)) {
            throw "DeepSeek API Key cannot be empty."
        }
        $env:DEEPSEEK_API_KEY = $DeepSeekPlainText
        [Environment]::SetEnvironmentVariable(
            "DEEPSEEK_API_KEY",
            $DeepSeekPlainText,
            "User"
        )
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($DeepSeekPointer)
        Remove-Variable DeepSeekSecret, DeepSeekPointer, DeepSeekPlainText `
            -ErrorAction SilentlyContinue
    }
}

Write-Host "DeepSeek is configured. Starting the local knowledge workbench."
& $KnowledgeLauncher --workspace $WorkspaceRoot web
