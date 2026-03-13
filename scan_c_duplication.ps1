param(
    [Parameter(Mandatory = $false)]
    [string]$Repo = ".",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = "python"

if ($null -eq $ExtraArgs -or $ExtraArgs.Count -eq 0) {
    $ExtraArgs = @("--mode", "scan-only")
} elseif (-not ($ExtraArgs -contains "--mode")) {
    $ExtraArgs = @("--mode", "scan-only") + $ExtraArgs
}

& $Python "$ScriptDir/main.py" workflow --repo $Repo @ExtraArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
