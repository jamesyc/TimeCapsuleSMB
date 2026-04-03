# TimeCapsuleSMB

This project exists for one simple reason: old Apple AirPort Time Capsules are still perfectly usable pieces of hardware, but Apple left them behind in exactly the part that matters most, which is file sharing that still works cleanly with a modern Mac.

The original Time Capsule experience depended heavily on AFP and older SMB behavior. That was reasonable when Apple still cared about AFP and when SMB1 was still tolerated everywhere. That is not the world we live in anymore. AFP is effectively dead for normal users, SMB1 is old and increasingly unwelcome, and the result is that a Time Capsule which used to feel integrated and obvious now feels brittle, outdated, or simply broken.

What this repo does is replace that old sharing path with a modern Samba setup that runs directly on the Time Capsule itself. The result is that your Time Capsule can once again show up as a normal SMB server on your network, and your Mac can connect to it in the way you would expect from any other NAS.

The current default result is:

- SMB service name: `Time Capsule Samba 4`
- SMB username: `admin`
- share name: `Data`
- Finder address: `smb://timecapsulesamba4.local/Data`

If you want the long-form engineering background, design decisions, and implementation details, read [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md). This README is intentionally aimed at the person who mostly wants the thing to work.

## What You Should Expect

If the setup completes successfully, your Time Capsule will boot its own Samba 4 server automatically, advertise itself over Bonjour, and accept authenticated SMB connections from macOS. You should then be able to open Finder, choose Connect to Server, and use a normal SMB URL instead of relying on Apple’s legacy file-sharing stack.

The current authentication model is straightforward. You log in as `admin`, and the password is the same password you enter during setup when the scripts ask for the Time Capsule password. Guest access is disabled. That means the box behaves much more like a normal SMB appliance and much less like the vague old “maybe Finder will discover it, maybe it will not” experience that many people are used to from aging Apple hardware.

## What You Need

You do not need to rebuild Samba yourself. The working binaries are already checked into this repository under [bin/](/Users/jameschang/git/TimeCapsuleSMB/bin), and the normal user workflow uses those checked-in files directly.

For the typical setup path, you need only:

- a Mac on the same local network as the Time Capsule
- the Time Capsule password
- Python 3 on your Mac

That is it. The build system exists in this repository because it was necessary to get the binaries in the first place, but most users should ignore that part entirely.

## The Short Version

From the root of this repository, the normal flow is:

1. `python3 scripts/bootstrap_host.py`
2. `.venv/bin/python scripts/prep_device.py`
3. `.venv/bin/python scripts/configure.py`
4. `.venv/bin/python scripts/deploy.py`
5. `.venv/bin/python scripts/doctor.py`

If you already know what you are doing, that may be enough. If not, the sections below explain each step in plain English.

## Step 1: Prepare Your Mac

Run:

```bash
python3 scripts/bootstrap_host.py
```

This script prepares the local Python environment on your Mac. It creates the virtual environments that the rest of the workflow expects and installs the Python packages needed for device discovery, AirPyrt integration, deployment, and verification. The point of this step is simply to make the rest of the workflow predictable, so that you are not trying to guess which global Python or random package version your machine happens to have.

If this step fails, stop there and fix that first. There is no point trying to debug Time Capsule behavior when the local host environment itself is incomplete.

## Step 2: Find The Time Capsule And Enable SSH

Run:

```bash
.venv/bin/python scripts/prep_device.py
```

This is the step that finds the Time Capsule on your local network and, if necessary, enables SSH access on it. The script is intentionally focused on preparation, not on the full deployment. It exists to solve the annoying early-stage problem that most users have, which is simply identifying the right device and getting it into a state where it can be managed.

In practical terms, this script will:

- discover Time Capsules on your network
- let you pick the correct one
- enable SSH if SSH is not already available

This is why the script is called `prep_device.py` and not something grander. It is not trying to pretend it does everything. It just gets the device ready for the real install step.

## Step 3: Create The Local Config

Run:

```bash
.venv/bin/python scripts/configure.py
```

This writes a local `.env` file in the repository. That file is not meant to be committed. It is your local machine’s configuration for the target Time Capsule.

For most users, the defaults are good enough. If the script offers a value and you do not have a reason to change it, pressing Enter is usually the correct choice.

The most important defaults are:

- SMB share name: `Data`
- Samba username: `admin`
- Bonjour service name: `Time Capsule Samba 4`
- Bonjour hostname label: `timecapsulesamba4`

The password you enter here is important. It becomes the password used for the SMB login as well. In other words, after setup, you normally connect with:

- username: `admin`
- password: the same Time Capsule password you entered during configuration

That is not because Samba is magically using Apple’s internal password backend. It is because this project deliberately reuses the same password value so that the user-facing experience is simpler and less confusing.

## Step 4: Deploy It

Run:

```bash
.venv/bin/python scripts/deploy.py
```

This is the actual installation step. It copies the checked-in binaries and boot files to the Time Capsule, sets up the Samba password files, installs the boot hook, and reboots the device so the new runtime comes up cleanly.

By default, `deploy.py` does the sensible thing, which is to reboot the Time Capsule after deployment and then wait for it to come back. That is not an accident. This project deliberately favors the boring, repeatable path over the clever one-off path, because boot-time behavior is what really matters here. If it only works until the next reboot, it does not really work.

If you want to skip the reboot confirmation prompt, you can run:

```bash
.venv/bin/python scripts/deploy.py --yes
```

There are also flags such as `--no-reboot` and `--dry-run`, but beginners should generally leave those alone unless they have a specific reason to use them.

## Step 5: Verify The Result

Run:

```bash
.venv/bin/python scripts/doctor.py
```

This is the non-destructive diagnostic script. It exists because the difference between “the deploy finished” and “the system is actually healthy” matters. A lot of old-device hacking projects stop at a half-working state and call that good enough. This repo does not try to do that.

`doctor.py` checks:

- that your `.env` is complete
- that the required local tools exist
- that the checked-in binaries are present
- that SSH is reachable
- that SMB is reachable
- that Bonjour `_smb._tcp` advertisement is visible
- that an authenticated SMB listing actually works

If `doctor.py` passes, you are in much better shape than simply assuming Finder will sort itself out.

## Connecting From Finder

Once deployment has completed and the Time Capsule has rebooted, you should be able to connect from Finder with:

```text
smb://timecapsulesamba4.local/Data
```

If your Bonjour hostname is different on your system, `deploy.py` and `doctor.py` will tell you what name was actually advertised.

When Finder prompts for credentials, use:

- username: `admin`
- password: your Time Capsule password

This is the intended normal user path. You should not have to care about the internal mountpoints, boot scripts, or NetBSD build details just to open the share.

## Why The Design Looks Strange

The Time Capsule hardware is old, constrained, and opinionated. That matters.

It has three relevant storage areas:

- `/mnt/Flash`, which is persistent but tiny
- `/mnt/Memory`, which is small and temporary
- the internal HDD under `/Volumes/dk2` or `/Volumes/dk3`, which is large but managed by Apple in ways that are not always friendly

So the final working design is not “copy one binary somewhere and call it a day.” That would have been nice, but the hardware does not really allow it.

The design that actually works is:

1. Keep the real payload on the internal disk.
2. Keep only very small boot hooks on flash.
3. At boot, wait for the internal disk to appear and mount.
4. Copy the runtime binaries into `/mnt/Memory`.
5. Start Samba from RAM, not from the disk Apple may later decide to unmount.
6. Advertise `_smb._tcp` with a separate tiny mDNS helper.

That is the reason the repository contains both:

- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

and boot files such as:

- [boot/samba4/rc.local](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/rc.local)
- [boot/samba4/start-samba.sh](/Users/jameschang/git/TimeCapsuleSMB/boot/samba4/start-samba.sh)

It is not pretty in the abstract, but it is grounded in what the hardware actually allows.

## The Files You Are Most Likely To Use

If you are just using the project rather than maintaining it, these are the files that matter most:

- [scripts/bootstrap_host.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/bootstrap_host.py)  
  Prepares the local Python environment on your Mac.

- [scripts/prep_device.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/prep_device.py)  
  Finds the Time Capsule and enables SSH if needed.

- [scripts/configure.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/configure.py)  
  Writes your local `.env` file.

- [scripts/deploy.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/deploy.py)  
  Installs the working runtime onto the Time Capsule.

- [scripts/doctor.py](/Users/jameschang/git/TimeCapsuleSMB/scripts/doctor.py)  
  Checks whether the result is actually healthy.

## Troubleshooting

### The Time Capsule Does Not Show Up In Finder

First, do not guess. Run:

```bash
.venv/bin/python scripts/doctor.py
```

Then check Bonjour directly:

```bash
dns-sd -B _smb._tcp local.
```

If the system is working normally, you should see:

- `Time Capsule Samba 4`

Finder is not always the best first diagnostic tool. The service can be up and correct even when Finder browsing is being slow or temperamental.

### Finder Still Does Not Connect

Try the direct address explicitly:

```text
smb://timecapsulesamba4.local/Data
```

If necessary, use the IP address from your `.env` instead. The point is to separate “Bonjour browsing did something odd” from “SMB itself is broken.”

### Deploy Says SMB Listing Failed Right After Reboot

That can happen if the device has come back on the network but is still finishing startup. These old boxes are not fast.

Wait a little, then run:

```bash
.venv/bin/python scripts/doctor.py
```

Again, the idea is to check the real state rather than assume the first timing-sensitive check tells the whole story.

### I Want The Full Technical Story

Read:

- [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md)
- [plan/session-handoff-2026-04-03-2.md](/Users/jameschang/git/TimeCapsuleSMB/plan/session-handoff-2026-04-03-2.md)

Those documents explain the engineering constraints, historical dead ends, and current implementation in much more detail than a beginner should have to absorb up front.

## Security Notes

This should be treated as a LAN-only setup.

Do not expose this SMB service directly to the public internet. Do not forward ports to it. Do not pretend that an old Time Capsule turned into a modern hardened NAS just because the SMB side now works better.

Also note that the current auth model maps SMB access to `root` internally on the Time Capsule. That is a deliberate compatibility choice for this old firmware. It is practical, and it works, but it is not the same thing as a carefully sandboxed modern server design.

## For Developers And Maintainers

Most users should stop reading here.

The checked-in binaries are already built. If you want to rebuild them yourself, the maintainer build flow lives under [build/](/Users/jameschang/git/TimeCapsuleSMB/build) and depends on a NetBSD VM.

The main build outputs are:

- [bin/samba4/smbd](/Users/jameschang/git/TimeCapsuleSMB/bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](/Users/jameschang/git/TimeCapsuleSMB/bin/mdns/mdns-smbd-advertiser)

If you want the actual engineering details, the right place to start is not the old hand-wavy README history but:

- [DETAIL.md](/Users/jameschang/git/TimeCapsuleSMB/DETAIL.md)
- [plan/](/Users/jameschang/git/TimeCapsuleSMB/plan)
