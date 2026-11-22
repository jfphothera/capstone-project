<#
    Lab.Memory - what holds a lab machine's kernel memory, read-only.

    Head 7 (2026-11-10): committed 43.9 of 44.7 GB with all programs together holding about 6 GB; the kernel's paged
    pool held 24 GB and its nonpaged pool 3.1 GB (214 MB and 321 MB after a restart). The process list cannot say who
    that is. Windows counts kernel memory per four-letter pool tag; a tag names its owner (a driver, or a kind of
    kernel object such as tokens, registry keys, graphics allocations), and a program that leaks handles shows as a
    huge handle count.
#>

$script:PoolTagSource = @'
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
public static class LabPoolTags {
    [DllImport("ntdll.dll")] static extern int NtQuerySystemInformation(int cls, IntPtr buf, int len, out int ret);
    public class Tag { public string Name; public long PagedBytes, NonPagedBytes; public long PagedAllocs, NonPagedAllocs; }
    // SystemPoolTagInformation (22): ULONG Count, then SYSTEM_POOLTAG[Count]; on x64 each is 40 bytes:
    // Tag[4] PagedAllocs PagedFrees (pad) PagedUsed(8) NonPagedAllocs NonPagedFrees NonPagedUsed(8).
    public static List<Tag> Read() {
        int len = 1 << 20; var list = new List<Tag>();
        for (int i = 0; i < 6; i++) {
            IntPtr buf = Marshal.AllocHGlobal(len);
            try {
                int ret; int st = NtQuerySystemInformation(22, buf, len, out ret);
                if (st == unchecked((int)0xC0000004)) { len = Math.Max(ret, len * 2); continue; }
                if (st != 0) throw new InvalidOperationException("NtQuerySystemInformation(SystemPoolTagInformation) returned 0x" + st.ToString("X8"));
                int count = Marshal.ReadInt32(buf);
                for (int k = 0; k < count; k++) {
                    IntPtr e = IntPtr.Add(buf, 8 + k * 40);
                    var b = new byte[4]; Marshal.Copy(e, b, 0, 4);
                    var name = new string(new[] { (char)b[0], (char)b[1], (char)b[2], (char)b[3] });
                    long pa = Marshal.ReadInt32(e, 4) - (long)Marshal.ReadInt32(e, 8), na = Marshal.ReadInt32(e, 24) - (long)Marshal.ReadInt32(e, 28);
                    list.Add(new Tag { Name = name, PagedBytes = Marshal.ReadInt64(e, 16), NonPagedBytes = Marshal.ReadInt64(e, 32), PagedAllocs = pa, NonPagedAllocs = na });
                }
                return list;
            } finally { Marshal.FreeHGlobal(buf); }
        }
        throw new InvalidOperationException("the pool tag list did not fit");
    }
}
'@

# Well-known Windows tags (Microsoft's pooltag.txt), so the report says what a tag is without a debugger.
# Pool tags are case-sensitive ('DxgK' and 'Dxgk' are different tags): an ordinal dictionary, not a PowerShell hashtable.
$script:KnownTags = New-Object 'Collections.Generic.Dictionary[string,string]' ([StringComparer]::Ordinal)
foreach ($pair in @(
    @('Toke', 'security tokens (a program creating processes or impersonating without closing them)'), @('SeAt', 'security attributes'),
    @('Proc', 'process objects'), @('Thre', 'thread objects'), @('File', 'file objects (open file handles)'), @('Even', 'event objects'),
    @('MmSt', 'mapped-file section bookkeeping (memory-mapped files)'), @('MmCa', 'mapped-file control areas'), @('MmRe', 'mapped-file sections'),
    @('CM31', 'registry (hive data)'), @('CM25', 'registry'), @('CM16', 'registry'), @('CM29', 'registry'), @('CMDa', 'registry'),
    @('ObNm', 'object names'), @('ObHd', 'object handles'), @('ObtR', 'object references'), @('Ntff', 'NTFS file control blocks'), @('NtFs', 'NTFS'),
    @('FMfn', 'filter manager file names (antivirus/file filters)'), @('FMfc', 'filter manager contexts'), @('AleE', 'Windows firewall (WFP) endpoints'),
    @('EtwB', 'event tracing buffers'), @('Vad ', 'process address-space descriptors'), @('Wnf ', 'Windows notifications'), @('Sema', 'semaphores'),
    @('DxgK', 'graphics kernel (dxgkrnl)'), @('Dxgk', 'graphics kernel (dxgkrnl)'), @('DxgA', 'graphics allocations'), @('DxgP', 'graphics'), @('Gfx ', 'graphics'),
    @('smNp', 'memory compression store'), @('NDnd', 'network (NDIS)'), @('Wfpn', 'Windows firewall (WFP)'), @('Pool', 'pool tables'), @('Irp ', 'I/O requests'))) {
    $script:KnownTags[$pair[0]] = $pair[1]
}
function Get-LabTagMeaning([string]$Tag) { $v = $null; if ($script:KnownTags.TryGetValue($Tag, [ref]$v)) { return $v }; return '' }

function Get-LabPoolTags {
    <#  The kernel pool tags holding the most memory: Top in each pool, MB, outstanding allocations, and what the tag
        is when it is a well-known Windows tag. -FindDrivers also names the driver files that contain each tag (the
        way Microsoft's pooltag lookup is done: the tag's four letters inside System32\drivers\*.sys).  #>
    param([int]$Top = 10, [switch]$FindDrivers)
    if (-not ('LabPoolTags' -as [type])) { Add-Type -TypeDefinition $script:PoolTagSource -Language CSharp }
    $all = [LabPoolTags]::Read()
    $pick = {
        param($pool)
        $prop = if ($pool -eq 'paged') { 'PagedBytes' } else { 'NonPagedBytes' }; $allocs = if ($pool -eq 'paged') { 'PagedAllocs' } else { 'NonPagedAllocs' }
        @($all | Sort-Object $prop -Descending | Select-Object -First $Top | ForEach-Object {
            [pscustomobject][ordered]@{ pool = $pool; tag = $_.Name; mb = [math]::Round($_.$prop / 1MB, 1); allocations = $_.$allocs; what = (Get-LabTagMeaning $_.Name); drivers = '' } })
    }
    $rows = @(& $pick 'paged') + @(& $pick 'nonpaged')
    if ($FindDrivers) {
        $sys = @(Get-ChildItem -LiteralPath (Join-Path $env:windir 'System32\drivers') -Filter *.sys -File -ErrorAction SilentlyContinue)
        foreach ($r in @($rows | Where-Object { $_.mb -ge 100 -and -not $_.what })) {
            $hits = @($sys | Where-Object { try { [IO.File]::ReadAllText($_.FullName, [Text.Encoding]::ASCII).Contains($r.tag) } catch { $false } } | Select-Object -First 6 | ForEach-Object Name)
            $r.drivers = $hits -join ', '
        }
    }
    return $rows
}

function Get-LabPoolTagTable {
    <#  Every pool tag now, as name -> @(paged bytes, nonpaged bytes): two of these, taken apart, show what grew.  #>
    if (-not ('LabPoolTags' -as [type])) { Add-Type -TypeDefinition $script:PoolTagSource -Language CSharp }
    $t = New-Object 'Collections.Generic.Dictionary[string,long[]]' ([StringComparer]::Ordinal)
    foreach ($x in [LabPoolTags]::Read()) { $t[$x.Name] = [long[]]@($x.PagedBytes, $x.NonPagedBytes) }
    return ,$t
}

function Find-LabTagDrivers {
    <#  The driver files in System32\drivers that contain this pool tag's four letters.  #>
    param([Parameter(Mandatory)][string]$Tag)
    $hits = @(Get-ChildItem -LiteralPath (Join-Path $env:windir 'System32\drivers') -Filter *.sys -File -ErrorAction SilentlyContinue |
        Where-Object { try { [IO.File]::ReadAllText($_.FullName, [Text.Encoding]::ASCII).Contains($Tag) } catch { $false } } | Select-Object -First 6 | ForEach-Object Name)
    return ($hits -join ', ')
}

function Get-LabHandleHolders {
    <#  The programs holding the most handles, and the system's total: a program that leaks handles fills the
        kernel's paged pool while its own memory stays small.  #>
    param([int]$Top = 10)
    $p = @(Get-Process -ErrorAction SilentlyContinue)
    return [pscustomobject]@{ total = [long](($p | Measure-Object HandleCount -Sum).Sum)
        top = @($p | Sort-Object HandleCount -Descending | Select-Object -First $Top | ForEach-Object { [pscustomobject][ordered]@{ name = $_.ProcessName; pid = $_.Id; handles = $_.HandleCount; started = $(try { $_.StartTime.ToString('s') } catch { '' }) } }) }
}

function Format-LabKernelMemory {
    <#  Lines for a memory report: the pools' biggest tags and the handle holders.  #>
    param($Tags, $Handles)
    $lines = @('', 'kernel memory by pool tag (biggest first):', '   pool       MB   allocations  tag   what / driver files')
    foreach ($t in $Tags) { $lines += ('   {0,-8} {1,8:N1} {2,12:N0}  {3}  {4}' -f $t.pool, $t.mb, $t.allocations, $t.tag, $(if ($t.what) { $t.what } elseif ($t.drivers) { 'in ' + $t.drivers } else { '' })) }
    $lines += '', ('handles: {0:N0} in all; most held by:' -f $Handles.total)
    foreach ($h in $Handles.top) { $lines += ('   {0,9:N0}  {1} (pid {2}, started {3})' -f $h.handles, $h.name, $h.pid, $h.started) }
    return $lines
}

function New-LabMemoryWatchScript {
    <#  Pure - unit tested. The hourly memory log a head keeps (scheduled task, SYSTEM): one CSV line per hour with the
        committed memory, both pools, their three biggest tags, the handle total and its biggest holder, and Agent
        Arena's and Ollama's memory. Read-only; the file is kept under 2 MB. The collector brings it back.  #>
    param([Parameter(Mandatory)][string]$ModulePath, [Parameter(Mandatory)][string]$CsvPath)
    return @"
# Agent Arena Lab: hourly memory log (read-only). Written by FIX-LAB-HEAD; remove with
#   Unregister-ScheduledTask -TaskName 'Agent Arena Lab memory log' -Confirm:`$false
`$ErrorActionPreference = 'SilentlyContinue'
Import-Module '$ModulePath' -Force -DisableNameChecking
`$csv = '$CsvPath'
`$m = Get-CimInstance Win32_PerfFormattedData_PerfOS_Memory
`$t = @(Get-LabPoolTags -Top 3)
`$h = Get-LabHandleHolders -Top 1
`$ps = @(Get-Process)
`$web = [math]::Round(((`$ps | Where-Object ProcessName -eq 'msedgewebview2' | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB)
`$oll = [math]::Round(((`$ps | Where-Object ProcessName -like 'ollama*' | Measure-Object PrivateMemorySize64 -Sum).Sum) / 1MB)
`$tags = { param(`$pool) (@(`$t | Where-Object pool -eq `$pool | ForEach-Object { '{0}={1}' -f `$_.tag.Trim(), `$_.mb }) -join ' ') }
if (-not (Test-Path -LiteralPath `$csv)) { 'time,committed_gb,limit_gb,paged_mb,nonpaged_mb,paged_top,nonpaged_top,handles,top_handles,webview_mb,ollama_mb' | Set-Content -LiteralPath `$csv -Encoding ascii }
`$inv = [Globalization.CultureInfo]::InvariantCulture
`$row = @((Get-Date).ToString('s'), [math]::Round(`$m.CommittedBytes / 1GB, 1).ToString(`$inv), [math]::Round(`$m.CommitLimit / 1GB, 1).ToString(`$inv),
    [math]::Round(`$m.PoolPagedBytes / 1MB), [math]::Round(`$m.PoolNonpagedBytes / 1MB), (& `$tags 'paged'), (& `$tags 'nonpaged'), `$h.total,
    ('{0} {1}' -f `$h.top[0].name, `$h.top[0].handles), `$web, `$oll) -join ','
Add-Content -LiteralPath `$csv -Value `$row -Encoding ascii
if ((Get-Item -LiteralPath `$csv).Length -gt 2MB) { `$k = Get-Content -LiteralPath `$csv; (@(`$k[0]) + `$k[-5000..-1]) | Set-Content -LiteralPath `$csv -Encoding ascii }
"@
}

function Get-LabTraceVerdict {
    <#  Pure - unit tested. MEMORY-TRACE's verdict from its four phases (objects with phase, arena_open, inference,
        pool_mb_per_hour, programs). An effect is claimed only when BOTH comparisons that isolate it agree in sign and
        each is beyond the noise floor: Agent Arena = phase 2 - 1 and phase 4 - 3; inference = phase 3 - 1 and 4 - 2.
        A first trace on a laptop (2026-11-10) said "Agent Arena adds 542 MB/h" from one comparison of +899 and one
        of +184: the quiet phase was shrinking as memory was handed back, and Zoom started in phase 2.  #>
    param([Parameter(Mandatory)]$Phases, [int]$Minutes = 30)
    $lines = @('VERDICT')
    $p = @{}; foreach ($r in @($Phases)) { $p[[int]$r.phase] = $r }
    if ($p.Count -ne 4 -or @($p.Values | Where-Object { $null -eq $_.pool_mb_per_hour }).Count) { return @('VERDICT', '  NOT MEASURED: a phase did not finish or had too few samples; see TRACE-LOG.txt') }
    $rate = @{}; foreach ($k in 1..4) { $rate[$k] = [double]$p[$k].pool_mb_per_hour }
    $floor = [math]::Max(30, [math]::Round(600 / [math]::Max(1, $Minutes)))
    $judge = {
        param([string]$what, [bool]$ok, [string]$why, [double]$d1, [string]$n1, [double]$d2, [string]$n2)
        $pad = ('  {0}:' -f $what).PadRight(24)
        if (-not $ok) { return "$pad NOT MEASURED: $why; see TRACE-LOG.txt" }
        $both = ('({0} {1:+#,0;-#,0;0}, {2} {3:+#,0;-#,0;0} MB/h)' -f $n1, $d1, $n2, $d2)
        if ($d1 -ge $floor -and $d2 -ge $floor) { return ('{0} ADDS about {1:N0} MB/h of kernel memory {2}' -f $pad, [math]::Min($d1, $d2), $both) }
        if ($d1 -le -$floor -and $d2 -le -$floor) { return ('{0} lowers it by about {1:N0} MB/h {2}' -f $pad, [math]::Min(-$d1, -$d2), $both) }
        return ('{0} no consistent effect {1}: an effect is claimed only when both comparisons agree and each is over {2} MB/h' -f $pad, $both, $floor)
    }
    $okA = [bool]($p[2].arena_open -and $p[4].arena_open -and -not $p[1].arena_open -and -not $p[3].arena_open)
    $okL = [bool]($p[3].inference -and $p[4].inference -and -not $p[1].inference -and -not $p[2].inference)
    $lines += ('  with nothing running: {0:+#,0;-#,0;0} MB/h' -f $rate[1])
    $lines += (& $judge 'Agent Arena open' $okA 'it was not open in phases 2 and 4, or not closed in 1 and 3' ($rate[2] - $rate[1]) 'phase 2-1' ($rate[4] - $rate[3]) 'phase 4-3')
    $lines += (& $judge 'inference running' $okL 'no requests were answered in phases 3 and 4' ($rate[3] - $rate[1]) 'phase 3-1' ($rate[4] - $rate[2]) 'phase 4-2')
    $started = @(foreach ($k in 1..4) { foreach ($g in @($p[$k].programs)) { if ([string]$g.processes -match '^0->[1-9]') { "$($g.program) (phase $k)" } } }) | Select-Object -Unique
    if ($started.Count) { $lines += ('  started during the trace, and may disturb it: ' + ($started -join ', ')) }
    $lines += '  (a leak of 300 MB/h fills 24 GB in about 3 days, as on head 7)'
    return $lines
}

Export-ModuleMember -Function Get-LabTraceVerdict, Get-LabPoolTags, Get-LabPoolTagTable, Find-LabTagDrivers, Get-LabTagMeaning, Get-LabHandleHolders, Format-LabKernelMemory, New-LabMemoryWatchScript
