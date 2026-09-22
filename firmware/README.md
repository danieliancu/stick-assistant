# Stick Assistant firmware (M5Stack M5StickS3)

This firmware turns the M5StickS3 into a push-to-talk voice terminal for the
Django backend in `../backend`. The device only records audio, uploads it,
shows the reply and plays the spoken answer. Speech recognition, the AI,
OpenAI credentials and the agenda database all stay on the server.

> **Status:** the firmware compiles (see CI). **It has not yet been tested on a
> physical M5StickS3.** Work through the hardware checklist below before relying
> on it.

## Hardware facts used

Taken from the official StickS3 docs and the M5Unified source (v0.2.23):

| Item | Value |
|---|---|
| SoC | ESP32-S3-PICO-1-N8R8: 8 MB flash, 8 MB **OPI** PSRAM |
| Display | ST7789P3, 135×240 (used landscape, 240×135) |
| BtnA | KEY1, front, **G11**. Also the power key: single click powers on/resets. |
| BtnB | KEY2, side, **G12** |
| Audio | ES8311 codec (I²C 0x18), MEMS mic on I2S1, AW8737 amplifier + 8 Ω speaker on I2S0 |
| Shared pins | MCLK G18, BCLK G17, LRCK G15 are shared by mic and speaker, so **only one runs at a time** |
| Power | M5PM1 PMIC (I²C 0x6E), 250 mAh battery |

The firmware always stops the speaker before recording and stops the
microphone (after all queued chunks are filled) before playback.

## Behaviour

| Action | Result |
|---|---|
| Hold **A**, speak, release | Records (max 12 s, 16 kHz mono 16-bit), uploads, plays the reply |
| Click **B** | Speaks today's agenda (built by the server from SQLite) |
| Any button while speaking | Stops playback |
| Hold **B** while powering on | **Mic test mode**: hold A to record, release to hear it back (no network) |

Screen states: `BOOTING`, `CONNECTING`, `READY`, `RECORDING`, `UPLOADING`,
`THINKING`, `SPEAKING`, `ERROR`, `OFFLINE` (and `MIC TEST`). Romanian letters
are shown without diacritics, because the built-in font lacks those glyphs; the
spoken audio is unaffected.

### Reliability

- Every recording gets a random UUID `request_id`. Retries after a timeout or
  dropped connection **reuse the same id**. Before re-uploading, the device asks
  `GET /api/voice/requests/<id>/` whether the server already has it. The server
  runs each command at most once, so retries never create duplicate tasks.
- `202 processing` → the device polls the status endpoint (every 1.5 s, up to 90 s).
- `429 busy` → the device waits and retries with the same id.
- Buffers are bounded:
  - recording: 384 KB in PSRAM, allocated once;
  - reply audio: at most 3 MB, in PSRAM, freed after playback;
  - JSON responses: at most 8 KB.
- Network errors never reboot the device. Wi-Fi reconnects in the background.
- If holding the front key causes problems on your unit (it is also the power
  key), build with `-DRECORD_MODE_HOLD=0` for press-to-start / press-to-stop.

## 1. Install PlatformIO (Windows PowerShell)

Either install the **PlatformIO IDE** extension in VS Code, or install the
command-line tool:

```powershell
python -m venv $HOME\.platformio\penv
& $HOME\.platformio\penv\Scripts\python.exe -m pip install platformio
$env:Path += ";$HOME\.platformio\penv\Scripts"
pio --version
```

## 2. Configure Wi-Fi, backend URL and device token

```powershell
cd firmware
Copy-Item include\secrets.example.h include\secrets.h
notepad include\secrets.h
```

`include/secrets.h` is gitignored. Set:

- `WIFI_SSID` / `WIFI_PASSWORD`: a **2.4 GHz** network.
- `DEVICE_API_TOKEN`: the same value as `DEVICE_API_TOKEN` in `backend/.env`.
- `BACKEND_BASE_URL`:
  - **Production:** `https://your-server` plus `BACKEND_ROOT_CA`, the PEM root
    certificate of the server's HTTPS certificate (for Let's Encrypt this is
    ISRG Root X1). Certificates are always verified; there is no insecure TLS
    mode.
  - **LAN development:** `http://<your PC's LAN IP>:8000` **and** uncomment
    `#define ALLOW_INSECURE_HTTP_DEV 1`. Without that line the build fails on
    purpose. `localhost` and `127.x` are also rejected at compile time, because
    on the device they would point to the device itself.

### Running the backend for LAN testing

```powershell
# Find your PC's IPv4 address (e.g. 192.168.1.50)
Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.IPAddress -notlike "127.*" } |
  Select-Object IPAddress, InterfaceAlias

# backend\.env must contain DJANGO_DEBUG=True for plain HTTP and the LAN IP in hosts:
#   DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.1.50

# Allow inbound TCP 8000 on private networks (run PowerShell as Administrator, once)
New-NetFirewallRule -DisplayName "Stick Assistant dev (8000)" -Direction Inbound `
  -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private

cd backend
.\.venv\Scripts\Activate.ps1
python manage.py runserver 0.0.0.0:8000
```

Check from another device on the network:
`http://192.168.1.50:8000/api/health/` should return `{"status": "ok", ...}`.

## 3. Build

```powershell
cd firmware
pio run
```

## 4. Before the first flash: back up XiaoZhi

**Flashing replaces the firmware currently on the device (for example the
XiaoZhi AI firmware).** Make a full 8 MB flash backup first; it can be restored
byte for byte.

1. Connect the StickS3 by USB-C and find its COM port:
   ```powershell
   pio device list
   # or:
   Get-PnpDevice -Class Ports -PresentOnly | Select-Object FriendlyName, Status
   ```
   The ESP32-S3 appears as "USB Serial Device (COMx)", USB VID `303A`.
2. Read the whole flash (replace `COM5`):
   ```powershell
   pio pkg exec -p tool-esptoolpy -- esptool.py --chip esp32s3 --port COM5 `
     read_flash 0 0x800000 xiaozhi-backup.bin
   ```
   Keep `xiaozhi-backup.bin` somewhere safe, outside the repository.

**To restore XiaoZhi later:**

```powershell
pio pkg exec -p tool-esptoolpy -- esptool.py --chip esp32s3 --port COM5 `
  write_flash 0x0 xiaozhi-backup.bin
```

Without a backup, reinstall XiaoZhi from its official source: M5Burner, or the
releases of `github.com/78/xiaozhi-esp32`. Check that the image is built for
the **M5StickS3** specifically.

## 5. Flash and monitor

```powershell
cd firmware
pio run -t upload --upload-port COM5
pio device monitor -p COM5 -b 115200
```

If the upload cannot connect, put the board into download mode. According to
M5Stack's StickS3 docs, a **long press of KEY1 (the front button)** enters
download mode; then retry the upload. Press KEY1 once to reset afterwards. The
serial monitor shows `[state] ...`, `[voice] request_id ...` and
`[audio] recorded ... samples` lines.

## 6. Hardware test checklist (manual)

Tick each item on the real device. None of these are covered by compilation.

**Boot and display**
- [ ] Boots to `CONNECTING`, then `READY` with Wi-Fi RSSI and battery % in the status bar.
- [ ] Wrong Wi-Fi password → `OFFLINE`, no reboot loop; fixing the network recovers it.

**Mic and speaker (hold B while powering on)**
- [ ] `MIC TEST` shown. Hold A and speak: the level bar moves.
- [ ] Release A: the recording plays back clearly at normal pitch and speed.
  Wrong speed would mean the 16 kHz mic rate is not honoured.
- [ ] Peak value shown is well above 300 when speaking and near 0 when silent.
- [ ] **Holding A for the full 12 s does not reset or power off the device.**
  If it does, rebuild with `-DRECORD_MODE_HOLD=0`.

**Voice flow (backend running, correct token)**
- [ ] Say "Amintește-mi mâine la 10 să sun la dentist.": `UPLOADING` → `THINKING`
  → `SPEAKING`; the reply is audible; one task appears in Django admin.
- [ ] "Ce am de făcut mâine?" → the reply mentions the dentist.
- [ ] "Mută-l la ora 12." → the same task moves to 12:00 (check admin).
- [ ] Click B → today's agenda is spoken.
- [ ] "Șterge taskul cu dentistul" → a confirmation question; answer "Nu" → the task still exists.
- [ ] Press a button during playback → playback stops.
- [ ] Very short tap on A → "Too short", nothing uploaded.

**Failure handling**
- [ ] Wrong `DEVICE_API_TOKEN` → "Device token rejected".
- [ ] Stop the backend → "Server unreachable", then `READY` again; no reboot.
- [ ] Unplug the router during `THINKING` → device retries/polls with the same
  request_id; afterwards Django admin shows **one** task and one VoiceRequest.

## Troubleshooting

| Symptom | Check |
|---|---|
| Build fails "Plain http:// requires ALLOW_INSECURE_HTTP_DEV" | Intended; see step 2 |
| `Server unreachable (connection refused)` | runserver bound to `0.0.0.0:8000`? Firewall rule? Same Wi-Fi? |
| HTTP 400 from Django on every request | LAN IP missing from `DJANGO_ALLOWED_HOSTS` |
| Requests redirect / fail with plain HTTP | `DJANGO_DEBUG=False` enables the HTTPS redirect; use `True` for LAN dev |
| `Device token rejected` | `DEVICE_API_TOKEN` in `secrets.h` ≠ `backend/.env` |
| HTTPS handshake fails | `BACKEND_ROOT_CA` must be the *root* CA of the server certificate chain |
| Chipmunk / slow playback of the mic test | Report it: indicates a sample-rate mismatch on this hardware |
| No COM port | Use a data-capable USB-C cable; enter download mode (long press KEY1) |

## Limitations

- Not yet verified on physical hardware (see checklist).
- The display cannot show diacritics (they are transliterated).
- Reply audio is downloaded completely before playback starts (bounded to 3 MB)
  rather than streamed.
- There is no OTA; updates require USB flashing.
