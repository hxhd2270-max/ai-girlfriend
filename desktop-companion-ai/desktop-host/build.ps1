param([string]$OutputDirectory = '')
$ErrorActionPreference = 'Stop'
$hostRoot = $PSScriptRoot
$sdkRoot = Join-Path $hostRoot 'dependencies/webview2-1.0.4258.31'
$compilerPath = Join-Path $env:WINDIR 'Microsoft.NET/Framework64/v4.0.30319/csc.exe'
$outputRoot = if ($OutputDirectory) { [IO.Path]::GetFullPath($OutputDirectory) } else { Join-Path $hostRoot 'bin' }
if (-not (Test-Path -LiteralPath $sdkRoot)) { throw 'Pinned WebView2 SDK is missing. See desktop-host/README.md.' }
if (-not (Test-Path -LiteralPath $compilerPath)) { throw '.NET Framework compiler is missing.' }
New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
$corePath = Join-Path $sdkRoot 'lib/net462/Microsoft.Web.WebView2.Core.dll'
$formsPath = Join-Path $sdkRoot 'lib/net462/Microsoft.Web.WebView2.WinForms.dll'
$buildArguments = @('/nologo','/target:winexe','/platform:x64','/optimize+',('/out:' + (Join-Path $outputRoot 'Companion.Desktop.exe')),('/win32manifest:' + (Join-Path $hostRoot 'app.manifest')),'/r:System.dll','/r:System.Core.dll','/r:System.Drawing.dll','/r:System.Windows.Forms.dll','/r:System.Web.Extensions.dll',('/r:' + $corePath),('/r:' + $formsPath),(Join-Path $hostRoot 'Host.cs'))
$buildArguments += @((Join-Path $hostRoot 'DesktopSession.cs'), (Join-Path $hostRoot 'DesktopMode.cs'), (Join-Path $hostRoot 'DesktopChat.cs'))
$buildArguments += '/r:Accessibility.dll'
& $compilerPath $buildArguments
if ($LASTEXITCODE -ne 0) { throw 'Standalone host compilation failed.' }
Copy-Item -LiteralPath $corePath,$formsPath -Destination $outputRoot
Copy-Item -LiteralPath (Join-Path $sdkRoot 'runtimes/win-x64/native/WebView2Loader.dll') -Destination $outputRoot
Get-Item -LiteralPath (Join-Path $outputRoot 'Companion.Desktop.exe') | Select-Object FullName,Length,LastWriteTime
