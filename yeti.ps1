# Launcher: .\yeti.ps1 --sim scan   or   .\yeti.ps1 --channel COM5 scan
$env:PYTHONPATH = "$PSScriptRoot\.deps;$PSScriptRoot"
py -3.13 -m yeti_diag @args
