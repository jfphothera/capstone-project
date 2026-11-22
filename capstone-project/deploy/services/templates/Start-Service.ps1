param([string]$InstallRoot='C:\Program Files\{{SERVICE}}',[string]$DataRoot='C:\ProgramData\{{SERVICE}}')
$ErrorActionPreference='Stop';$current=Get-Content -Raw "$InstallRoot\current.json"|ConvertFrom-Json;$config=Get-Content -Raw "$DataRoot\config\service.json"|ConvertFrom-Json
if(!(Test-Path $config.token_file)){throw 'Protected service token file is missing'};$python="$InstallRoot\releases\$($current.version)\python-runtime\python.exe";$hostScript="$InstallRoot\releases\$($current.version)\application\service_host.py"
$ErrorActionPreference='Continue';& $python $hostScript --kind '{{KIND}}' --data-root $config.data_root --host $config.listen_host --port $config.listen_port --token-file $config.token_file 1>>"$DataRoot\logs\service.log" 2>>"$DataRoot\logs\service-error.log"
exit $LASTEXITCODE
