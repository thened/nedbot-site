# ─────────────────────────────────────────────
#  nedbot gateway  ·  https://gateway.nedbot.site
#  Starts a local JSON server on $PORT and the
#  Cloudflare tunnel in one go. Ctrl+C kills both.
# ─────────────────────────────────────────────

$PORT        = 8888
$TUNNEL_NAME = "nedbot-gateway"
$ALLOWED_ORIGIN = "https://bet.nedbot.site"

# ── Add commands here ─────────────────────────
# Each entry: "name" = { param($body) ... return hashtable }

$Commands = [ordered]@{

    "ping" = {
        param($body)
        @{ status = "ok"; pong = $true; time = (Get-Date -Format "o") }
    }

    "echo" = {
        param($body)
        @{ status = "ok"; echo = $body }
    }

}

# ─────────────────────────────────────────────

function Write-Json ($res, $obj, $status = 200) {
    $json  = $obj | ConvertTo-Json -Compress -Depth 10
    $bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
    $res.StatusCode      = $status
    $res.ContentType     = "application/json; charset=utf-8"
    $res.ContentLength64 = $bytes.Length
    $res.OutputStream.Write($bytes, 0, $bytes.Length)
    $res.Close()
}

function Start-Server {
    $listener = [System.Net.HttpListener]::new()
    $listener.Prefixes.Add("http://localhost:$PORT/")
    $listener.Start()
    Write-Host " Server  →  http://localhost:$PORT" -ForegroundColor Green

    while ($listener.IsListening) {
        $ctx = $null
        try { $ctx = $listener.GetContext() } catch { break }

        $req = $ctx.Request
        $res = $ctx.Response
        $res.Headers.Add("Access-Control-Allow-Origin",  $ALLOWED_ORIGIN)
        $res.Headers.Add("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        $res.Headers.Add("Access-Control-Allow-Headers", "Content-Type")

        $method = $req.HttpMethod
        $path   = $req.Url.AbsolutePath

        Write-Host "$method $path" -ForegroundColor DarkGray

        # CORS preflight
        if ($method -eq "OPTIONS") {
            $res.StatusCode = 204; $res.Close(); continue
        }

        # Health check
        if ($method -eq "GET" -and $path -eq "/health") {
            Write-Json $res @{ status = "ok"; tunnel = $TUNNEL_NAME }
            continue
        }

        # Command endpoint
        if ($method -eq "POST" -and $path -eq "/command") {
            try {
                $reader  = [System.IO.StreamReader]::new($req.InputStream)
                $rawBody = $reader.ReadToEnd()
                $reader.Close()
                $body = $rawBody | ConvertFrom-Json
                $cmd  = $body.command

                if (-not $cmd) {
                    Write-Json $res @{ error = "Missing 'command' field" } 400
                    continue
                }

                if ($Commands.Contains($cmd)) {
                    $result = & $Commands[$cmd] $body
                    Write-Json $res $result
                } else {
                    Write-Json $res @{ error = "Unknown command: $cmd"; available = @($Commands.Keys) } 400
                }
            } catch {
                Write-Json $res @{ error = $_.Exception.Message } 500
            }
            continue
        }

        Write-Json $res @{ error = "Not found" } 404
    }
}

# ── Start tunnel ──────────────────────────────
Write-Host ""
Write-Host " Starting nedbot gateway" -ForegroundColor Cyan
Write-Host " Tunnel  →  https://gateway.nedbot.site" -ForegroundColor Cyan

$tunnel = Start-Process cloudflared `
    -ArgumentList "tunnel run $TUNNEL_NAME" `
    -PassThru -NoNewWindow

Start-Sleep -Seconds 2

try {
    Start-Server
} finally {
    Write-Host "`n Shutting down..." -ForegroundColor Yellow
    if ($tunnel -and -not $tunnel.HasExited) {
        Stop-Process -Id $tunnel.Id -ErrorAction SilentlyContinue
    }
}
