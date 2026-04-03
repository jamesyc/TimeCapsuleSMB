# TimeCapsuleSMB

Give an old Apple AirPort Time Capsule a modern SMB server again.

This project replaces the Time Capsule's outdated file sharing with a working Samba 4 setup that:

- boots automatically after reboot
- shows up over Bonjour on your Mac
- works with modern macOS SMB
- uses the same password you already enter for the Time Capsule during setup

The default result is:

- SMB service name: `Time Capsule Samba 4`
- SMB username: `admin`
- share name: `Data`
- Finder address: `smb://timecapsulesamba4.local/Data`

For the deeper engineering details, see [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md).

## Why This Exists

Old Time Capsules used AFP and SMB1-era behavior. That is a problem now because:

- AFP is effectively dead on modern Apple systems
- SMB1 is old, fragile, and often disabled or unsupported
- Apple's original Time Capsule sharing experience no longer feels reliable on a current Mac

This repo gives the Time Capsule a newer SMB server so it can behave more like a normal NAS on a home network.

## What You Get

When this is working, your Time Capsule:

- runs Samba 4.3.13
- advertises itself over Bonjour as an SMB server
- accepts authenticated SMB logins
- serves the internal Time Capsule disk over SMB2/SMB3

Current auth model:

- username: `admin`
- password: the same password you provide during setup in `.env`

Guest access is disabled.

## Before You Start

You need:

- a Mac on the same local network as the Time Capsule
- the Time Capsule's password
- Python 3 on your Mac

You do not need to build Samba yourself. The working binaries are already checked into this repo under [bin/](/Users/jameschang/git/TimeCapsuleSMB/bin).

## The Simple Path

If you are a normal user and just want this working, these are the commands you care about.

### 1. Prepare your Mac

From the repo root:

```bash
python3 scripts/bootstrap_host.py
```

This creates the local Python environments and installs the tools this repo needs.

### 2. Find the Time Capsule and enable SSH

```bash
.venv/bin/python scripts/prep_device.py
```

This script:

- finds Time Capsules on your network
- lets you pick the right one
- enables SSH on it if needed

### 3. Create your local config

```bash
.venv/bin/python scripts/configure.py
```

This writes a local `.env` file in the repo.

For most people, pressing Enter for the defaults is fine.

Important defaults:

- share name: `Data`
- Samba username: `admin`
- Bonjour service name: `Time Capsule Samba 4`
- Bonjour hostname: `timecapsulesamba4`

### 4. Deploy it

```bash
.venv/bin/python scripts/deploy.py
```

By default, `deploy.py`:

- copies the checked-in Samba files to the Time Capsule
- installs the boot scripts
- configures the SMB password
- reboots the Time Capsule
- waits for it to come back
- runs a basic post-deploy check

If you want to skip the reboot prompt:

```bash
.venv/bin/python scripts/deploy.py --yes
```

### 5. Check that it worked

Run:

```bash
.venv/bin/python scripts/doctor.py
```

This checks:

- your local `.env`
- required local tools
- the checked-in binaries
- SSH reachability
- SMB reachability
- Bonjour `_smb._tcp`
- authenticated SMB listing

## Connecting From Finder

Once deployment finishes and the Time Capsule has rebooted, you can connect from Finder with:

```text
smb://timecapsulesamba4.local/Data
```

If that hostname is different on your setup, `deploy.py` and `doctor.py` will tell you what it is.

Login with:

- username: `admin`
- password: your Time Capsule password

## What The Scripts Actually Do

You do not need this section to use the repo, but it helps explain why the project looks unusual.

The Time Capsule has very little persistent writable space:

- `/mnt/Flash` is tiny
- `/mnt/Memory` is a small RAM disk
- the internal HDD is large, but Apple may mount and unmount it on its own schedule

So the working design is:

1. Keep the real payload on the internal disk.
2. Keep only tiny boot hooks on flash.
3. At boot, copy the runtime into RAM.
4. Start Samba from RAM.
5. Advertise SMB with a separate tiny mDNS helper.

That is why this repo uses:

- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)
- [boot/samba4/rc.local](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/rc.local)
- [boot/samba4/start-samba.sh](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/start-samba.sh)

## Files You Probably Care About

- [scripts/bootstrap_host.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/bootstrap_host.py)
  Sets up the local Mac-side environment.
- [scripts/prep_device.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/prep_device.py)
  Finds the Time Capsule and enables SSH.
- [scripts/configure.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/configure.py)
  Writes `.env`.
- [scripts/deploy.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/deploy.py)
  Deploys the working system.
- [scripts/doctor.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/doctor.py)
  Verifies that the setup is healthy.

## Troubleshooting

### I do not see the Time Capsule in Finder

Try:

```bash
.venv/bin/python scripts/doctor.py
```

and also:

```bash
dns-sd -B _smb._tcp local.
```

If Bonjour is working, you should see:

- `Time Capsule Samba 4`

### Finder still does not connect

Try connecting directly with:

```text
smb://timecapsulesamba4.local/Data
```

or use the IP address from your `.env`.

### Deploy says SMB listing failed right after reboot

That can happen if the Time Capsule is still coming up. Wait a little and run:

```bash
.venv/bin/python scripts/doctor.py
```

### I want more detail

Read:

- [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md)
- [plan/session-handoff-2026-04-03-2.md](/Users/jameschang/git/TimeCapsuleSMB/plan/session-handoff-2026-04-03-2.md)

## Security Notes

- Keep this LAN-only.
- Do not expose this SMB service to the public internet.
- The current auth model maps SMB access to `root` on the Time Capsule internally.
- This is a practical compatibility choice for old Apple firmware, not a modern hardened NAS design.

## For Developers

Most users do not need this section.

The checked-in binaries are already built. If you want to rebuild them yourself, the maintainer build flow lives under [build/](/Users/jameschang/git/TimeCapsuleSMB/build) and uses a NetBSD VM.

Main build outputs:

- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

The long-form history and design notes are in:

- [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md)
- [plan/](/Users/jameschang/git/TimeCapsuleSMB/plan)
