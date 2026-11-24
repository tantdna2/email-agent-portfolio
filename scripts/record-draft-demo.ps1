#Requires -Version 5.1
[CmdletBinding()]
param(
    [switch]$Rehearse
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-DemoRequest {
    param(
        [string]$Method,
        [string]$Path,
        [hashtable]$Body
    )

    $request = [System.Net.Http.HttpRequestMessage]::new(
        [System.Net.Http.HttpMethod]::new($Method), "http://127.0.0.1:8000$Path")
    $response = $null
    try {
        if ($Body) {
            $request.Content = [System.Net.Http.StringContent]::new(
                (ConvertTo-Json -InputObject $Body -Compress), [Text.Encoding]::UTF8, 'application/json')
        }
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        $null = $response.EnsureSuccessStatusCode()
        ConvertFrom-Json -InputObject ($response.Content.ReadAsStringAsync().GetAwaiter().GetResult())
    }
    finally {
        if ($response) { $response.Dispose() }
        $request.Dispose()
    }
}

function Show-Card {
    param([string]$Title, [string[]]$Lines)

    if (-not $Rehearse) { Clear-Host }
    Write-Host ''
    Write-Host $Title -ForegroundColor Cyan
    Write-Host ''
    foreach ($line in $Lines) { Write-Host $line }
    Write-Host ''
}

function Wait-ForEnter {
    param([string]$Prompt = 'Press Enter for the next step')

    if (-not $Rehearse) { $null = Read-Host $Prompt }
}

function Confirm-Approval {
    param($Reply, [string]$Status, [int]$Count)

    if ($Reply.status -cne $Status -or $Reply.simulated_send_count -ne $Count) {
        throw 'Unexpected approval result.'
    }
}

# Load the demo fixture and aggregate counts from the locked archive.
$readEvidence = '
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, sys.argv[1])
import demo_daily_sender_digest as fixture
from evaluate_daily_drafts import score

archive = Path(sys.argv[2])
_, totals, _ = score(archive)
review = json.loads((archive / "review.json").read_text(encoding="utf-8"))
languages = Counter(entry["case"]["language"] for entry in review)
print(json.dumps(dict(
    fixture=dict(sender=fixture.SENDER, subject=fixture.SUBJECT, body=fixture.BODY,
                 instruction=fixture.INSTRUCTION, initial=fixture.INITIAL, revised=fixture.REVISED),
    scores=totals,
    languages=languages,
)))
'

Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
$client = $null
try {
    $failure = 'Could not load the sample and archived grades. Check WSL Ubuntu and the checkout.'
    $evidenceJson = $readEvidence | & wsl.exe -d Ubuntu -- python3 -B - scripts evals/results/heldout-a3160ef 2>$null
    if ($LASTEXITCODE -ne 0) { throw 'Offline evidence could not be read.' }
    $evidence = ConvertFrom-Json -InputObject ($evidenceJson -join "`n")
    $sample = $evidence.fixture

    $failure = 'Could not read the local demo at 127.0.0.1:8000. Check Uvicorn.'
    Add-Type -AssemblyName System.Net.Http
    $handler = [System.Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $handler.UseCookies = $false
    $client = [System.Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(10)
    $outbox = Invoke-DemoRequest -Method GET -Path '/outbox'
    $failure = 'The outbox must be empty. Restart Uvicorn for a fresh take.'
    if ($outbox.simulated_send_count -ne 0 -or @($outbox.messages).Count -ne 0) {
        throw 'Expected an empty simulated outbox.'
    }

    Show-Card 'Email Agent | Approval after an edit' @(
        'Local FastAPI demo | Canned drafts, real approval logic',
        'Gmail and Telegram are simulated',
        '',
        "From: $($sample.sender)",
        "Subject: $($sample.subject)",
        $sample.body
    )
    Wait-ForEnter

    $failure = 'Could not create the expected v1 draft. Restart Uvicorn for a fresh take.'
    $v1 = Invoke-DemoRequest -Method POST -Path '/draft'
    $replySubject = "Re: $($sample.subject)"
    if ($v1.version -ne 1 -or $v1.status -cne 'awaiting_review' -or
        $v1.draft.subject -cne $replySubject -or $v1.draft.to -cne $sample.sender -or
        -not [string]::Equals($v1.draft.draft_body, $sample.initial, [StringComparison]::Ordinal) -or
        -not $v1.edit_token -or -not $v1.approval_token) {
        throw 'Unexpected initial draft.'
    }
    Show-Card 'Create the first draft' @(
        'POST /draft',
        "Version: $($v1.version)    Status: $($v1.status)",
        "Subject: $($v1.draft.subject)",
        '',
        $v1.draft.draft_body
    )
    Wait-ForEnter

    $failure = 'Could not create the expected v2 revision. Restart Uvicorn and retry.'
    $v2 = Invoke-DemoRequest -Method POST -Path '/draft/edit' -Body @{
        edit_token = $v1.edit_token
        instruction = $sample.instruction
    }
    if ($v2.version -ne 2 -or $v2.status -cne 'awaiting_review' -or
        $v2.draft.subject -cne $replySubject -or $v2.draft.to -cne $sample.sender -or
        -not [string]::Equals($v2.draft.draft_body, $sample.revised, [StringComparison]::Ordinal) -or
        -not $v2.approval_token -or $v2.approval_token -ceq $v1.approval_token) {
        throw 'Unexpected revised draft.'
    }
    Show-Card 'Edit the draft' @(
        'POST /draft/edit',
        "Edit request: $($sample.instruction)",
        "Version: $($v2.version)    Status: $($v2.status)",
        '',
        $v2.draft.draft_body
    )
    Wait-ForEnter

    $failure = 'The v1 approval must return stale with zero simulated sends.'
    $stale = Invoke-DemoRequest -Method POST -Path '/draft/approve' -Body @{ approval_token = $v1.approval_token }
    Confirm-Approval $stale 'stale' 0
    Show-Card 'Try the old v1 approval' @(
        'POST /draft/approve',
        "Status: $($stale.status)",
        "Simulated sends: $($stale.simulated_send_count)"
    )
    Wait-ForEnter

    $failure = 'The v2 approval must return sent with one simulated send.'
    $approveV2 = @{ approval_token = $v2.approval_token }
    $approved = Invoke-DemoRequest -Method POST -Path '/draft/approve' -Body $approveV2
    Confirm-Approval $approved 'sent' 1
    Show-Card 'Approve the revised v2 draft' @(
        'POST /draft/approve',
        "Status: $($approved.status)",
        "Simulated sends: $($approved.simulated_send_count)"
    )
    Wait-ForEnter

    $failure = 'Replaying the v2 approval must keep the simulated send count at one.'
    $replayed = Invoke-DemoRequest -Method POST -Path '/draft/approve' -Body $approveV2
    Confirm-Approval $replayed 'sent' 1
    Show-Card 'Replay the same v2 approval' @(
        'POST /draft/approve',
        "Status: $($replayed.status)",
        "Simulated sends: $($replayed.simulated_send_count)"
    )
    Wait-ForEnter

    $failure = 'The simulated outbox must contain exactly one copy of the v2 reply.'
    $outbox = Invoke-DemoRequest -Method GET -Path '/outbox'
    $messages = @($outbox.messages)
    if ($outbox.simulated_send_count -ne 1 -or $messages.Count -ne 1 -or
        $messages[0].to -cne $v2.draft.to -or $messages[0].subject -cne $v2.draft.subject -or
        -not [string]::Equals($messages[0].draft_body, $v2.draft.draft_body, [StringComparison]::Ordinal)) {
        throw 'Unexpected simulated outbox contents.'
    }
    $counts = @($stale.simulated_send_count, $approved.simulated_send_count, $replayed.simulated_send_count)
    Show-Card 'Verify the simulated outbox' @(
        'GET /outbox',
        "Approval send counts: $($counts -join ' / ')",
        "Outbox messages: $($messages.Count)",
        '',
        $messages[0].draft_body
    )
    Write-Host 'PASS: The outbox contains exactly the v2 reply.' -ForegroundColor Green
    Wait-ForEnter

    $failure = 'Could not display the archived evaluation scores.'
    $baseline = $evidence.scores.baseline
    $candidate = $evidence.scores.candidate
    $row = '{0,-30}{1,-12}{2}'
    Show-Card 'Archived held-out prompt evaluation' @(
        ('{0} model responses across {1} fictional emails' -f ($baseline.cases + $candidate.cases), $baseline.cases),
        ('{0} Vietnamese / {1} English' -f $evidence.languages.vi, $evidence.languages.en),
        'Recomputed from locked grades in evals/results/heldout-a3160ef',
        '',
        ($row -f '', 'Baseline', 'Candidate'),
        ($row -f 'All key facts kept', "$($baseline.facts_kept)/$($baseline.cases)", "$($candidate.facts_kept)/$($candidate.cases)"),
        ($row -f 'Needed a meaningful edit', "$($baseline.needs_edit)/$($baseline.cases)", "$($candidate.needs_edit)/$($candidate.cases)")
    )
    Wait-ForEnter

    Show-Card 'Email Agent | Run the offline demo' @(
        'From the repo root in PowerShell with WSL Ubuntu:',
        'wsl -d Ubuntu -- python3 scripts/demo_daily_sender_digest.py'
    )
    Wait-ForEnter 'Press Enter after stopping the recording'
}
catch {
    # Request errors can contain approval tokens, so show only a fixed message.
    Write-Host ''
    Write-Host "STOP. $failure" -ForegroundColor Red
    Wait-ForEnter 'Press Enter after stopping the recording'
    exit 1
}
finally {
    if ($client) { $client.Dispose() }
    Pop-Location
}
