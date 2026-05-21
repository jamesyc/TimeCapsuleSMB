# TimeCapsuleSMB GUI UX Brainstorm

This document describes what the macOS GUI should feel like and how its user
experience should be shaped. It is based on the CLI product surface and README,
translated into a native app product surface.

## Product Direction

The app should feel like a device manager for old Time Capsules, not like a
terminal wrapper.

The main user job is:

1. Find one or more Time Capsules on the network.
2. Save them as named devices.
3. Install or update modern SMB support.
4. Verify Finder and Time Machine readiness.
5. Recover from common disk, metadata, Bonjour, SSH, reboot, or NetBSD4 issues.
6. Remove the install safely if desired.

The app should not expose repo-oriented setup commands. `bootstrap`, `paths`,
and `validate-install` should run as app readiness checks in the background.
Normal users should never see those as actions. If the bundled app is damaged or
missing binaries, the app should say the app install is damaged and point the
user to reinstall the app.

The app should support multiple saved Time Capsules from the beginning. A user
may own more than one unit, may test Gen 5 and Gen 1-4 devices side by side, or
may need to manage a friend's device temporarily.

## Visual Tone

This should be a quiet Mac utility:

- sidebar + detail layout
- dense but readable status rows
- clear progress timelines for long operations
- simple colored health badges
- native controls and sheets
- no decorative landing page
- no raw JSON as a primary UX
- no "wizard wall of text"

Use short, concrete text. Prefer device facts and next actions over explanation.
Deep logs, raw events, payload details, and advanced flags should exist, but
behind disclosure controls.

## App Shell

Recommended top-level structure:

- Sidebar
  - All Time Capsules
  - Add Time Capsule
  - Activity
  - Settings
  - Help

- Device detail area
  - selected device summary
  - primary action
  - health and warnings
  - workflow tabs or sections

- Bottom or collapsible activity drawer
  - latest operation progress
  - log lines
  - copy diagnostics button

The sidebar device rows should show:

- user nickname
- Bonjour/device name
- host or IP
- health badge
- last seen time
- small NetBSD4 marker when relevant

Example row statuses:

- Not set up
- Ready to install
- Installing
- Rebooting
- Verifying
- Healthy
- Needs activation
- Warning
- Failed
- Removed
- Offline

## First Launch

The first launch should do background app readiness immediately:

- verify bundled helper/runtime is present
- verify bundled Samba, mDNS, NBNS, scripts, and manifest are present
- check app version support, using cached network metadata when available
- detect host macOS version and Time Machine warning status
- start Bonjour discovery

The user-facing first screen should be an empty device list with active
discovery results, not a setup checklist.

Empty state:

- title: "No Time Capsules saved"
- primary button: "Add Time Capsule"
- secondary button: "Enter Address Manually"
- inline list of discovered candidates if any

Do not ask the user to run setup or install dependencies. If a required bundled
asset is missing, show a blocking app readiness alert:

"TimeCapsuleSMB is incomplete. Reinstall the app."

Advanced details can show the failed checks, but the main remediation should be
reinstalling the app.

## Multiple Saved Devices

Each saved device should be a profile with a stable app-level identity.

User-visible profile fields:

- nickname
- Bonjour name
- host/IP
- model
- generation
- OS family
- payload family
- last known SMB URL
- last doctor result
- last successful deploy/update time
- NetBSD4 activation reminder status
- flash backup availability if any

Credentials should live in Keychain. The app should not repeatedly ask for the
password unless the Keychain item is missing or authentication fails.

The app should allow:

- rename device
- forget device
- refresh identity
- update saved host/IP
- replace stored password
- duplicate profile is detected and merged or warned

Discovery should not create profiles automatically. It should present candidates
that can be saved.

## Add Device Flow

The add-device flow should be one guided panel with clear stages:

1. Discover
2. Select
3. Authenticate
4. Enable SSH if needed
5. Identify device
6. Save

Discovery screen:

- list AirPort/Time Capsule candidates from Bonjour
- show name, host, IPv4, model hint, and service status
- support manual address entry
- warn when only link-local `169.254.x.x` is available

Authentication screen:

- password field labeled "Time Capsule password"
- short note: "This password is also used for SMB login after install."
- "Save in Keychain" should be on by default

SSH state handling:

- if SSH is reachable and auth works, continue
- if SSH is closed, explain that the app can enable SSH using the Time Capsule
  admin protocol and the device will reboot
- after enabling SSH, show a reboot wait progress state
- if password fails, ask again without saving a broken profile

Device identity result:

- model and syAP
- NetBSD version and architecture
- supported/unsupported status
- payload family
- expected behavior:
  - Gen 5 / NetBSD 6: persistent install, reboot after deploy
  - Gen 1-4 / NetBSD 4: deploy activates now, needs activation after later reboots unless flash patch is used

Save screen:

- nickname defaulted from Bonjour name
- primary button: "Save Time Capsule"
- next suggested action: "Install SMB"

## Device Dashboard

The device dashboard should answer four questions at a glance:

- Is this device reachable?
- Is TimeCapsuleSMB installed?
- Is SMB currently working?
- What should I do next?

Suggested layout:

- Header
  - nickname
  - model/generation
  - health badge
  - last checked

- Primary action strip
  - "Install SMB" for not installed
  - "Update SMB" for installed but app bundle has newer payload
  - "Run Activation" for NetBSD4 deployed but inactive
  - "Open in Finder" for healthy devices
  - "Run Checkup" for warning/failed state

- Health sections
  - Connection
  - Runtime
  - Finder/Bonjour
  - SMB auth
  - Time Machine

- Secondary actions
  - Maintenance
  - Uninstall
  - Advanced

The dashboard should run a lightweight refresh when selected. Full doctor can be
manual or automatically offered after deploy/update.

## Known macOS Time Machine Warnings

The app should proactively warn when the host macOS version is known to have
Time Machine network backup issues.

Known warning policy:

- macOS 15.7.5
- macOS 15.7.6
- macOS 15.7.7
- macOS 26.4.x

Warning behavior:

- show a top-level banner on launch when the current Mac matches
- repeat the warning before deploy verification if the user expects Time Machine
  validation
- do not block installation
- make clear that normal Finder SMB file sharing can still work
- make clear that Time Machine failure on this Mac may be a macOS issue, not a
  TimeCapsuleSMB install failure

Suggested text:

"This macOS version has known Time Machine network backup issues. Finder SMB
access may still work, but Time Machine validation may fail on this Mac. Use a
different macOS version or update macOS before treating Time Machine failure as a
device problem."

This should be data-driven so a later app update can change the warning list
without redesigning the UI.

## Install And Update UX

The deploy CLI should become an "Install SMB" or "Update SMB" workflow.

The workflow should always start with a plan.

Plan screen should show:

- target device
- detected generation and OS
- payload family
- install location on disk
- files to upload, summarized
- mDNS/NBNS behavior
- reboot behavior
- NetBSD4 activation behavior
- expected downtime
- whether Time Machine warning applies on this Mac

The normal user should see:

- "This will install Samba 4.24.1 on the Time Capsule."
- "The device will reboot and may be unavailable for several minutes."
- "After it returns, the app will verify Finder and SMB access."

Advanced disclosure should show:

- upload count
- boot files
- payload directory
- selected volume
- mount wait setting
- NBNS toggle
- debug logging toggle

Deploy progress should be a timeline:

- Preparing
- Checking device
- Checking bundled files
- Finding disk
- Building plan
- Uploading
- Syncing to disk
- Rebooting or activating
- Waiting for device
- Verifying SMB
- Done

Post-success screen:

- show SMB URL
- "Open in Finder"
- "Run Time Machine Check"
- "Run Full Checkup"
- for NetBSD4, show activation reminder:
  "This device needs activation after each reboot unless the flash boot hook is patched."

## Doctor / Checkup UX

The CLI `doctor` should be a "Checkup" workflow.

It should group results by domain:

- App
  - bundled files
  - local helper/tools
  - app version
- Device
  - SSH
  - model and OS
  - payload family
  - interface/IP
- Runtime
  - Samba process
  - TCP 445
  - mDNS takeover
  - NBNS if enabled
  - persistent xattr database
- Finder/Bonjour
  - advertised names
  - resolved addresses
  - `_smb._tcp`
  - `_adisk._tcp`
- SMB
  - authenticated listing
  - share names
  - file operation test
- Time Machine
  - share flags
  - host macOS warning

Each check row should have:

- status icon: pass, warning, fail, info
- human message
- "What to do" action if available
- raw detail disclosure

Doctor failure should not be a wall of logs. The top should say:

- "SMB is not running"
- "Bonjour is advertising the wrong name"
- "The disk did not mount"
- "This may be a macOS Time Machine issue"

Recovery actions should be buttons:

- Retry Checkup
- Reboot Device
- Run Activation
- Run Disk Repair
- Repair xattrs
- Open Finder to SMB URL
- Copy Diagnostics

## Maintenance UX

Maintenance should be available per saved device. It should be visually
separate from the primary install/checkup path because several actions are
destructive or specialized.

Recommended sections:

- NetBSD4 Activation
- Disk Repair
- File Metadata Repair
- Uninstall
- Firmware Flash, disabled or experimental

### NetBSD4 Activation

Show this only when the saved or probed device is NetBSD4, or keep it disabled
with an explanation.

States:

- not needed
- needs activation
- planning
- ready to activate
- activating
- verifying
- active
- failed

UX:

- "Start SMB now"
- dry-run plan shown first
- confirmation required before modifying runtime state
- after success, show "Open in Finder" and "Run Checkup"

### Disk Repair

This maps to `fsck`.

The UX should be careful because it can stop sharing, unmount disks, run
`fsck_hfs`, and reboot.

Flow:

1. List mounted HFS volumes.
2. Select volume.
3. Build repair plan.
4. Confirm.
5. Run repair.
6. Reboot/wait if required.
7. Suggest Checkup.

Volume picker should show:

- device path, for example `/dev/dk2`
- mountpoint
- volume name
- internal/external marker

Default should be conservative:

- reboot after fsck
- wait for device to return
- do not expose `--no-reboot` and `--no-wait` unless advanced options are shown

### File Metadata Repair

This maps to `repair-xattrs`.

This is a local macOS-side workflow for mounted SMB shares. It should use a path
picker instead of asking users to type paths.

Flow:

1. Choose mounted SMB share or folder.
2. Scan.
3. Show findings.
4. Repair known-safe issues.
5. Show summary.

Defaults:

- recursive scan on
- skip hidden paths
- skip Time Machine bundles
- do not fix permissions unless advanced
- do not include Time Machine unless advanced and heavily warned

If the host is not macOS, disable the feature with a simple explanation.

If no mounted matching share is found, show:

- "Open in Finder"
- "Choose Folder"
- "Connect to SMB URL"

### Uninstall

Uninstall should be a destructive advanced action, but still polished.

Flow:

1. Build uninstall plan.
2. Show what will be removed.
3. Confirm.
4. Remove managed files.
5. Reboot or leave running state as explicitly chosen.
6. Verify removal when possible.

Plan should show:

- flash hooks to remove
- payload directories to remove
- whether reboot is required
- whether post-reboot verification will run

Default should be reboot and verify. `No reboot` should be advanced.

## Flash UX

Flash should be planned now, but disabled before release unless it has gone
through separate acceptance testing.

Product label:

"Persistent NetBSD4 Boot Hook"

Do not call the main entry point "flash" in the normal UI. The word can appear
inside advanced details.

Release gating:

- hidden by default
- visible only in an Advanced or Experimental section
- write actions disabled in release builds until explicitly enabled
- read-only backup/analyze may be available earlier, but only for NetBSD4

Eligibility checks:

- saved device exists
- device is NetBSD4
- SSH is reachable and authenticated
- app can read both firmware banks
- app can read ACP checksum properties
- app can identify the active bank or explain ambiguity
- app can classify the live `LOGIN` hook

Flash landing screen should say:

"This experimental workflow can back up and inspect the two firmware banks on a
NetBSD4 Time Capsule. Write modes can modify firmware. A failed or interrupted
write can make the device difficult or impossible to recover without hardware
tools."

Modes:

- Back Up and Inspect
- Check Against Apple Firmware
- Download Apple Firmware Only
- Patch Boot Hook, disabled by default
- Restore Apple Firmware, disabled by default

Read-only analysis result should show:

- backup directory
- primary bank validity
- secondary bank validity
- active bank
- how active bank was selected
- LOGIN classification: stock, patched, unknown
- patch feasibility
- restore feasibility
- Apple firmware match if checked

Patch plan screen:

- target bank: primary
- inactive bank remains untouched
- backup validity for both banks
- target payload checksum
- warnings
- manual power-cycle requirement

Restore plan screen:

- target bank: active bank only
- Apple firmware source/version
- payload checksum
- optional reboot after restore
- post-restore check required

Write confirmation should be stronger than normal:

- require explicit checkbox: "I have saved the firmware backup."
- require explicit checkbox: "I understand only the selected bank will be written."
- require typed confirmation such as the device nickname
- show power warning

After patch write:

- do not offer software reboot
- show "Unplug the Time Capsule, wait 10 seconds, plug it back in."
- show a timer and then "Run Checkup"
- remind user that one bank was left untouched

After restore write:

- allow optional reboot
- suggest "Check Apple Firmware"
- then suggest normal deploy if the user wants TimeCapsuleSMB again

## Settings

App-level settings:

- default Bonjour timeout
- default mount wait
- diagnostics sharing/telemetry preference
- show advanced options
- check for app updates
- Time Machine warning policy version

Device-level settings:

- nickname
- host/IP
- stored password status
- NBNS enabled
- debug logging for future deploys
- advanced SSH options, hidden
- forget device

## Background Jobs

The app should run these without presenting them as commands:

- app bundle validation
- payload manifest validation
- version support check
- host macOS warning check
- periodic Bonjour discovery
- lightweight selected-device reachability refresh
- Keychain availability check

If background jobs fail:

- app damaged: blocking alert
- update required: blocking or strong warning based on version metadata
- missing optional verification tool: degraded checkup warning, not install blocker
- Bonjour unavailable: non-blocking warning with manual address option

## User-Facing Copy Principles

Use familiar words first:

- "Install SMB" instead of "deploy"
- "Checkup" instead of "doctor"
- "Start SMB" instead of "activate" except in advanced text
- "Disk Repair" instead of `fsck`
- "File Metadata Repair" instead of `repair-xattrs`
- "Persistent NetBSD4 Boot Hook" instead of `flash`

Use technical names in secondary labels or details so expert users can map GUI
actions back to CLI commands.

Do not expose implementation path names unless the user opens details.

## Suggested Screen Map

```text
All Time Capsules
  Device Detail
    Overview
    Install / Update
    Checkup
    Maintenance
      NetBSD4 Activation
      Disk Repair
      File Metadata Repair
      Uninstall
      Firmware Boot Hook (experimental)
    Advanced
      logs
      raw operation events
      copy diagnostics

Add Time Capsule
  Discover
  Manual Address
  Authenticate
  Enable SSH
  Identify
  Save

Activity
  current operation
  historical operations
  copied diagnostics

Settings
  app defaults
  warning policy
  updates
```

## Important UX States

Global app states:

- app ready
- app bundle damaged
- update required
- host macOS has Time Machine warning
- no saved devices
- discovery running
- discovery unavailable

Device states:

- discovered unsaved
- saved, unchecked
- password needed
- SSH disabled
- enabling SSH
- rebooting after SSH enable
- unsupported device
- ready to install
- install planned
- installing
- rebooting after install
- verifying after install
- healthy
- warning
- failed
- NetBSD4 activation needed
- removed
- offline

Operation states:

- idle
- preparing
- planning
- ready for review
- awaiting confirmation
- running
- waiting for reboot
- verifying
- succeeded
- warning
- failed
- cancelled

Flash-specific states:

- unavailable
- disabled in this build
- eligible for read-only analysis
- reading banks
- saving backup
- analyzing banks
- plan available
- write locked
- awaiting strong confirmation
- writing
- readback validating
- write validated
- manual power cycle required
- restore rebooting
- check Apple firmware needed
- failed

## Release Recommendation

For the first polished GUI release:

- include multi-device save/select
- include add-device, install/update, checkup, NetBSD4 activation, disk repair,
  xattr repair, and uninstall
- run app readiness in the background
- show macOS Time Machine warning proactively
- include flash read-only planning only if stable enough
- keep flash write actions disabled

The first release should make the normal Time Capsule owner successful without
teaching them the command set. The advanced tools should be available, but they
should feel like guarded recovery workflows rather than ordinary setup steps.
