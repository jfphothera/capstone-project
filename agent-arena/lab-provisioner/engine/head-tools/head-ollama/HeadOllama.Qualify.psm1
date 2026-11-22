<#
    HeadOllama.Qualify (provisioner 1.6.14) - the conversations HEAD-OLLAMA sends to a head's own Ollama, how an answer
    is judged, and the batch runner that sends them truly at once. The prompts are representative of Capstone's ticket
    work: a structured triage answer (JSON schema), a tool call, and the reviewer's written review with an output
    budget of 1024 tokens (an output budget only; the context stays at num_ctx 8192). Every conversation carries its
    own marker (ticket code) and order number, so an answer that holds another conversation's marker fails isolation.
#>

$script:Tickets = @(
    @{ text = 'I was charged twice for invoice 4471 this month and need one charge refunded.'; draft = 'We have refunded the duplicate charge; it will show in 5 business days.' },
    @{ text = 'The replacement sensors we ordered two weeks ago still have not shipped.'; draft = 'Your sensors ship tomorrow by express courier at no cost.' },
    @{ text = 'Our infusion pump shows alarm E-31 after the firmware update and stops the run.'; draft = 'Please reinstall the firmware; E-31 is harmless and can be ignored.' },
    @{ text = 'I cannot sign in to the service portal since my email address changed.'; draft = 'We updated your sign-in email; use the reset link we just sent.' }
)

function New-LabQualifyConversation {
    <#  Pure. One conversation: its marker, its order number, its kind and the /api/chat body.
        Kinds: the reviewer model gets review / structured, the worker model structured / tool, alternating.  #>
    param([Parameter(Mandatory)][string]$Model, [Parameter(Mandatory)][int]$Index, [Parameter(Mandatory)][string]$RunTag,
          [string]$ReviewerModel = 'qwen3:4b', [int]$NumCtx = 8192, [int]$ReviewerNumPredict = 1024, [string]$KeepAlive = '30m')
    $marker = 'TKT-{0}-{1:D2}' -f $RunTag, $Index
    $order = 'ORD-{0}{1:D2}' -f $RunTag, $Index
    $t = $script:Tickets[($Index - 1) % $script:Tickets.Count]
    $kind = $(if ($Model -eq $ReviewerModel) { if ($Index % 2) { 'review' } else { 'structured' } } else { if ($Index % 2) { 'structured' } else { 'tool' } })
    $opts = [ordered]@{ num_ctx = $NumCtx; temperature = 0; seed = 42; num_predict = 256 }
    $body = [ordered]@{ model = $Model; stream = $false; think = $false; keep_alive = $KeepAlive; options = $opts }
    switch ($kind) {
        'structured' {
            $body.format = [ordered]@{ type = 'object'; required = @('ticket_code', 'category', 'priority', 'summary')
                                       properties = [ordered]@{ ticket_code = @{ type = 'string' }; category = @{ type = 'string'; enum = @('billing', 'shipping', 'quality', 'account') }
                                                                priority = @{ type = 'string'; enum = @('low', 'medium', 'high') }; summary = @{ type = 'string' } } }
            $body.messages = @(
                [ordered]@{ role = 'system'; content = 'You triage customer support tickets for a medical-device manufacturer. Answer only with the requested JSON.' },
                [ordered]@{ role = 'user'; content = "This conversation's ticket code is $marker. Keep it for this conversation." },
                [ordered]@{ role = 'assistant'; content = "Understood. Ticket code $marker." },
                [ordered]@{ role = 'user'; content = ('Ticket text: "{0}" Return the JSON, with ticket_code set to this conversation''s ticket code.' -f $t.text) })
        }
        'tool' {
            $body.tools = @([ordered]@{ type = 'function'; function = [ordered]@{ name = 'lookup_order'; description = 'Look up the shipping status of a customer order.'
                                parameters = [ordered]@{ type = 'object'; required = @('order_id', 'ticket_code')
                                                         properties = [ordered]@{ order_id = @{ type = 'string'; description = 'The order number, for example ORD-12345' }
                                                                                  ticket_code = @{ type = 'string'; description = 'The ticket code of this conversation' } } } } })
            $body.messages = @(
                [ordered]@{ role = 'system'; content = 'You are a support agent. Use the tools when they help.' },
                [ordered]@{ role = 'user'; content = "The ticket code for this conversation is $marker." },
                [ordered]@{ role = 'assistant'; content = "Noted: $marker." },
                [ordered]@{ role = 'user'; content = "The customer asks where order $order is. Look it up with the lookup_order tool, passing the order number and this conversation's ticket code." })
        }
        'review' {
            $opts.num_predict = $ReviewerNumPredict
            $body.messages = @(
                [ordered]@{ role = 'system'; content = 'You are the quality reviewer for customer-support answers at a regulated medical-device manufacturer.' },
                [ordered]@{ role = 'user'; content = ("Review reference: {0}.`nTicket: ""{1}""`nDraft answer: ""{2}""`nWrite the review with the headings Findings, Risks and Verdict. The first line must be 'Review {0}'. The Verdict is APPROVE or REVISE." -f $marker, $t.text, $t.draft) })
        }
    }
    return [pscustomobject]@{ Index = $Index; Marker = $marker; Order = $order; Kind = $kind; Model = $Model; Body = $body }
}

function Test-LabQualifyAnswer {
    <#  Pure. Whether one reply is a valid answer to its conversation, the text isolation is checked on, and a short
        key (category/priority, tool/order, verdict) compared against the serial baseline.  #>
    param([Parameter(Mandatory)]$Conversation, $Reply)
    $msg = $(if ($Reply) { $Reply.message } else { $null })
    $content = $(if ($msg) { [string]$msg.content } else { '' })
    $why = ''; $key = ''; $text = $content
    switch ($Conversation.Kind) {
        'structured' {
            $j = $null; try { $j = $content | ConvertFrom-Json -ErrorAction Stop } catch { }
            if (-not $j) { $why = 'not JSON' }
            elseif ([string]$j.ticket_code -ne $Conversation.Marker) { $why = "ticket_code '$($j.ticket_code)'" }
            elseif (@('billing', 'shipping', 'quality', 'account') -notcontains [string]$j.category) { $why = "category '$($j.category)'" }
            elseif (@('low', 'medium', 'high') -notcontains [string]$j.priority) { $why = "priority '$($j.priority)'" }
            elseif (-not [string]$j.summary) { $why = 'no summary' }
            if ($j) { $key = '{0}/{1}' -f $j.category, $j.priority }
        }
        'tool' {
            $call = $(if ($msg -and $msg.tool_calls) { @($msg.tool_calls)[0] } else { $null })
            $fn = $(if ($call) { $call.function } else { $null })
            $a = $(if ($fn) { $fn.arguments } else { $null })
            if ($a -is [string]) { try { $a = $a | ConvertFrom-Json -ErrorAction Stop } catch { } }
            $text = $content + ' ' + $(if ($a) { ($a | ConvertTo-Json -Compress -Depth 5) } else { '' })
            if (-not $fn) { $why = 'no tool call' }
            elseif ([string]$fn.name -ne 'lookup_order') { $why = "tool '$($fn.name)'" }
            elseif ([string]$a.order_id -ne $Conversation.Order) { $why = "order_id '$($a.order_id)'" }
            elseif ([string]$a.ticket_code -ne $Conversation.Marker) { $why = "ticket_code '$($a.ticket_code)'" }
            if ($fn) { $key = '{0}/{1}' -f $fn.name, $a.order_id }
        }
        'review' {
            $v = $(if ($content -match '(?is)verdict\W{0,20}(APPROVE|REVISE)') { $Matches[1].ToUpperInvariant() } else { '' })
            if (-not $content.Contains($Conversation.Marker)) { $why = 'review reference missing' }
            elseif (-not $v) { $why = 'no Verdict APPROVE/REVISE' }
            $key = $v
        }
    }
    return [pscustomobject]@{ Valid = (-not $why -and $null -ne $Reply); Why = $(if ($null -eq $Reply) { 'no reply' } else { $why }); Text = $text; Key = $key }
}

function Get-LabAgreement {
    <#  Pure. Share of conversations whose key equals the serial baseline's key for the same conversation.  #>
    param([AllowEmptyCollection()][array]$Results, [AllowEmptyCollection()][array]$Serial)
    $base = @{}; foreach ($s in $Serial) { if ($s.Valid) { $base[[string]$s.Marker] = [string]$s.Key } }
    $n = 0; $same = 0
    foreach ($r in $Results) { if ($base.ContainsKey([string]$r.Marker)) { $n++; if ([string]$r.Key -eq $base[[string]$r.Marker]) { $same++ } } }
    return $(if ($n) { [math]::Round($same / $n, 2) } else { $null })
}

function Invoke-LabQualifyBatch {
    <#  Sends the conversations at once (HttpClient, its own connection per request) and waits, sampling free memory
        and the resident models every second. -Serial sends them one after another instead. Each result: Marker, Kind,
        Model, Ok, Error, Reply, Sent, Received (Unix seconds), Valid, Why, Key, Text.  #>
    param([Parameter(Mandatory)][string]$Base, [Parameter(Mandatory)][array]$Conversations, [int]$TimeoutSeconds = 900, [switch]$Serial)
    Add-Type -AssemblyName System.Net.Http
    [Net.ServicePointManager]::DefaultConnectionLimit = [math]::Max(64, [Net.ServicePointManager]::DefaultConnectionLimit)   # .NET Framework allows 2 per host otherwise
    $client = New-Object System.Net.Http.HttpClient
    $client.Timeout = [TimeSpan]::FromSeconds($TimeoutSeconds)
    $uri = $Base.TrimEnd('/') + '/api/chat'
    $now = { ([DateTimeOffset]::UtcNow.UtcTicks - 621355968000000000) / 1e7 }
    $mem = New-Object System.Collections.ArrayList; $resident = 0
    $sample = {
        try { [void]$mem.Add([math]::Round((Get-CimInstance Win32_OperatingSystem -ErrorAction Stop).FreePhysicalMemory / 1MB, 2)) } catch { }
        try { $n = @((Invoke-RestMethod -Uri ($Base.TrimEnd('/') + '/api/ps') -TimeoutSec 3).models).Count; if ($n -gt $script:resident) { $script:resident = $n } } catch { }
    }
    $script:resident = 0
    $groups = New-Object System.Collections.ArrayList
    if ($Serial) { foreach ($c in $Conversations) { [void]$groups.Add(@($c)) } } else { [void]$groups.Add(@($Conversations)) }
    $out = @()
    foreach ($g in $groups) {
        $pending = @()
        foreach ($c in $g) {
            $json = $c.Body | ConvertTo-Json -Depth 12 -Compress
            $content = New-Object System.Net.Http.StringContent($json, [Text.Encoding]::UTF8, 'application/json')
            $pending += [pscustomobject]@{ C = $c; Sent = (& $now); Task = $client.PostAsync($uri, $content); Received = $null }
        }
        $deadline = (Get-Date).AddSeconds($TimeoutSeconds + 30)
        $tick = 0
        while (@($pending | Where-Object { -not $_.Received }).Count -and (Get-Date) -lt $deadline) {
            foreach ($p in $pending) { if (-not $p.Received -and $p.Task.IsCompleted) { $p.Received = (& $now) } }
            if ($tick % 10 -eq 0) { & $sample }   # about once a second; completions are seen within 0.1 s
            $tick++; Start-Sleep -Milliseconds 100
        }
        foreach ($p in $pending) {
            $r = [ordered]@{ Marker = $p.C.Marker; Kind = $p.C.Kind; Model = $p.C.Model; Ok = $false; Error = ''; Reply = $null; Sent = $p.Sent; Received = $(if ($p.Received) { $p.Received } else { & $now }) }
            try {
                if (-not $p.Task.IsCompleted) { $r.Error = 'no answer before the deadline' }
                elseif ($p.Task.IsFaulted) { $r.Error = [string]$p.Task.Exception.GetBaseException().Message }
                else {
                    $resp = $p.Task.Result; $txt = $resp.Content.ReadAsStringAsync().Result
                    if (-not $resp.IsSuccessStatusCode) { $r.Error = ('HTTP {0}: {1}' -f [int]$resp.StatusCode, ($txt -replace '\s+', ' ').Substring(0, [math]::Min(200, $txt.Length))) }
                    else { $r.Reply = $txt | ConvertFrom-Json; $r.Ok = $true }
                }
            } catch { $r.Error = $_.Exception.Message }
            $v = Test-LabQualifyAnswer -Conversation $p.C -Reply $r.Reply
            $r.Valid = $v.Valid; $r.Why = $(if ($r.Ok) { $v.Why } else { $r.Error }); $r.Key = $v.Key; $r.Text = $v.Text
            $out += [pscustomobject]$r
        }
    }
    $client.Dispose()
    return [pscustomobject]@{ Results = $out; MinFreeGb = $(if ($mem.Count) { ($mem | Measure-Object -Minimum).Minimum } else { $null }); MaxResident = $script:resident }
}

Export-ModuleMember -Function 'New-LabQualifyConversation', 'Test-LabQualifyAnswer', 'Get-LabAgreement', 'Invoke-LabQualifyBatch'
