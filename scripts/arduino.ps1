param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]] $CliArgs
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$ArduinoRoot = Join-Path $Root ".arduino"

$env:ARDUINO_DIRECTORIES_DATA = (New-Item -ItemType Directory -Force (Join-Path $ArduinoRoot "data")).FullName
$env:ARDUINO_DIRECTORIES_DOWNLOADS = (New-Item -ItemType Directory -Force (Join-Path $ArduinoRoot "downloads")).FullName
$env:ARDUINO_DIRECTORIES_USER = (New-Item -ItemType Directory -Force (Join-Path $ArduinoRoot "user")).FullName

$Cli = Join-Path $Root ".tools\arduino-cli\arduino-cli.exe"
& $Cli @CliArgs
exit $LASTEXITCODE
