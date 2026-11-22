<#
  Draws the Agent Arena icon (the approved robot head) at 16/24/32/48/64/256 px and
  writes a PNG-compressed .ico. Deterministic; no external assets.
#>
param([Parameter(Mandatory)][string]$Out)
Add-Type -AssemblyName System.Drawing
$sizes = 16, 24, 32, 48, 64, 256
$images = foreach ($s in $sizes) {
    $bmp = New-Object Drawing.Bitmap $s, $s
    $g = [Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = 'AntiAlias'; $g.Clear([Drawing.Color]::Transparent)
    $u = $s / 48.0                                   # robot geometry from the 48-unit SVG
    $teal = [Drawing.Color]::FromArgb(255, 0x55, 0xD6, 0xBE); $ink = [Drawing.Color]::FromArgb(255, 7, 23, 34)
    $ivory = [Drawing.Color]::FromArgb(255, 0xF1, 0xED, 0xD8); $screen = [Drawing.Color]::FromArgb(255, 0x14, 0x25, 0x36)
    $pen = New-Object Drawing.Pen $ink, ([Math]::Max(1, 3 * $u))
    $g.FillRectangle((New-Object Drawing.SolidBrush $ink), [single](21 * $u), [single](1 * $u), [single](6 * $u), [single](5 * $u))   # antenna
    $g.FillRectangle((New-Object Drawing.SolidBrush $teal), [single](4 * $u), [single](6 * $u), [single](40 * $u), [single](36 * $u)) # head
    $g.DrawRectangle($pen, [single](4 * $u), [single](6 * $u), [single](40 * $u), [single](36 * $u))
    $g.FillRectangle((New-Object Drawing.SolidBrush $screen), [single](10 * $u), [single](14 * $u), [single](28 * $u), [single](18 * $u)) # face screen
    $g.FillRectangle((New-Object Drawing.SolidBrush $ivory), [single](14 * $u), [single](18 * $u), [single](7 * $u), [single](7 * $u))   # eyes
    $g.FillRectangle((New-Object Drawing.SolidBrush $ivory), [single](27 * $u), [single](18 * $u), [single](7 * $u), [single](7 * $u))
    if ($s -ge 32) { $g.FillRectangle((New-Object Drawing.SolidBrush $ivory), [single](20 * $u), [single](28 * $u), [single](8 * $u), [single](2 * $u)) }  # mouth
    $g.Dispose()
    $ms = New-Object IO.MemoryStream; $bmp.Save($ms, [Drawing.Imaging.ImageFormat]::Png); $bmp.Dispose()
    , $ms.ToArray()
}
$w = New-Object IO.BinaryWriter ([IO.File]::Create($Out))
$w.Write([uint16]0); $w.Write([uint16]1); $w.Write([uint16]$sizes.Count)
$offset = 6 + 16 * $sizes.Count
for ($i = 0; $i -lt $sizes.Count; $i++) {
    $s = $sizes[$i]; $dim = $(if ($s -ge 256) { 0 } else { $s })
    $w.Write([byte]$dim); $w.Write([byte]$dim); $w.Write([byte]0); $w.Write([byte]0)
    $w.Write([uint16]1); $w.Write([uint16]32); $w.Write([uint32]$images[$i].Length); $w.Write([uint32]$offset)
    $offset += $images[$i].Length
}
foreach ($img in $images) { $w.Write($img) }
$w.Close()
"icon written: $Out"
