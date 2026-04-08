# TimeCapsuleSMB

Apple AirPort Time Capsules are still perfectly usable pieces of hardware, but they only support AFP and SMB1. Apple has removed SMB1 support from MacOS a long time ago, and AFP support is being removed for MacOS 27.

**NOTE THAT TIME MACHINE ON MACOS 26.4 IS CURRENTLY BROKEN**, see https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups

This repo configures a modern Samba setup that runs directly on the Time Capsule itself. The goal is that a Time Capsule can once again show up as a normal SMB server on your network, and your Mac can connect to it as a network share. This project is currently confirmed to work for NetBSD 6 based Time Capsules! Your Time Capsule should work if it looks like this:  
<img width="256" height="192" alt="image" src="https://github.com/user-attachments/assets/5d0b044f-2137-4bb7-8d65-3d1bb251754c" />

If you want the long-form engineering background, design decisions, and implementation details, read [DETAIL.md](/DETAIL.md).

## Expectations

If the setup completes successfully, your Time Capsule will boot its own Samba 4 server automatically, advertise itself over Bonjour (show up automatically in the "Network" folder on MacOS), and accept authenticated SMB connections from macOS. You should then be able to open Finder, choose Connect to Server, and use a normal SMB URL instead of relying on Apple’s legacy stack. **This will disable Apple's AFP and SMB file server**, so do not expect those to be running at the same time. 

THIS CURRENTLY DOES NOT SUPPORT older NetBSD 4 based Time Capsules. This only supports Time Capsules running NetBSD 6. Support for older Time Capsules is in progress, for more information [see this issue](https://github.com/jamesyc/TimeCapsuleSMB/issues/15). 

**It is expected to get "Internal disk needs repair" because this adds files to the internal disk**; see [this issue for more information](https://github.com/jamesyc/TimeCapsuleSMB/issues/13). The `deploy` script will drop 4 files in `/mnt/Flash` on the Time Capsule, plus a `samba4` folder on the root of the hard drive. The `uninstall` script will delete these files and reboot the Time Capsule. 

The current authentication model uses `admin` as the username, and the Samba password is the same password you enter during setup when the tool asks for the Time Capsule password. Guest access is disabled. 

## Requirements

You do not need to build Samba yourself. The working binaries are already checked into this repository under [bin/](bin), and the normal user workflow uses those checked-in files directly. To rebuild `smbd` by yourself, run the scripts in `build/` on a NetBSD machine.

Also, if you are an expert, you can copy the binary at [/bin/samba4/smbd](/bin/samba4/smbd) onto the Time Capsule and set it up yourself. 

For the typical setup path, you need only:

- a Mac on the same local network as the Time Capsule
- the Time Capsule password
- Python 3 and Homebrew installed on your Mac.

## Quick Start

From the root of this repository, the normal flow is:

1. `./tcapsule bootstrap`
2. `.venv/bin/tcapsule configure`
3. `.venv/bin/tcapsule prep-device`
4. `.venv/bin/tcapsule deploy`
5. `.venv/bin/tcapsule doctor`
6. `.venv/bin/tcapsule uninstall` if you want to remove TimeCapsuleSMB later

If you prefer, you can activate the virtual environment after step 1 and then run `tcapsule ...` directly:

```bash
source .venv/bin/activate
tcapsule configure
tcapsule prep-device
tcapsule deploy
tcapsule doctor
tcapsule uninstall
```

## Step 1: Prepare Your Mac

Run:

```bash
./tcapsule bootstrap
```

This command prepares the local Python environment in this folder. It creates the `.venv` folder, installs in there the Python dependencies needed for discovery, deployment, and verification, installs the local `tcapsule` command into that virtualenv, and optionally provisions AirPyrt support.

If this is your first time using the repo, this is the only command you should run with the repo-local launcher. After this step, use `.venv/bin/tcapsule ...` or activate `.venv`.

## Step 2: Create The Local Config

Run:

```bash
.venv/bin/tcapsule configure
```

This writes a `.env` file in the repo folder, and the other `tcapsule` commands use that file as their local device configuration.

At the start of `configure`, the tool first tries to discover your Time Capsule on the local network via mDNS/Bonjour. If it finds one, it prefills the SSH target for you. If it does not find one, it falls back to the normal manual prompt flow.

For typical users, most of the defaults are good enough. If the script offers a value and you do not have a reason to change it, **just pressing Enter is usually the correct choice**.

The most important defaults are:

- SMB share name: `Data`
- Samba username: `admin`
- Bonjour service name: `Time Capsule Samba 4`
- Bonjour hostname label: `timecapsulesamba4`

The password you enter here is important. It becomes the password used for the SMB login as well. In other words, after setup, you normally connect with:

- username: `admin`
- password: the same Time Capsule password you entered during configuration

Samba does not magically use Apple’s internal password backend; unfortunately, using Apple's password system is not possible. We deliberately reuse the same password value so that the user experience is simpler and less confusing.

## Step 3: Find The Time Capsule And Enable SSH

Run:

```bash
.venv/bin/tcapsule prep-device
```

This step uses the `.env` configuration you just wrote. In particular, it uses the configured `TC_HOST` and `TC_PASSWORD` values and then enables or disables SSH through AirPyrt as needed.

In practical terms, this script will:

- use the Time Capsule target from `.env`
- check whether SSH is already reachable
- enable SSH if SSH is not already available

## Step 4: Deploy It

Run:

```bash
.venv/bin/tcapsule deploy
```

This is the installation step. It validates the checked-in binaries, copies the payload and boot files to the Time Capsule, sets up the Samba password files, installs the boot hook, and reboots the device so the new runtime comes up cleanly.

By default, `tcapsule deploy` reboots the Time Capsule after deployment and then waits for it to come back. If you want to skip the reboot confirmation prompt, you can run:

```bash
.venv/bin/tcapsule deploy --yes
```

There are also other flags such as `--no-reboot` and `--dry-run`, but leave those alone unless you have a specific reason to use them.

If you want a machine-readable deployment plan without changing the device, use:

```bash
.venv/bin/tcapsule deploy --dry-run --json
```

## Step 5: Verify The Result

Run:

```bash
.venv/bin/tcapsule doctor
```

This is a non-destructive diagnostic command. `tcapsule doctor` checks:

- that your `.env` is complete
- that the required local tools exist
- that the checked-in binaries are present and match the expected checksums
- that SSH is reachable
- that SMB is reachable
- that Bonjour `_smb._tcp` advertisement is visible
- that an authenticated SMB listing actually works

If you want the results in JSON instead of human-readable text, use:

```bash
.venv/bin/tcapsule doctor --json
```

## Step 6: Remove It Later If Needed

Run:

```bash
.venv/bin/tcapsule uninstall
```

This removes the managed TimeCapsuleSMB payload from the internal disk, removes the boot hook files from `/mnt/Flash`, and reboots the Time Capsule so the custom Samba runtime does not come back on the next boot.

If you want to skip the reboot confirmation prompt, use:

```bash
.venv/bin/tcapsule uninstall --yes
```

If you want to preview the uninstall plan without changing the device, use:

```bash
.venv/bin/tcapsule uninstall --dry-run
```

For machine-readable dry-run output:

```bash
.venv/bin/tcapsule uninstall --dry-run --json
```

Uninstall success means the managed payload and boot files are gone after reboot. It does **not** require Apple SMB or AFP to be enabled afterward. Those services may be on or off depending on the device's own settings.

## Connecting From Finder

Once deployment has completed and the Time Capsule has rebooted, you should be able to connect from Finder with:

```text
smb://timecapsulesamba4.local/Data
```

If your Bonjour hostname is different on your system, `tcapsule deploy` and `tcapsule doctor` will tell you what name was actually advertised.

When Finder prompts for credentials, use:

- username: `admin`
- password: your Time Capsule password

This is the intended normal user path. You should not have to care about the internal mountpoints, boot scripts, or NetBSD build details just to open the share.

## Technical Notes

Everything above is the beginner path. The rest of this README is more technical and explains why the repo is structured this way.

## Design

The Time Capsule hardware is old and constrained. It has three relevant storage areas:

- `/mnt/Flash`, which is persistent but only has ~900KB of free space.
- `/mnt/Memory`, which is a 16MB ramdisk
- the internal HDD mounted under `/Volumes/dk2` or `/Volumes/dk3`, which is large but managed by Apple and unmounts when idle. You cannot run a binary off this location for that reason.

Unfortunately, it was not an option to "copy one binary somewhere and call it a day" to get `smbd` running. Thus, the current process is:

1. Keep the full `smbd` payload on the internal disk.
2. Keep only a very small `rc.local` boot hook on flash.
3. At boot, wait for the internal disk to appear and mount.
4. Copy the runtime binaries into `/mnt/Memory`.
5. Start Samba from the ramdisk, not from the disk Apple may later decide to unmount.
6. Advertise `_smb._tcp` with a separate tiny mDNS helper.

That is the reason the repository contains both:

- [bin/samba4/smbd](bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](bin/mdns/mdns-smbd-advertiser)

and boot files such as:

- [src/timecapsulesmb/assets/boot/samba4/rc.local](src/timecapsulesmb/assets/boot/samba4/rc.local)
- [src/timecapsulesmb/assets/boot/samba4/start-samba.sh](src/timecapsulesmb/assets/boot/samba4/start-samba.sh)

There are other constraints the Time Capsule places on us:  
- The NetBSD 6 source code does not support earmv4 builds, so we need to build from NetBSD 7.
- Samba 3.x compiles easily, but it doesn't support directory traversal on NetBSD 6. This is a known bug apparently.
- Samba 4.0.x has the same issue
- Samba 4.2.x and 4.3.x are much harder to compile, and do not support vfs_fruit
- Samba 4.8.x is the first version that seems to work, although getting it to compile can be very difficult.

## Troubleshooting

The current default result is:

- SMB service name: `Time Capsule Samba 4`
- SMB username: `admin`
- share name: `Data`
- Finder address: `smb://timecapsulesamba4.local/Data`

### The Time Capsule Does Not Show Up In Finder

Run:

```bash
.venv/bin/tcapsule doctor
```

Then check Bonjour directly:

```bash
dns-sd -B _smb._tcp local.
```

To inspect discovery results from the tool itself in JSON, run:

```bash
.venv/bin/tcapsule discover --json
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

That can happen if the device has come back on the network but is still finishing startup. These old Time Capsule CPUs are not fast.

Wait a little, then run:

```bash
.venv/bin/tcapsule doctor
```

### I Want The Full Technical Story

Read:

- [DETAIL.md](DETAIL.md)

Those documents explain the engineering constraints, historical dead ends, and current implementation in much more detail.

## Security Notes

This should be treated as a LAN-only setup.

Do not expose this SMB service directly to the public internet. Do not forward ports to it. Do not pretend that an old Time Capsule turned into a modern hardened NAS just because the SMB side now works better. I have tested this with a M1 Macbook Pro and an A1470 Time Capsule. Your mileage may vary. Older models of Time Capsules may have a smaller `/mnt/Memory` that the `smbd` binary does not fit in; I am unable to confirm.

Also note that the current auth model maps SMB access to `root` internally on the Time Capsule. That is a deliberate compatibility choice for this old firmware, as the version of NetBSD 6 running on the Time Capsule errors when Samba tries to switch users.

## For Developers And Maintainers

Most users should stop reading here.

The checked-in binaries are already built. If you want to rebuild them yourself, the maintainer build flow lives under [build/](build) and depends on a NetBSD VM.

The main build outputs are:

- [bin/samba4/smbd](bin/samba4/smbd)
- [bin/mdns/mdns-smbd-advertiser](bin/mdns/mdns-smbd-advertiser)

If you want the actual engineering details, the right place to start is:

- [DETAIL.md](DETAIL.md)
- [plan/](plan)
