# Mac setup guide (one-time, ~15 min)

PresenceSync needs four short-lived pieces of data from a Mac that's signed
into your Apple ID:

- `BeaconStore.key` — 32-byte AES key that decrypts your AirTag master keys
- `OwnedBeacons/*.record` — encrypted master keys, one per Find My item
- `BeaconNamingRecord/` + `KeyAlignmentRecords/` — names + rolling-key sync data
- (`FMIPDataManager.bplist` + `FMFDataManager.bplist` are also captured, but PresenceSync doesn't currently use them)

Everything gets packaged into a single `presencesync-bundle.tar.gz` you upload
to the PresenceSync add-on's web UI. **The Mac is never touched again** —
PresenceSync queries Apple's gateway directly from inside Home Assistant from
that point on.

> ⚠️ Two macOS security features must be temporarily disabled so the key
> extractor's debugger can attach to Apple-signed binaries. **Both can be
> re-enabled after extraction completes** — PresenceSync only needs the
> resulting bundle, not the relaxed state.

## 0. What you need

- A Mac signed in to the Apple ID that owns your AirTags (Intel **or** Apple Silicon, macOS 13+).
- 15 minutes uninterrupted plus two reboots.
- A USB keyboard if the Mac is headless (you'll need it briefly to boot into Recovery).
- The admin password for the Mac.

## 1. Disable System Integrity Protection

SIP prevents non-Apple code from attaching debuggers to Apple binaries.

### Apple Silicon (M1 / M2 / M3 / M4)

1. **Shut down** the Mac.
2. Hold the **Power button** until you see "Loading startup options."
3. Click **Options → Continue**. Pick your admin user, authenticate.
4. Top menu bar → **Utilities → Terminal**.
5. Run:
   ```bash
   csrutil disable
   ```
   Type `y`, enter your admin password.
6. Top-left  menu → **Restart**.

### Intel

1. **Restart** while holding `⌘R` to enter Recovery Mode.
2. Top menu bar → **Utilities → Terminal**.
3. Run `csrutil disable`.
4. Top-left  menu → **Restart**.

### Verify after reboot

```bash
csrutil status
# expected: System Integrity Protection status: disabled.
```

## 2. Set the AMFI boot-arg

```bash
sudo nvram boot-args="amfi_get_out_of_my_way=0x1"
sudo reboot
```

After reboot, verify:

```bash
nvram boot-args
# expected:
# boot-args  amfi_get_out_of_my_way=0x1
```

## 3. Sign into Find My once

Open the **Find My** app once. Sign in if asked. Wait until your AirTags
appear in the **Items** tab — this confirms Apple's daemon has fetched the
master keys we're about to extract. You can quit Find My right after.

## 4. Grant Full Disk Access to Terminal

System Settings → Privacy & Security → **Full Disk Access** → `+` → add
**Terminal** (`/System/Applications/Utilities/Terminal.app`) → toggle on.
Close + reopen Terminal so the new permission applies.

## 5. Run the extractors

```bash
git clone https://github.com/PrayerfulDrop/findmy-key-extractor.git ~/src/findmy-key-extractor
cd ~/src/findmy-key-extractor
git checkout x86_64-port

# Phase A: the three keys from findmylocateagent + FindMy.app
sudo ./extract.sh

# Phase B: the BeaconStore key from searchpartyuseragent
sudo ./extract_beaconstore.sh
```

You should now have **four** files in `keys/`:

| File | Size | What |
| --- | --- | --- |
| `LocalStorage.key` | 32 B | not used by PresenceSync |
| `FMFDataManager.bplist` | 171 B | not used by PresenceSync |
| `FMIPDataManager.bplist` | 171 B | not used by PresenceSync |
| `BeaconStore.key` | 32 B | ⭐ the one that matters |

If any are missing, see [Troubleshooting](#troubleshooting-the-extractor).

## 6. Package the bundle

```bash
./bundle.sh
```

That produces `presencesync-bundle.tar.gz` (~100 KB) at the root of the
extractor repo. Contents:

```
BeaconStore.key
OwnedBeacons/*.record           # 13 encrypted master keys
BeaconNamingRecord/             # names for items + accessory groups
KeyAlignmentRecords/            # rolling-key date sync
OwnedBeaconGroups/              # AirPods pairing
manifest.json                   # produced_at + extractor version
```

> ⚠️ The bundle gives anyone who has it the ability to read your AirTag
> locations. Treat it like a password. After uploading to HA, delete it from
> the Mac if you don't want a copy lying around.

## 7. Upload to PresenceSync

Get `presencesync-bundle.tar.gz` to a device that can open Home Assistant in
a browser (AirDrop to your phone, save to a USB stick, etc.) and use the
PresenceSync add-on's web UI step 3 to upload it. Done with the Mac.

## 8. (Optional) Roll back the security settings

PresenceSync **does not** need SIP or AMFI relaxed at runtime — only the
one-time extraction step did. Once the bundle is uploaded and you've
confirmed AirTags appear in HA, you can roll back:

```bash
sudo nvram -d boot-args
```

Reboot into Recovery Mode (same procedure as step 1) and:

```bash
csrutil enable
```

Reboot again. PresenceSync continues to work — the AirTag master keys
inside your uploaded bundle are still valid.

You only need to repeat the disable / extract / re-enable dance if:

- You sign out of iCloud and back in (Apple rotates the keys).
- You change your Apple ID password.
- You upgrade macOS to a major version that changes the encryption scheme (rare).

## Troubleshooting the extractor

**`extract.sh` runs but produces no files.** Stale `lldb --wait-for`
instances from prior failed runs survive across script invocations:

```bash
sudo pkill -9 lldb
sudo pkill -9 findmylocateagent
./extract.sh
```

**`extract_beaconstore.sh` produces no `BeaconStore.key`.** Make sure the
sudo session is primed (the script runs lldb as root). If you see only
`(lldb) process attach --name "searchpartyuseragent" --waitfor` in the log
with nothing after, the launchctl kickstart didn't respawn the agent in
time — re-run the script.

**`csrutil status` still says enabled after reboot.** You didn't actually
run `csrutil disable` in Recovery Mode. Running it from regular Terminal
fails silently on Apple Silicon — you must boot into Recovery.

**`nvram boot-args` is empty after reboot.** Apple Silicon Macs sometimes
reset NVRAM during reboot if Secure Boot is at the default level. Recovery
Mode → Utilities → Startup Security Utility → "Reduced Security" → allow
user-management of kernel extensions, then retry the boot-arg.

**`lldb` says "process timeout" or "operation not permitted."** AMFI is
still active. Verify with `nvram boot-args` — it must show
`amfi_get_out_of_my_way=0x1`.
