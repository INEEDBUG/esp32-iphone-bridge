param(
    [Parameter(Mandatory = $true)]
    [string] $ImagePath,
    [string] $ExpectedNonce
)

$ErrorActionPreference = 'Stop'
$phase = 'INPUT'

function Write-Result([string] $status, [string] $code, [hashtable] $fields = @{}) {
    $result = @{ status = $status; code = $code }
    foreach ($key in $fields.Keys) { $result[$key] = $fields[$key] }
    [Console]::WriteLine(($result | ConvertTo-Json -Compress -Depth 6))
}

function Await-Operation($operation, [type] $resultType) {
    $task = $script:asTask.MakeGenericMethod($resultType).Invoke($null, @($operation))
    if (-not $task.Wait([TimeSpan]::FromSeconds(15))) { throw 'ASYNC_TIMEOUT' }
    return $task.Result
}

try {
    if ([IO.Path]::GetExtension($ImagePath) -ine '.png' -or -not (Test-Path -LiteralPath $ImagePath -PathType Leaf)) {
        Write-Result 'ERROR' 'IMAGE_UNAVAILABLE'
        exit 2
    }
    $fileInfo = Get-Item -LiteralPath $ImagePath
    if ($fileInfo.Length -gt 20971520) {
        Write-Result 'ERROR' 'IMAGE_TOO_LARGE'
        exit 2
    }

    $markerValidated = $false
    if ($PSBoundParameters.ContainsKey('ExpectedNonce')) {
        $phase = 'MARKER_VALIDATE'
        if ($ExpectedNonce -notmatch '^[A-Za-z0-9_-]{1,128}$') {
            Write-Result 'ERROR' 'MARKER_NOT_VALIDATED'
            exit 2
        }
        $pythonPath = Join-Path $PSScriptRoot '..\.venv\Scripts\python.exe'
        if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
            Write-Result 'ERROR' 'MARKER_NOT_VALIDATED'
            exit 2
        }
        $pythonPath = (Resolve-Path -LiteralPath $pythonPath).Path
        $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
        $resolvedImagePath = (Resolve-Path -LiteralPath $ImagePath).Path
        # Keep the fixed -c source free of quote characters: Windows PowerShell
        # 5.1 strips embedded quotes when serializing native command arguments.
        $markerCode = 'import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from phonelab.visual_marker import decode_visual_marker; decode_visual_marker(Path(sys.argv[2]).read_bytes(),sys.argv[3]); print(chr(77)+chr(65)+chr(82)+chr(75)+chr(69)+chr(82)+chr(95)+chr(79)+chr(75))'
        $priorErrorAction = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $markerOutput = & $pythonPath -c $markerCode $projectRoot $resolvedImagePath $ExpectedNonce 2>$null
        $ErrorActionPreference = $priorErrorAction
        if ($LASTEXITCODE -ne 0 -or $markerOutput -ne 'MARKER_OK') {
            Write-Result 'ERROR' 'MARKER_NOT_VALIDATED'
            exit 2
        }
        $markerValidated = $true
    }

    $phase = 'OCR_INIT'
    Add-Type -AssemblyName System.Runtime.WindowsRuntime
    $null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
    $null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
    $null = [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
    $script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object {
            $_.Name -eq 'AsTask' -and $_.IsGenericMethodDefinition -and
            $_.GetParameters().Count -eq 1 -and
            $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
        } | Select-Object -First 1
    if ($null -eq $script:asTask) {
        Write-Result 'ERROR' 'OCR_BRIDGE_UNAVAILABLE'
        exit 2
    }

    $phase = 'FILE_OPEN'
    $storageFile = Await-Operation (
        [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]::GetFileFromPathAsync((Resolve-Path -LiteralPath $ImagePath).Path)
    ) ([Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime])
    $stream = Await-Operation (
        $storageFile.OpenAsync([Windows.Storage.FileAccessMode,Windows.Storage,ContentType=WindowsRuntime]::Read)
    ) ([Windows.Storage.Streams.IRandomAccessStream,Windows.Storage,ContentType=WindowsRuntime])

    $phase = 'IMAGE_DECODE'
    $decoder = Await-Operation (
        [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]::CreateAsync($stream)
    ) ([Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime])
    $bitmap = Await-Operation ($decoder.GetSoftwareBitmapAsync()) (
        [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
    )

    $phase = 'OCR_ENGINE'
    $engine = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]::TryCreateFromUserProfileLanguages()
    if ($null -eq $engine) {
        Write-Result 'ERROR' 'OCR_UNAVAILABLE'
        exit 2
    }
    if ($bitmap.PixelWidth -gt 4096 -or $bitmap.PixelHeight -gt 4096) {
        Write-Result 'ERROR' 'IMAGE_DIMENSION_UNSUPPORTED'
        exit 2
    }

    $phase = 'OCR_RECOGNIZE'
    $ocr = Await-Operation ($engine.RecognizeAsync($bitmap)) (
        [Windows.Media.Ocr.OcrResult,Windows.Foundation,ContentType=WindowsRuntime]
    )
    $ocrLines = @($ocr.Lines)
    $text = [regex]::Replace((($ocrLines | ForEach-Object { $_.Text }) -join ' '), '\s+', ' ')

    # Require identifying page labels before returning even synthetic counters.
    if (-not $markerValidated -and
        ($text -notmatch '(?i)PhoneLab' -or $text -notmatch '(?i)Browser-reported probe clicks')) {
        Write-Result 'ERROR' 'PAGE_NOT_RECOGNIZED'
        exit 2
    }
    $summaryLine = ''
    for ($lineIndex = 0; $lineIndex -lt $ocrLines.Count; $lineIndex++) {
        $candidateLine = [string] $ocrLines[$lineIndex].Text
        if ($candidateLine -match '(?i)Browser-reported probe clicks') {
            $summaryLine = $candidateLine
            if ($candidateLine -notmatch '(?i)Last' -and $lineIndex + 1 -lt $ocrLines.Count) {
                $nextLine = [string] $ocrLines[$lineIndex + 1].Text
                if ($nextLine -match '(?i)^\s*Last\b') { $summaryLine += ' ' + $nextLine }
            }
            break
        }
    }
    $summaryClicks = [regex]::Match($summaryLine, '(?i)Browser-reported probe clicks\D{0,20}(\d{1,4})')
    if (-not $summaryClicks.Success -and -not $markerValidated) {
        Write-Result 'ERROR' 'SYNTHETIC_FIELDS_NOT_RECOGNIZED'
        exit 2
    }
    $lastMatch = [regex]::Match($summaryLine, '(?i)Last\D{0,8}(none|0[1-9]|1[0-9]|2[0-5]|[1-9])(?!\d)')
    $summaryLast = if ($markerValidated) { $null } else { 'UNKNOWN' }
    $summaryStatus = 'PARTIAL'
    if ($summaryClicks.Success -and $lastMatch.Success) {
        $candidateLast = $lastMatch.Groups[1].Value
        if ($candidateLast -ieq 'none') {
            if ([int] $summaryClicks.Groups[1].Value -eq 0) {
                $summaryLast = 'none'
                $summaryStatus = 'OK'
            }
        } else {
            $summaryLast = '{0:D2}' -f [int] $candidateLast
            if ([int] $summaryClicks.Groups[1].Value -gt 0) { $summaryStatus = 'OK' }
        }
    }
    if ($markerValidated -and -not $lastMatch.Success) { $summaryLast = $null }

    $visibleProbes = @()
    foreach ($line in $ocrLines) {
        $lineText = [string] $line.Text
        # Windows OCR may emit full-width parenthesis glyphs; replacing them
        # one-for-one preserves original character offsets for word bounds.
        $matchingLine = $lineText.Replace([char] 0xFF08, '(').Replace([char] 0xFF09, ')')
        $wordSpans = @()
        $cursor = 0
        foreach ($word in $line.Words) {
            $wordText = [string] $word.Text
            if ([string]::IsNullOrWhiteSpace($wordText)) { continue }
            $offset = $lineText.IndexOf($wordText, $cursor, [StringComparison]::OrdinalIgnoreCase)
            if ($offset -lt 0) { continue }
            $rect = $word.BoundingRect
            $wordSpans += [pscustomobject]@{
                text = $wordText; offset = $offset; length = $wordText.Length
                x = [double] $rect.X; y = [double] $rect.Y
                right = [double] $rect.X + [double] $rect.Width
                bottom = [double] $rect.Y + [double] $rect.Height
            }
            $cursor = $offset + $wordText.Length
        }
        foreach ($probeMatch in [regex]::Matches($matchingLine, '(?i)\bProbe\s+(0[1-9]|1[0-9]|2[0-5])\s*\(\s*(\d{1,3})\s*\)')) {
            $matchStart = $probeMatch.Index
            $matchEnd = $matchStart + $probeMatch.Length
            $matchedWords = @($wordSpans | Where-Object {
                $_.offset -lt $matchEnd -and ($_.offset + $_.length) -gt $matchStart
            } | Sort-Object offset)
            if ($matchedWords.Count -eq 0) { continue }
            $expectedText = ($probeMatch.Value -replace '[^A-Za-z0-9]', '').ToLowerInvariant()
            $observedText = ((($matchedWords | ForEach-Object { $_.text }) -join '') -replace '[^A-Za-z0-9]', '').ToLowerInvariant()
            if ($observedText -ne $expectedText) { continue }

            $left = [double] (($matchedWords | Measure-Object x -Minimum).Minimum)
            $top = [double] (($matchedWords | Measure-Object y -Minimum).Minimum)
            $right = [double] (($matchedWords | Measure-Object right -Maximum).Maximum)
            $bottom = [double] (($matchedWords | Measure-Object bottom -Maximum).Maximum)
            if ($left -lt 0 -or $top -lt 0 -or $right -gt $bitmap.PixelWidth -or
                $bottom -gt $bitmap.PixelHeight -or $right -le $left -or $bottom -le $top) { continue }
            $bounds = [pscustomobject]@{
                x = [math]::Round($left, 1); y = [math]::Round($top, 1)
                width = [math]::Round(($right - $left), 1)
                height = [math]::Round(($bottom - $top), 1)
            }
            $visibleProbes += [pscustomobject]@{
                id = $probeMatch.Groups[1].Value
                count = [int] $probeMatch.Groups[2].Value
                image_text_bounds = $bounds
            }
        }
    }
    $visibleProbes = @($visibleProbes | Sort-Object id)

    $status = if ($summaryStatus -eq 'OK') { 'OK' } else { 'PARTIAL' }
    $code = if ($summaryStatus -eq 'OK') { 'NONE' } elseif (-not $summaryClicks.Success) { 'SUMMARY_NOT_VISIBLE' } else { 'SUMMARY_LAST_UNKNOWN' }
    Write-Result $status $code @{
        summary_clicks = if ($summaryClicks.Success) { [int] $summaryClicks.Groups[1].Value } else { $null }
        summary_last = $summaryLast
        visible_probes = $visibleProbes
    }
    if ($status -eq 'OK') { exit 0 } elseif ($markerValidated) { exit 0 } else { exit 1 }
} catch {
    Write-Result 'ERROR' ("LOCAL_" + $phase.ToUpperInvariant() + "_FAILED")
    exit 2
} finally {
    if ($null -ne $stream) { try { $stream.Dispose() } catch {} }
}
