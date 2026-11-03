#requires -Version 5.1
param(
    [ValidateSet('zh-CN', 'en')]
    [string]$Language = 'zh-CN'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$projectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$runtimeRoot = Join-Path $projectRoot '.runtime'
$logPath = Join-Path $runtimeRoot ('startup-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + $PID + '.log')
$messages = Get-Content -LiteralPath (Join-Path $PSScriptRoot "messages/windows-$Language.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$manifest = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'windows-runtime.json') -Raw -Encoding UTF8 | ConvertFrom-Json

function Get-Message([string]$Key, [object[]]$Values = @()) {
    return [string]::Format($messages.$Key, $Values)
}

function Invoke-CheckedCommand([string]$Executable, [string[]]$Arguments) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw (Get-Message 'command_failed' @($LASTEXITCODE, $Executable))
    }
}

function Install-Runtime([string]$Name, $Package) {
    $destination = Join-Path $runtimeRoot $Package.directory
    $readyPath = Join-Path $destination '.ready.sha256'
    $complete = Test-Path -LiteralPath $readyPath -PathType Leaf
    if ($complete) {
        $complete = (Get-Content -LiteralPath $readyPath -Raw).Trim() -eq $Package.sha256
        foreach ($executable in $Package.executables) {
            $complete = $complete -and (Test-Path -LiteralPath (Join-Path $destination $executable) -PathType Leaf)
        }
    }
    if ($complete) {
        Write-Host (Get-Message 'runtime_reused' @($Name, $Package.version))
        return $destination
    }

    $downloadDirectory = Join-Path $runtimeRoot 'downloads'
    [IO.Directory]::CreateDirectory($downloadDirectory) | Out-Null
    $archivePath = Join-Path $downloadDirectory ([IO.Path]::GetFileName(([uri]$Package.url).AbsolutePath))
    if (Test-Path -LiteralPath $archivePath) {
        if ((Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash -ne $Package.sha256) {
            Write-Host (Get-Message 'cache_invalid' @($archivePath))
            Remove-Item -LiteralPath $archivePath -Force
        }
    }
    if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
        Write-Host (Get-Message 'downloading' @($Name, $Package.version))
        $partialPath = "$archivePath.part"
        try {
            Invoke-WebRequest -UseBasicParsing -Uri $Package.url -OutFile $partialPath -TimeoutSec 600
            $actualHash = (Get-FileHash -LiteralPath $partialPath -Algorithm SHA256).Hash
            if ($actualHash -ne $Package.sha256) {
                throw (Get-Message 'checksum_failed' @($archivePath, $Package.sha256, $actualHash))
            }
            Move-Item -LiteralPath $partialPath -Destination $archivePath -Force
        }
        finally {
            if (Test-Path -LiteralPath $partialPath) { Remove-Item -LiteralPath $partialPath -Force }
        }
    }

    Write-Host (Get-Message 'extracting' @($Name, $Package.version))
    $stagingPath = Join-Path $runtimeRoot ('.extract-' + [guid]::NewGuid().ToString('N'))
    try {
        Expand-Archive -LiteralPath $archivePath -DestinationPath $stagingPath
        $extractedRoot = if ($Package.archive_root) { Join-Path $stagingPath $Package.archive_root } else { $stagingPath }
        foreach ($executable in $Package.executables) {
            $executablePath = Join-Path $extractedRoot $executable
            if (-not (Test-Path -LiteralPath $executablePath -PathType Leaf)) {
                throw (Get-Message 'runtime_incomplete' @($executablePath))
            }
        }
        if (Test-Path -LiteralPath $destination) { Remove-Item -LiteralPath $destination -Recurse -Force }
        Move-Item -LiteralPath $extractedRoot -Destination $destination
        [IO.File]::WriteAllText($readyPath, $Package.sha256)
    }
    finally {
        if (Test-Path -LiteralPath $stagingPath) { Remove-Item -LiteralPath $stagingPath -Recurse -Force }
    }
    return $destination
}

$transcriptStarted = $false
$bootstrapLock = $null
$exitCode = 0
try {
    Set-Location -LiteralPath $projectRoot
    $architecture = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
    if ($env:OS -ne 'Windows_NT' -or [Environment]::OSVersion.Version.Major -lt 10 -or $architecture -ne 'AMD64') {
        throw (Get-Message 'unsupported_system' @($architecture))
    }
    [IO.Directory]::CreateDirectory($runtimeRoot) | Out-Null
    try {
        $bootstrapLock = [IO.File]::Open((Join-Path $runtimeRoot 'setup.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
    }
    catch [IO.IOException] {
        throw (Get-Message 'install_busy')
    }
    [IO.File]::WriteAllText($logPath, '', (New-Object System.Text.UTF8Encoding($true)))
    Start-Transcript -LiteralPath $logPath -Append | Out-Null
    $transcriptStarted = $true
    Write-Host (Get-Message 'banner')
    Write-Host (Get-Message 'log_location' @($logPath))
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    $pythonRuntime = Install-Runtime 'Python' $manifest.python
    $ffmpegRuntime = Install-Runtime 'FFmpeg' $manifest.ffmpeg
    $curlRuntime = Install-Runtime 'curl' $manifest.curl
    $env:PATH = (Join-Path $curlRuntime 'bin') + ';' + (Join-Path $ffmpegRuntime 'bin') + ';' + $env:PATH
    $pythonExecutable = Join-Path $pythonRuntime 'python.exe'
    $venvRoot = Join-Path $projectRoot '.venv'
    $venvPython = Join-Path $venvRoot 'Scripts/python.exe'
    $dependencyStamp = Join-Path $runtimeRoot 'dependencies.sha256'
    $dependencyFingerprint = $pythonRuntime + "`n" + ((@('requirements.txt', 'requirements-web.txt') | ForEach-Object {
        (Get-FileHash -LiteralPath (Join-Path $projectRoot $_) -Algorithm SHA256).Hash
    }) -join "`n")
    $environmentReady = (Test-Path -LiteralPath $venvPython -PathType Leaf) -and (Test-Path -LiteralPath $dependencyStamp -PathType Leaf)
    if ($environmentReady) {
        $environmentReady = (Get-Content -LiteralPath $dependencyStamp -Raw -Encoding UTF8) -eq $dependencyFingerprint
    }
    if (-not $environmentReady) {
        Write-Host (Get-Message 'creating_environment')
        Invoke-CheckedCommand $pythonExecutable @('-m', 'venv', $venvRoot)
        Write-Host (Get-Message 'installing_dependencies')
        Invoke-CheckedCommand $venvPython @('-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary=:all:', '-r', (Join-Path $projectRoot 'requirements-web.txt'))
        Invoke-CheckedCommand $venvPython @('-c', 'import flask, PIL; from workbench.server import create_app')
        [IO.File]::WriteAllText($dependencyStamp, $dependencyFingerprint)
    }
    else {
        Write-Host (Get-Message 'dependencies_reused')
        Invoke-CheckedCommand $venvPython @('-c', 'import flask, PIL; from workbench.server import create_app')
    }
    Invoke-CheckedCommand (Join-Path $ffmpegRuntime 'bin/ffmpeg.exe') @('-version')
    Invoke-CheckedCommand (Join-Path $ffmpegRuntime 'bin/ffprobe.exe') @('-version')
    Invoke-CheckedCommand (Join-Path $curlRuntime 'bin/curl.exe') @('--version')
    $bootstrapLock.Dispose()
    $bootstrapLock = $null
    Write-Host (Get-Message 'starting')
    Invoke-CheckedCommand $venvPython @('-u', (Join-Path $projectRoot 'workbench.py'), '--open')
    Write-Host (Get-Message 'stopped')
}
catch {
    Write-Host (Get-Message 'failed' @($_.Exception.Message)) -ForegroundColor Red
    Write-Host (Get-Message 'log_location' @($logPath))
    $exitCode = 1
}
finally {
    if ($null -ne $bootstrapLock) { $bootstrapLock.Dispose() }
    if ($transcriptStarted) { Stop-Transcript | Out-Null }
}
exit $exitCode
