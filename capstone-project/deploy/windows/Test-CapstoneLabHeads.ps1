param([string]$InstallRoot='C:\Program Files\Capstone',[string]$DataRoot='C:\ProgramData\Capstone',[int]$TimeoutSeconds=1800)
$ErrorActionPreference='Stop'
$settings=Get-Content -Raw -LiteralPath (Join-Path $DataRoot 'config\appsettings.json')|ConvertFrom-Json
$token=(Get-Content -Raw -LiteralPath $settings.developer_token_file).Trim()
$headers=@{'X-Capstone-Developer-Token'=$token;'Content-Type'='application/json'}
$base="http://$($settings.listen_host):$($settings.listen_port)"
$state=Invoke-RestMethod -Uri "$base/api/lab/state?refresh=true" -Headers $headers -TimeoutSec 30
$inventoryDeadline=(Get-Date).AddSeconds(75)
while($state.inventory_status -eq 'checking' -and (Get-Date) -lt $inventoryDeadline){
 Start-Sleep -Seconds 1
 $state=Invoke-RestMethod -Uri "$base/api/lab/state" -Headers $headers -TimeoutSec 10
}
if($state.inventory_status -ne 'ready'){
 $reason=if($state.inventory_message){[string]$state.inventory_message}else{'Gateway inventory did not reach ready state within 75 seconds'}
 [ordered]@{passed=$false;targets=0;inventory_status=$state.inventory_status;error=$reason}|ConvertTo-Json -Compress
 exit 1
}
$targets=@($state.heads|Where-Object {$_.serving})
if($targets.Count -eq 0){
 [ordered]@{passed=$false;targets=0;inventory_status='ready';error='Gateway inventory is ready but contains zero serving Testing Lab heads'}|ConvertTo-Json -Compress
 exit 1
}
$output=@();$passed=$true
foreach($head in $targets){
 $record=$null
 $preferred=$head.models|Where-Object {$_.kind -eq 'chat' -and $_.usable -and $_.name -eq 'qwen3.5:4b'}|Select-Object -First 1
 if(!$preferred){$preferred=$head.models|Where-Object {$_.kind -eq 'chat' -and $_.usable}|Sort-Object size_bytes|Select-Object -First 1}
 if(!$preferred){
  $record=[ordered]@{resource_id=$head.resource_id;model=$null;status='failed';reason='no usable chat model';arena_rows_written=0}
  $passed=$false
 }else{
  $body=@{experiment='smoke';models=@($preferred.name);heads=@($head.resource_id);repetitions=1}|ConvertTo-Json
  try{
   $started=Invoke-RestMethod -Method Post -Uri "$base/api/lab/start" -Headers $headers -Body $body -TimeoutSec 30
   $deadline=(Get-Date).AddSeconds($TimeoutSeconds);$run=$null
   do{
    Start-Sleep -Seconds 2;$all=Invoke-RestMethod -Uri "$base/api/lab/results?experiment_id=$($started.id)" -Headers $headers -TimeoutSec 30
    $experiment=$all.experiments|Where-Object {$_.id -eq $started.id}|Select-Object -First 1
    if($experiment){$run=$experiment.runs|Select-Object -First 1}
   }while((Get-Date) -lt $deadline -and (!$run -or $run.status -in @('queued','running')))
   $pinnedObserved=$run -and @($run.inference_correlations|Where-Object {$_.resource_id -eq $head.resource_id}).Count -gt 0
   # A synthetic case may legitimately pause for a person's review after the
   # pinned model call.  That still proves the head served Capstone.
   $ok=$run -and $run.status -in @('completed','paused') -and [int]$run.model_calls -ge 1 -and $pinnedObserved
   $record=[ordered]@{resource_id=$head.resource_id;model=$preferred.name;status=if($run){$run.status}else{'timeout'};error=if($run){$run.error}else{'timed out waiting for the run'};model_calls=if($run){$run.model_calls}else{0};pinned_head_observed=[bool]$pinnedObserved;gateway_request_id=$run.gateway_request_id;queue_ms=$run.queue_ms;duration_ms=$run.duration_ms;arena_rows_written=if($run){$run.arena_rows_written}else{0}}
   if(!$ok){$passed=$false}
  }catch{
   $record=[ordered]@{resource_id=$head.resource_id;model=$preferred.name;status='failed';error=$_.Exception.Message;arena_rows_written=0}
   $passed=$false
  }
 }
 $output+=$record
 $record|ConvertTo-Json -Compress
}
[ordered]@{passed=$passed;targets=$output.Count}|ConvertTo-Json -Compress
if(!$passed){exit 1}
