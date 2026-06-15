# Frequently Asked Questions (FAQ)

## General Questions

#### What Time Capsule models are supported?

Gen 5 Time Capsules - fully supported with automatic startup
![Time Capsule Model](https://github.com/user-attachments/assets/5d0b044f-2137-4bb7-8d65-3d1bb251754c)

Gen 1-4 Time Capsules - supported with manual activation after each reboot

#### What AirPort Extreme models are supported?

AirPort Extreme models with attached USB storage are supported by the same deploy/runtime model, but they are less broadly validated than Time Capsule hardware. Use `tcapsule configure` and `tcapsule doctor` to confirm the specific device.

#### Is this safe to use?

Yep. This doesn't touch anything that will permanently brick a Time Capsule. This also does not delete any of your previous data. 

## Setup and Configuration

#### What is the "Device Password" mode?

TimeCapsuleSMB needs the device/root password during setup. That password is used to enable or access SSH from the app/CLI. The managed Samba runtime reads the current AirPort device password from `syPW` on the Time Capsule at boot and generates its RAM-only Samba auth files before `smbd` starts.

AirPort Utility commonly exposes this as **"With device password"** under disk sharing. This project does not validate the AirPort disk-sharing mode directly, but using the device password mode keeps the password model aligned with what the managed Samba runtime reads from AirPort config.

To check/change this:
1. Open AirPort Utility on your Mac
2. Select your Time Capsule
3. Go to the "Disks" tab
4. Look for the "Secure Shared Disks" setting
5. Ensure it's set to "With device password" mode

The current AirPort device password is the SMB password. If you change it in AirPort Utility, reboot the device so the managed runtime regenerates the RAM auth file.

#### Do I need to keep the TimeCapsuleSMB folder after setup?

**Yes, it is recommended to keep the TimeCapsuleSMB folder** on your Mac for maintenance purposes. While you can delete it after initial setup, keeping it allows you to:  
- Run `tcapsule doctor` to diagnose issues
- Run `tcapsule fsck` to repair the disk
- Run `tcapsule activate` after reboots (for Gen 1-4 NetBSD 4 devices)
- Run `tcapsule uninstall` if you want to remove it from the Time Capsule

The folder contains all the scripts, binaries, and configuration files needed for ongoing maintenance. It is safe to delete it after setup, but keep it or re-download it in order to run maintenance commands. 

#### How do I connect to the Time Capsule after setup?

Once deployment is complete, you can connect via:
- **Finder:** Look in the "Network" folder
- **Direct URL:** `smb://<advertised-host>.local/<share-name>` or `smb://<yourtimecapsuleIP>/<share-name>`

**Credentials:**
- Username: `admin` in the docs/examples. The managed Samba config maps incoming SMB usernames to Unix `root`.
- Password: Your Time Capsule password

#### Do I need to `uninstall` before updating?

No. You can run `deploy` over an old deployment.

## Troubleshooting

#### I'm not sure what went wrong

1. Reboot the device
2. Do a fresh `deploy` on top of the (maybe corrupt) old deploy

A reboot and clean deploy will fix 90% of issues. This is especially useful for old Gen 1-4 devices, because their firmware usually does not provide remote `scp`, so uploads use a slower SSH fallback. The deploy flow verifies uploaded file sizes, but rerunning `deploy` is still the simplest way to replace any interrupted upload.

#### Time Machine backups are broken on macOS?

Time Machine network backups have known macOS-side regressions on macOS 26.4.x and macOS 15.7.5-15.7.7. You may get an error like `The network backup disk could not be accessed because there was a problem with the network username or password. You may need to re-select the backup disk and enter the correct username and password.`

| macOS Version    |                      Release date |
| ---------------- | --------------------------------: |
| `26.4`           |                **March 24, 2026** |
| `15.7.5`         |                **March 24, 2026** |
| `26.4.1`         |                 **April 9, 2026** |
| `15.7.6`         |            **Beta versions only** |
| `15.7.7`         |                  **May 11, 2026** |

See this [Cult of Mac report](https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups) and this later [MacObserver report about a 26.5 fix](https://www.macobserver.com/news/macos-tahoe-26-4-breaks-time-machine-users-report-widespread-failures/) for context. Either update to macOS 26.5 or newer, or try the plist fix here: https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups

**Workaround:** Macs running these versions can still use the device as a standard Samba network share in Finder, but Time Machine backups will not work properly. You can also try the workaround mentioned in the article. See also this [community discussion regarding Error 80](https://community.qnap.com/t/time-machine-backup-fails-with-authentication-error-80-on-tbs-h574tx/5613/9).

#### If you have the OSStatus Error 80

OSStatus Error 80 can happen when macOS is trying to reuse an existing Time Machine backup and stale local backup state gets in the way.

Try these steps:

1. Make sure the Time Machine backup is not mounted or in use on any Mac.
2. In Finder, open the SMB share and find the affected `.sparsebundle`.
3. Right-click the `.sparsebundle`, choose "Show Package Contents", and delete the `lock` file if one is present.
4. Open Keychain Access and delete entries that reference the affected `.sparsebundle` or `.sparsebund` name, especially matching entries in the System keychain.

If Keychain Access cannot remove the entries, use Terminal to find and delete the matching generic password entries. Replace the example sparsebundle name with your real one:

```bash
sudo security find-generic-password -l "Bob's MacBook Pro.sparsebundle"
sudo security delete-generic-password -l "Bob's MacBook Pro.sparsebundle"
```

The `-l` option matches the keychain item label. You can also use `-a` for an account name or `-s` for a service name if the label does not match.

If macOS is searching a different keychain, list the available keychains and pass the specific keychain path at the end of the command:

```bash
security list-keychains
security find-generic-password -l "Bob's MacBook Pro.sparsebundle" ~/Library/Keychains/login.keychain-db
sudo security find-generic-password -l "Bob's MacBook Pro.sparsebundle" /Library/Keychains/System.keychain
sudo security delete-generic-password -l "Bob's MacBook Pro.sparsebundle" /Library/Keychains/System.keychain
```

If it still fails, check Keychain Access for older Time Machine entries that refer to the same Time Capsule or backup and remove only entries you recognize as related to this backup.

#### The Time Capsule doesn't show up in Finder

1. Try connecting directly:
   ```
   smb://<advertised-host>.local/<share-name>
   ```

2. Use the IP address from your `.env` file if hostname resolution fails:
   ```
   smb://<yourtimecapsuleIP>/<share-name>
   ```
#### I get a "MaSt" error

We use ACP `MaSt` to check what hard drives are connected to the device. If you see the message `No deployable HFS disk was found after 10 MaSt queries spaced 3 seconds apart`, that means we checked 10 times and the hard drive never loaded. 

- If you are using an AirPort Express with an external hard drive, make sure it is plugged in.
- If you are using an external hard drive, make sure it's properly formatted with HFS+
- If you have a Time Capsule with an internal hard drive, then Apple ACP cannot detect the hard drive for some reason. Try reformatting it or swapping to a different hard drive.

#### I get "Error 22" or "Invalid Argument" errors

**Error 22 / Invalid Argument errors usually indicate disk corruption.** 

This is usually not directly related to TimeCapsuleSMB, and can happen from things like "rebooting without doing a disk sync". This sometimes happens if you reboot while Samba is writing files to disk. 

Usually, this is not a severe issue, and a `fsck` fixes the disk. 

To fix this:
1. Run the disk repair command:
   ```bash
   .venv/bin/tcapsule fsck
   ```

2. If `fsck` doesn't resolve the issue, you may need to:
   - Back up your data if possible
   - Erase the disk using Apple AirPort Utility
   - Re-run the TimeCapsuleSMB setup

#### My Gen 1-4 device is not working after every reboot

This is normal for **NetBSD 4 devices** (older Gen 1-4 Time Capsules). The firmware doesn't persist the `/etc` boot hook needed to auto-start Samba.

**Solution:** Run `tcapsule activate` after rebooting older stock devices. A normal `tcapsule deploy` handles this automatically by rebooting, waiting for SSH to return, and then activating the deployed runtime.

## Security and Privacy

#### Is this secure?

It's *probably* fine for a home network, but if you're very sensitive about security this is not the software for you. Use at your own risk. It's using a build of Samba 4.24.3 currently.

#### What files are added to the Time Capsule?

The `deploy` script installs files in:
- `/mnt/Flash` on the Time Capsule (boot files)
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/watchdog.sh`
  - `/mnt/Flash/common.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/mdns-advertiser`
  - `/mnt/Flash/tcapsulesmb.conf`
  - These files are created by `mdns-advertiser`
    - `/mnt/Flash/allmdns.txt`
    - `/mnt/Flash/applemdns.txt`
- `.samba4` folder on the root of the hard drive (which contains Samba files)

All other files/folders are stored on ramdisks and will be deleted after a reboot.

The `uninstall` script removes these managed files and optionally reboots the device. 

## Getting Help

#### Where can I get help?

If you find any problems, please [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues). The developer is actively working on improvements.

#### What information should I include when reporting issues?

When filing an issue, please include:
1. Your Time Capsule model
2. macOS version you're using
3. Output of `tcapsule doctor`
4. Any error messages you're seeing
5. Steps to reproduce the problem

#### How do I get logs from my Mac?

Run:
```
mkdir -p /tmp/tm-debug
log stream --style compact --level debug --predicate '
  process IN {"backupd","backupd-helper","diskimagesiod","diskarbitrationd","NetAuthSysAgent"} OR
  process == "kernel" OR
  subsystem CONTAINS[c] "TimeMachine" OR
  subsystem CONTAINS[c] "smb"
  ' >> /tmp/tm-debug/tm-live.log 2>&1
```

The logs are then located at `/tmp/tm-debug/tm-live.log`

## Advanced Topics

#### Can I rebuild the binaries myself?

Yes! If you want to rebuild `smbd` yourself, run the scripts in `build/` on a NetBSD machine. The binaries are statically compiled, so you don't need anything else on the Time Capsule.

#### Can I customize the configuration?

In the macOS app, each saved device has advanced settings for the managed SMB runtime. These settings are saved to the local device profile first. Run **Install / Update** afterward to push runtime-affecting changes to the Time Capsule.

- **Mount wait seconds**: default `30`. How long deploy, uninstall, fsck, and related operations wait for the AirPort disk to wake and mount.
- **ATA idle seconds**: default `300`. Sets the built-in ATA disk idle timer when the managed runtime starts. Use `0` to disable the idle timer.
- **ATA standby seconds**: default blank. Optionally sets the built-in ATA disk standby timer. Leave blank to avoid applying a standby timer; use `0` to disable the standby timer.
- **Enable NBNS**: default on. Starts the NetBIOS name responder so older SMB/Windows-style network browsing can find the device.
- **Internal Share Uses Disk Root**: default off. When off, the internal disk share points at the managed `ShareRoot` folder. When on, it shares the whole internal disk root. External disks still share their mounted root.
- **Allow SMB Share Browsing**: default off. Relaxes anonymous browse restrictions so clients can enumerate shares more easily. Shares still require authentication.
- **Allow Any SMB Protocol**: default off. Removes the SMB2/SMB3-only protocol restriction. Leave off unless an old client needs legacy SMB compatibility.
- **Force Debug Logging**: default off. Enables verbose smbd/mDNS logging on the device. Use only for troubleshooting because it writes more logs.
- **Use Netatalk for metadata**: default on. Stores Apple/Finder metadata in the Netatalk-compatible format used by Samba's fruit module. If unchecked, use `fruit:metadata = stream` instead.

Share names and Bonjour names still come from the Time Capsule itself. For most users, the defaults are recommended.

## Maintenance

#### How do I update TimeCapsuleSMB?

Download a new zip file from the releases page: https://github.com/jamesyc/TimeCapsuleSMB/releases

To use git to update to a newer version:
1. `git pull` in the TimeCapsuleSMB folder
2. Run `tcapsule deploy` again
3. Run `tcapsule doctor` to verify

#### How do I completely remove TimeCapsuleSMB?

To remove TimeCapsuleSMB:
```bash
.venv/bin/tcapsule uninstall
```

This removes the managed payload and boot files. After a reboot, your Time Capsule will be restored to its factory condition (though Apple SMB/AFP settings may vary).

#### What if I want to keep the project folder but remove it from my Mac?

The deployed runtime can keep working without the local TimeCapsuleSMB folder, because the managed runtime files are stored on the Time Capsule. Keep the local folder if you want to update, redeploy, run `doctor`, run `fsck`, activate older Gen 1-4 devices, or uninstall cleanly.

#### What about the `flash` command?

The `flash` command will flash a NetBSD 4 device to automatically run `/mnt/Flash/rc.local` after reboot without running `activate`. This is the only command that's dangerous and can permanently brick your device, so use at your own caution. That being said, I added a lot of safety checks to `flash`, and I do not have any reports of it permanently bricking a device, but I intentionally didn't automatically `flash` older devices in `deploy` due to the additional risk. 
