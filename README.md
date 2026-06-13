# TimeCapsuleSMB

Apple AirPort Time Capsules only support AFP and SMB1. Apple is removing AFP support in macOS 27, and removed SMB1 support from macOS a long time ago. This repo sets up modern Samba that runs directly on the Time Capsule itself; macOS 27 can connect to the Time Capsule as a network share, and use it for Time Machine backups. 

**NOTE THAT TIME MACHINE ON MACOS 26.4.x (AND 15.7.5-15.7.7) IS CURRENTLY BROKEN**, see https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups  
Macs running macOS 26.4.x can still use the device as a standard Samba network share in Finder, or upgrade to macOS 26.5 or newer.

This project has 2 parts:
- a fork of Samba 4, modified to work on the Apple Time Capsule
- the installers for the Samba binary, via python or the **macOS GUI app**. 

This is now fully working for all Time Capsules: the Time Capsule will run its own Samba 4.24.3 server, advertise itself over Bonjour (show up automatically in the "Network" folder on macOS), and accept authenticated SMB3 connections. You should then be able to open Finder, choose Connect to Server, and use a normal SMB URL instead of relying on Apple’s legacy stack. You should also be able to use the disk for Time Machine backups:  
<img width="478" height="268" alt="image" src="https://github.com/user-attachments/assets/c713a1c6-ff71-43a2-a057-451223a1c0e0" />  
This is now able to fully reproduce the full Apple experience: after you install this, you do not have to worry about it again, even if the device IP address changes. It will show up automatically in Time Machine in settings app, and it will use mDNS/Bonjour so it will work fine even if the IP address is not static and gets changed.

The "Install" or `deploy` script will install files in `/mnt/Flash` on the Time Capsule, plus a `.samba4` folder on the root of the hard drive. The `uninstall` script removes those managed files and can optionally reboot the device afterward.

NetBSD 6 devices automatically startup on boot. **Older NetBSD 4 devices may need a manual `activate` after every reboot**, or you can flash the firmware (to add a boot hook) to allow it to automatically start Samba on reboot. If you do not run the `activate` command after a reboot or flash the boot hook, then Samba will not start automatically on an older Time Capsule!

The current authentication model accepts any user as the username, and the Samba password is the current Time Capsule device password. At boot, the device reads its live AirPort `syPW` value and generates the Samba password file in RAM, so a device-password change is picked up after reboot. Guest access is disabled.

AirPort Extreme devices are not officially supported. Unofficially, they work fine. Note that this is installed to the hard drive, so it will not work for an Airport Extreme without a hard drive (as there is not enough space to store the binaries on the flash memory).   

## Requirements

The working binaries are saved in this repository under [bin/](bin), and the normal user workflow uses those checked-in files directly. You do not need to build Samba yourself, but if you want to rebuild `smbd` by yourself, run the scripts in `build/` on a NetBSD machine. 

Also, if you are an expert and want to DIY the install, you can copy the binary at [/bin/samba4/smbd](/bin/samba4/smbd) for NetBSD 6 devices, [/bin/samba4-netbsd4le/smbd](/bin/samba4-netbsd4le/smbd) for NetBSD 4 little-endian devices, or [/bin/samba4-netbsd4be/smbd](/bin/samba4-netbsd4be/smbd) for NetBSD 4 big-endian devices onto the Time Capsule and set it up yourself. The binaries are statically compiled, so you don't need anything else. 

You will need:  
- A macOS 14+ or Linux machine on the same local network as the Time Capsule
- The password for the Time Capsule

For the python setup, you need:  
- Python 3.9+
- `smbclient` installed locally for `doctor`
- Homebrew installed for macOS users

During first-time setup, if necessary `configure` can enable SSH on the Time Capsule.

## Quick Start (macOS app)

1. Download the latest release of the app from here: https://github.com/jamesyc/TimeCapsuleSMB/releases
2. Unzip the app and run it. If you get a "cannot be opened" warning, you need to manually disable Gatekeeper for this app.
3. Make sure Local Network permissions is granted (System Settings → Privacy & Security → Local Network → make sure TimeCapsuleSMB is allowed, then quit/reopen the app). Close and re-open the app after granting permissions.
4. Click "Add Device" on the left sidebar, and select your device. 
5. Enter your device password, and click "Save Device". 
6. Wait for the app to enable SSH for your Time Capsule.
    - If it fails, close the app, reopen the app, removed the saved device, and try again.  
7. Click the added device in the left sidebar, and then click on the "Install/Update" tab.  
   <img width="543" height="390" alt="image" src="https://github.com/user-attachments/assets/ea17ef0e-7624-4a06-888c-72ba6f8d4f8f" />  
8. Click "Install/Update" to deploy to the device.  
   <img width="544" height="390" alt="image" src="https://github.com/user-attachments/assets/49975391-29e5-46df-b249-2a75762983a7" />    
    - If deploying to the device fails, try removing the saved device from the app, then go back to step 4 above to "Add Device" again. It sometimes takes more than one deploy to copy all the files over. 
9. (For gen 1-4 devices only) Go to the maintenance page "Persistent NetBSD4 Boot Hook" section. Install the firmware patch to allow the device to automatically start Samba after reboots. Click "Back Up and Inspect" and "Plan Patch" to check if it can be installed; then run "Write Patch" to flash it to your device.    
   <img width="634" height="429" alt="image" src="https://github.com/user-attachments/assets/e35d8934-975b-4079-8087-8c22984a3165" />
10. (Optional) Wait 5-10 minutes for Samba to fully start up, then go to the Checkup tab and run a Checkup.  

## Quick Start (with python)

Download (or run `git clone`) this repository to a folder on your Mac or Linux machine.

From the root of this repository, the normal quick start commands to run are:

1. `./tcapsule bootstrap`
2. `.venv/bin/tcapsule configure` save a config/settings file
3. `.venv/bin/tcapsule deploy` deploy to the Time Capsule according to the config file
4. `.venv/bin/tcapsule doctor` check if everything is working
5. `.venv/bin/tcapsule flash` to backup the flash memory, and `.venv/bin/tcapsule flash --patch` to patch it

If you run into any issues:

- `.venv/bin/tcapsule activate` after reboot on NetBSD 4 devices if Samba did not auto-start
- `.venv/bin/tcapsule fsck` if the internal disk needs repair before deploy
- `.venv/bin/tcapsule discover` to list all mDNS/Bonjour devices
- `.venv/bin/tcapsule repair-xattrs` to repair any broken files on the disk from bad xattrs
- `.venv/bin/tcapsule uninstall` if you want to remove TimeCapsuleSMB later

Just delete this `TimeCapsuleSMB` folder if you want to remove it from your Mac after you're done setting up the Time Capsule. All the scripts/binaries/etc are stored in the `TimeCapsuleSMB` folder (so if you want to clean up your Mac, then just deleting the folder is fine).

If you find any bugs, I would appreciate it if you [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues) for help.

## Step 1: Prepare Your Host

Run:

```bash
./tcapsule bootstrap
```

This command prepares the local Python environment in this folder. It creates the `.venv` folder, installs the Python dependencies needed for discovery, deployment, and verification, and sets up the local `tcapsule` command into that virtualenv.

If `smbclient` or `sshpass` is missing, `bootstrap` will try to install it with Homebrew on macOS 14+ or the detected package manager on Linux. Older macOS versions can continue only when `smbclient` and `sshpass` are already installed manually. NetBSD 4 devices need `sshpass` because their firmware does not provide a usable remote `scp`.

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

The password you enter here is stored locally as `TC_PASSWORD` so the tool can keep using SSH and ACP. The managed Samba runtime reads the current device password on the Time Capsule at boot. In other words, after setup, you normally connect with:

- username: `admin` (or any other password)
- password: the same Time Capsule password you entered during configuration

Samba does not use Apple’s internal password backend directly. The boot script reads the AirPort `syPW` setting, asks `mdns-advertiser` to generate the NT hash, and writes the RAM-only Samba auth files before `smbd` starts.

## Step 3: Deploy It

Run:

```bash
.venv/bin/tcapsule deploy
```

This step installs (or updates) Samba onto the device. It validates the checked-in binaries and copies the payload and boot files to the Time Capsule. Samba password files are generated on the device in RAM each time the managed runtime stages. You can run `deploy` for a new version to update.

On Gen 5 NetBSD 6 devices, `deploy` reboots the device so the new runtime comes up cleanly.
On older Gen 1-4 NetBSD 4 devices, `deploy` also reboots to clear the RAM disk, waits for SSH to return, and then runs `/mnt/Flash/rc.local`. The older devices still need `tcapsule activate` after later reboots that are not part of `deploy`.

By default, `tcapsule deploy` reboots after deployment and then waits for the device to come back. If you want to skip the reboot confirmation prompt, you can run:

```bash
.venv/bin/tcapsule deploy --yes
```

There are also other flags such as `--no-nbns`, `--no-reboot` and `--dry-run`, but leave those alone unless you have a specific reason to use them. `--no-reboot` uploads the files, stops the manager process and `wcifsfs`, and starts the deployed runtime immediately by running `/mnt/Flash/rc.local`.

If you want a machine-readable deployment plan without changing the device, use:

```bash
.venv/bin/tcapsule deploy --dry-run --json
```

## Step 4: Flash, or Activate It Again If Needed

This is for older Gen 1-4 devices. Run:

```bash
.venv/bin/tcapsule flash
```

To back up the flash on your device. Then run: 

```bash
.venv/bin/tcapsule flash --patch
```
This will then patch a small boot hook launcher (to the primary firmware bank only). It just tells the device to run the `/mnt/Flash/rc.local` file at every startup.

On supported devices, `tcapsule flash --patch` can install the persistent boot hook and `tcapsule flash --restore` can restore the selected active bank from Apple stock firmware downloaded from Apple's catalog. Both write modes modify only one bank and leave the other flash bank untouched, then run validation by reading the written bank back after ACP accepts the write. Patch mode normally requires exactly one safely selected active bank; `--force` is available only for patch mode and bypasses the backup/active-candidate preflight to target the primary bank.

Patch mode cannot send a reboot or poweroff command after a successful write. After `tcapsule flash --patch` reports success, a user needs to manually
unplug the device to reboot, and then wait a few minutes for the device to boot to run `tcapsule doctor`. Restore mode can request a software reboot with `tcapsule flash --restore --reboot`; after that, use `tcapsule flash --check-apple` to verify the active bank matches Apple stock firmware.

If you do not want to patch the device, run:

```bash
.venv/bin/tcapsule activate
```

It starts Samba without copying the files again.  
For older Gen 1-4 hardware, this is currently needed after reboot because by default the firmware does not persist the boot hook needed to auto-start Samba. 

Unfortunately, you need to run `activate` after *every* reboot for older devices that do not start Samba automatically, if you do not run `flash`.

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

This removes the managed TimeCapsuleSMB payload from the internal disk and removes the loader files from `/mnt/Flash`. Apple wipes the filesystem on the device after every reboot, except for `/mnt/Flash`, so that's where we install the loader scripts. If you delete the 7 payload files in `/mnt/Flash`, delete the `.samba4` folder on the hard drive, and then reboot, you can restore your machine to factory clean condition.

By default `uninstall` asks before rebooting the Time Capsule. If you want to skip the reboot confirmation prompt, use:

```bash
.venv/bin/tcapsule uninstall --yes
```

If you want to preview the uninstall plan without changing the device, use:

```bash
.venv/bin/tcapsule uninstall --dry-run
.venv/bin/tcapsule uninstall --dry-run --json
```

Uninstall success means the managed payload and boot files are gone after reboot. It does **not** check whether Apple SMB or AFP is enabled afterward. Those services may be on or off depending on the device's own settings. 

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

- username: `admin` or any username
- password: your Time Capsule password

## Technical Notes

The rest of this README is more technical and explains why the repo is structured this way.

## Design

The Time Capsule hardware is extremely old and constrained. It has three relevant storage areas:

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
- [src/timecapsulesmb/assets/boot/samba4/boot.sh](src/timecapsulesmb/assets/boot/samba4/boot.sh)
- [src/timecapsulesmb/assets/boot/samba4/manager.sh](src/timecapsulesmb/assets/boot/samba4/manager.sh)

There are other constraints the Time Capsule places on us:  
- The NetBSD 6 source code does not support earmv4 builds, so we need to build from NetBSD 7.
- Samba 3.x compiles easily, but it doesn't support directory traversal with SMB2 on NetBSD 6. This is a known bug apparently.
- Samba 4.0.x has the same issue
- Samba 4.2.x was much harder to compile, and had a `talloc` / `loadparm` use-after-free runtime bug
- Samba 4.3.x was the first version to work as a network share, but it does not support vfs_fruit for Time Machine backup support
- Samba 4.8.x was the first version that fully worked; current builds ship Samba 4.24.3.

## Troubleshooting

Please read [FAQ.md](FAQ.md) as well. 

### The Time Capsule Does Not Show Up In Finder

Run:

```bash
.venv/bin/tcapsule doctor
```

Then check Bonjour directly:

```bash
dns-sd -B _smb._tcp local.
```

To inspect discovery results from the tool itself, run:

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

This should be treated as a LAN-only setup. Do not expose this SMB service directly to the public internet! Do not forward ports to it. I have tested this with an M1 Macbook Pro and an A1470 Time Capsule. Your mileage may vary. 

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
