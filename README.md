# TimeCapsuleSMB

Apple AirPort Time Capsules are still perfectly usable pieces of hardware, but they only support AFP and SMB1. Apple has removed SMB1 support from macOS a long time ago, and AFP support is being removed for macOS 27. This repo configures a modern Samba setup that runs directly on the Time Capsule itself; modern macOS can connect to the Time Capsule as a network share, and use it for Time Machine backups. 

**NOTE THAT TIME MACHINE ON MACOS 26.4.x (AND 15.7.5) IS CURRENTLY BROKEN**, see https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups  
Macs running macOS 26.4.x can still use the device as a standard Samba network share in Finder.

This project is fully working for Gen 5 (NetBSD 6 based) Time Capsules, and Gen 1-4 (NetBSD 4) support now exists as well with some extra caveats described below.

## Expectations

If the setup completes successfully, your Time Capsule will run its own Samba 4 server, advertise itself over Bonjour (show up automatically in the "Network" folder on macOS), and accept authenticated SMB3 connections from macOS. You should then be able to open Finder, choose Connect to Server, and use a normal SMB URL instead of relying on Apple’s legacy stack. You should also be able to use the disk for Time Machine backups:  
<img width="478" height="268" alt="image" src="https://github.com/user-attachments/assets/c713a1c6-ff71-43a2-a057-451223a1c0e0" />

The `deploy` script will install files in `/mnt/Flash` on the Time Capsule, plus a `.samba4` folder on the root of the hard drive by default. The `uninstall` script removes those managed files and can optionally reboot the device afterward.

NetBSD 6 devices automatically startup on boot. **Older NetBSD 4 devices need a manual `activate` after every reboot.** If you do not run the `activate` command after a reboot, then Samba will not start automatically on an older Time Capsule.

If you find any problems, I would appreciate it if you [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues) for help; I am actively working on it, so expect improvements! However **this is not supported by a trillion dollar company**, this is built by a guy in his free time. Therefore, I honestly do *not* recommend using this if you are still using the Time Capsule as your primary router, or if you have data on it that you are not comfortable losing. I do *not* expect this to permanently break the Time Capsule if something goes wrong, but it *may* mess up your configuration/data so you would need to reset/wipe the device. 

The current authentication model accepts any user as the username, and the Samba password is the same password you enter during setup when the tool asks for the Time Capsule password. Guest access is disabled. 

AirPort Extreme devices are not officially supported (because I do not own one to test on). Unoffically, they work with some minor tweaks. 

## Requirements

The working binaries are saved in this repository under [bin/](bin), and the normal user workflow uses those checked-in files directly. You do not need to build Samba yourself, but if you want to rebuild `smbd` by yourself, run the scripts in `build/` on a NetBSD machine. 

Also, if you are an expert and want to DIY the install, you can copy the binary at [/bin/samba4/smbd](/bin/samba4/smbd) for NetBSD 6 devices, [/bin/samba4-netbsd4le/smbd](/bin/samba4-netbsd4le/smbd) for NetBSD 4 little-endian devices, or [/bin/samba4-netbsd4be/smbd](/bin/samba4-netbsd4be/smbd) for NetBSD 4 big-endian devices onto the Time Capsule and set it up yourself. The binaries are statically compiled, so you don't need anything else. 

For the typical setup path, you need only:

- a Mac or Linux machine on the same local network as the Time Capsule
- the Time Capsule password
- Python 3.9+
- `smbclient` installed locally for `doctor`

During first-time setup, `configure` can enable SSH on the Time Capsule when needed using the built-in Python 3 ACP client.

## Quick Start

Download (or run `git clone`) this repository to a folder on your Mac or Linux machine.

From the root of this repository, the normal quick start commands to run is:

1. `./tcapsule bootstrap`
2. `.venv/bin/tcapsule configure` save a config/settings file
3. `.venv/bin/tcapsule deploy` deploy to the Time Capsule according to the config file
4. `.venv/bin/tcapsule doctor` check if everything is working
5. `.venv/bin/tcapsule activate` after reboot on NetBSD 4 devices if Samba did not auto-start

If you run into any issues:
- `.venv/bin/tcapsule fsck` if the internal disk needs repair before deploy
- `.venv/bin/tcapsule discover` to list all mDNS/Bonjour devices
- `.venv/bin/tcapsule repair-xattrs` to repair any broken files on the disk from bad xattrs
- `.venv/bin/tcapsule uninstall` if you want to remove TimeCapsuleSMB later

Just delete this `TimeCapsuleSMB` folder if you want to remove it from your Mac after you're done setting up the Time Capsule. All the scripts/binaries/etc are stored in the `TimeCapsuleSMB` folder, so if you want to clean up your Mac then just deleting the folder is fine.

If you find a bug, report it here: https://github.com/jamesyc/TimeCapsuleSMB/issues  

## Step 1: Prepare Your Host

Run:

```bash
./tcapsule bootstrap
```

This command prepares the local Python environment in this folder. It creates the `.venv` folder, installs in there the Python dependencies needed for discovery, deployment, and verification, and installs the local `tcapsule` command into that virtualenv.

On macOS, `bootstrap` can also offer to install `smbclient` via Homebrew. On Linux, `bootstrap` will guide you to install `smbclient` with your distro package manager. NetBSD 4 devices also need `sshpass` locally because their firmware does not provide a usable remote `scp`.

If this is your first time using the repo, this is the only command you should run with the repo-local launcher. After this step, use `.venv/bin/tcapsule ...` to run a command.

You can inspect the local repo-only install before continuing:

```bash
.venv/bin/tcapsule paths
.venv/bin/tcapsule validate-install
```

## Step 2: Create The Local Config

Run:

```bash
.venv/bin/tcapsule configure
```

This writes a hidden `.env` file in the repo folder, and the other `tcapsule` commands use that file as their local device configuration.

At the start of `configure`, the tool first tries to discover your Time Capsule on the local network via mDNS/Bonjour. If it finds one, it prefills the SSH target for you. If it does not find one, it falls back to the normal manual prompt flow.

`configure` also checks whether SSH is reachable. If SSH is closed, it enables SSH using the built-in Python 3 ACP client, reboots the device, waits for SSH to come up, and then continues the normal probing flow. If the password is wrong, it asks again instead of writing a broken `.env` file.

For typical users, most of the defaults are good enough. If the script offers a value and you do not have a reason to change it, **just pressing Enter is usually the correct choice**.

The default values you can customize are:

- Samba username: `admin`
- persistent payload folder name: `.samba4`
- device network interface: usually detected automatically

The password you enter here also becomes the password used for the SMB login as well. In other words, after setup, you normally connect with:

- username: `admin`
- password: the same Time Capsule password you entered during configuration

Samba does not magically use Apple’s internal password backend; unfortunately, using Apple's password system is not possible. We deliberately reuse the same password value so that the user experience is simpler and less confusing.

If you want to keep the device config somewhere else, pass `--config PATH` to `configure`; the other config-consuming commands accept the same option and load that path:

```bash
.venv/bin/tcapsule configure --config ./my-time-capsule.env
.venv/bin/tcapsule deploy --config ./my-time-capsule.env
.venv/bin/tcapsule doctor --config ./my-time-capsule.env
```

## Step 3: Deploy It

Run:

```bash
.venv/bin/tcapsule deploy
```

This step installs (or updates) Samba onto the device. It validates the checked-in binaries, copies the payload and boot files to the Time Capsule, and sets up the Samba password files. You can run `deploy` for a new version to update.

On NetBSD 6 devices, `deploy` then reboots the device so the new runtime comes up cleanly.
On NetBSD 4 devices, `deploy` instead activates the new runtime immediately without a reboot. Tested older devices still need `tcapsule activate` after later reboots.

By default, `tcapsule deploy` reboots NetBSD 6 devices after deployment and then waits for them to come back. If you want to skip the reboot confirmation prompt, you can run:

```bash
.venv/bin/tcapsule deploy --yes
```

There are also other flags such as `--no-nbns`, `--no-reboot` and `--dry-run`, but leave those alone unless you have a specific reason to use them.

If you want a machine-readable deployment plan without changing the device, use:

```bash
.venv/bin/tcapsule deploy --dry-run --json
```

## Step 4: Activate It Again If Needed

Run:

```bash
.venv/bin/tcapsule activate
```

This command is for older Gen 1-4 devices after a reboot. It starts Samba without copying the files again.  
For older hardware, this is currently needed after reboot because the firmware does not persist the `/etc` boot hook needed to auto-start Samba. 

Unfortunately, you need to run `activate` after *every* reboot if your device does not start Samba automatically.

Advanced NetBSD 4 users can back up the firmware with:

```bash
.venv/bin/tcapsule flash
```

The command is read-only by default. On supported devices, `tcapsule flash --patch` can install the persistent boot hook and `tcapsule flash --restore` can restore the active bank from Apple stock firmware downloaded from Apple's catalog. Both write modes modify only the primary copy (does not touch the secondary backup copy on the flash), and run validation by reading the bank back after ACP accepts the write.

Patch mode cannot not send a reboot or poweroff command after a successful write. After `tcapsule flash --patch` reports success, a user needs to manually
unplug the device to reboot, and then wait a few minutes for the device to boot to run `tcapsule doctor`. Restore mode can request a software reboot with `tcapsule flash --restore --reboot`; after that, use `tcapsule flash --check-apple` to verify the active bank matches Apple stock firmware.

## Step 5: Verify The Result

Run:

```bash
.venv/bin/tcapsule doctor
```

This is a non-destructive diagnostic command. `tcapsule doctor` checks:

- that the required local tools exist
- that your `.env` exists, is complete, and is valid
- that the checked-in binaries are present and match the expected checksums
- that SSH is reachable
- that the configured remote network interface, detected device compatibility, and selected payload family look sane
- that the managed runtime is up:
  - `smbd` is running and bound to TCP 445
  - the managed mDNS takeover is active
  - the NBNS responder is checked unless disabled
- what the box is currently advertising and serving for:
  - Bonjour instance name
  - Bonjour host label
  - Samba NetBIOS name
  - Samba share names
- that SMB is reachable
- that Bonjour `_smb._tcp` advertisement is visible and resolves
- that an authenticated SMB listing actually works and includes the active share name
- that authenticated SMB file operations also work on the share
- that `xattr_tdb:file` in the active Samba config points at persistent storage instead of the RAM disk

If you want the results in JSON instead of human-readable text, use:

```bash
.venv/bin/tcapsule doctor --json
```

## Step 6: Remove It Later If Needed

Run:

```bash
.venv/bin/tcapsule uninstall
```

This removes the managed TimeCapsuleSMB payload from the internal disk and removes the loader files from `/mnt/Flash`. Apple wipes the filesystem on the device after every reboot, except for `/mnt/Flash`, so that's where we install the loader script. If you delete the 6 non-Apple files we put in `/mnt/Flash`, and delete the `.samba4` folder on the hard drive, and then reboot, you can restore your machine to factory clean condition.

By default `uninstall` asks before rebooting the Time Capsule. If you want to skip the reboot confirmation prompt, use:

```bash
.venv/bin/tcapsule uninstall --yes
```

If you want to preview the uninstall plan without changing the device, use:

```bash
.venv/bin/tcapsule uninstall --dry-run
.venv/bin/tcapsule uninstall --dry-run --json
```

Uninstall success means the managed payload and boot files are gone after reboot. It does **not** check if Apple SMB or AFP to be enabled afterward. Those services may be on or off depending on the device's own settings. 

If you want to remove the files without rebooting immediately, use:

```bash
.venv/bin/tcapsule uninstall --no-reboot
```

## Connecting From Finder

Once deployment has completed and the Time Capsule has rebooted, you should be able to connect from Finder. The device should show up in the "Network" folder, or with:

```text
smb://<advertised-host>.local/<share-name>
```


When Finder prompts for credentials, use:

- username: `admin`
- password: your Time Capsule password

## Technical Notes

The rest of this README is more technical and explains why the repo is structured this way.

## Design

The Time Capsule hardware is very old and constrained. It has three relevant storage areas:

- `/mnt/Flash`, which is persistent but only has ~900KB of free space.
- `/mnt/Memory`, which is a 16MB ramdisk
- the internal HDD mounted under `/Volumes/dk2` or `/Volumes/dk3`, which is large but managed by Apple and unmounts when idle. You cannot run a binary off this location for that reason.

Unfortunately, it was not an option to "copy one binary somewhere and call it a day" to get `smbd` running. Thus, the current process is:

1. Keep the full `smbd` payload on the big internal hard disk.
2. Keep only a very small `rc.local` boot script on flash.
3. At boot, wait for the internal disk to appear and mount.
4. Copy the runtime binaries into `/mnt/Memory`.
5. Start Samba from the `/mnt/Memory`, not from the big disk Apple may later decide to unmount.
6. Advertise `_smb._tcp` with a separate tiny mDNS helper.

That is the reason the repository contains both:

- [bin/samba4/smbd](bin/samba4/smbd)
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)

and boot files such as:

- [src/timecapsulesmb/assets/boot/samba4/rc.local](src/timecapsulesmb/assets/boot/samba4/rc.local)
- [src/timecapsulesmb/assets/boot/samba4/start-samba.sh](src/timecapsulesmb/assets/boot/samba4/start-samba.sh)

There are other constraints the Time Capsule places on us:  
- The NetBSD 6 source code does not support earmv4 builds, so we need to build from NetBSD 7.
- Samba 3.x compiles easily, but it doesn't support directory traversal with SMB2 on NetBSD 6. This is a known bug apparently.
- Samba 4.0.x has the same issue
- Samba 4.2.x was much harder to compile, and had a `talloc` / `loadparm` use-after-free runtime bug
- Samba 4.3.x was the first version to work as a network share, but it does not support vfs_fruit for Time Machine backup support
- Samba 4.8.x was the first version that fully worked; current builds ship Samba 4.24.1.

## Troubleshooting

The normal result is:

- SMB username: `admin`
- Finder address: shown by `tcapsule doctor` or `tcapsule discover`
- share names: derived from the Time Capsule's disk volume names

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

If the system is working normally, you should see an `_smb._tcp` service with the Time Capsule's current device name.

Finder is not always the best first diagnostic tool. The service can be up and correct even when Finder browsing is being slow or temperamental.

### Finder Still Does Not Connect

Try a reboot, then try the direct address explicitly:

```text
smb://<advertised-host>.local/<share-name>
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

This explains the engineering constraints, historical dead ends, and current implementation in much more detail.

## Security Notes

This should be treated as a LAN-only setup. Do not expose this SMB service directly to the public internet! Do not forward ports to it. Do not pretend that an old Time Capsule turned into a modern hardened NAS just because the SMB side now works better. I have tested this with a M1 Macbook Pro and an A1470 Time Capsule. Your mileage may vary. 

Also note that the current auth model maps SMB access to `root` internally on the Time Capsule. That is a deliberate compatibility choice for this old firmware, as the version of NetBSD 6 running on the Time Capsule errors when Samba tries to switch users.

The commands have logging and telemetry enabled by default. Errors and exceptions are logged so they can be easily investigated later.

## For Developers And Maintainers

The checked-in binaries are already built. If you want to rebuild them yourself, the maintainer build flow lives under [build/](build) and depends on a NetBSD VM.

The main build outputs are:

- [bin/samba4/smbd](bin/samba4/smbd)
- [bin/samba4-netbsd4le/smbd](bin/samba4-netbsd4le/smbd)
- [bin/samba4-netbsd4be/smbd](bin/samba4-netbsd4be/smbd)
- [bin/mdns/mdns-advertiser](bin/mdns/mdns-advertiser)
- [bin/mdns-netbsd4le/mdns-advertiser](bin/mdns-netbsd4le/mdns-advertiser)
- [bin/mdns-netbsd4be/mdns-advertiser](bin/mdns-netbsd4be/mdns-advertiser)
- [bin/nbns/nbns-advertiser](bin/nbns/nbns-advertiser)
- [bin/nbns-netbsd4le/nbns-advertiser](bin/nbns-netbsd4le/nbns-advertiser)
- [bin/nbns-netbsd4be/nbns-advertiser](bin/nbns-netbsd4be/nbns-advertiser)

If you want the actual engineering details, read [DETAIL.md](DETAIL.md)
