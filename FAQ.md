# Frequently Asked Questions (FAQ)

## General Questions

### What Time Capsule models are supported?

Gen 5 Time Capsules - fully supported with automatic startup
![Time Capsule Model](https://github.com/user-attachments/assets/5d0b044f-2137-4bb7-8d65-3d1bb251754c)

Gen 1-4 Time Capsules - supported with manual activation after each reboot

### What AirPort Extreme models are supported?

None of them, officially. Unofficially, they might work. I don't own an Airport Extreme, though, so I cannot test to see if anything is working or broken. Use at your own risk. 

### Is this safe to use?

Yep. This doesn't touch anything that will permanently brick a Time Capsule. This also does not delete any of your previous data. 

## Setup and Configuration

### What is the "Device Password" mode?

Your Time Capsule must be set to **"Device Password"** mode, not "With accounts" mode; you will get errors if you use "With accounts" mode.

To check/change this:
1. Open AirPort Utility on your Mac
2. Select your Time Capsule
3. Go to the "Disks" tab
4. Look for the "Secure Shared Disks" setting
5. Ensure it's set to "With device password" mode

The device password you enter during setup will become the SMB password.

### Do I need to keep the TimeCapsuleSMB folder after setup?

**Yes, it is recommended to keep the TimeCapsuleSMB folder** on your Mac for maintenance purposes. While you can delete it after initial setup, keeping it allows you to:  
- Run `tcapsule doctor` to diagnose issues
- Run `tcapsule fsck` to repair the disk
- Run `tcapsule activate` after reboots (for Gen 1-4 NetBSD 4 devices)
- Run `tcapsule uninstall` if you want to remove it from the Time Capsule

The folder contains all the scripts, binaries, and configuration files needed for ongoing maintenance.

### How do I connect to the Time Capsule after setup?

Once deployment is complete, you can connect via:
- **Finder:** Look in the "Network" folder
- **Direct URL:** `smb://<yourtimecapsuleIP>/Data`

**Credentials:**
- Username: `admin`
- Password: Your Time Capsule password

## Troubleshooting

### Time Machine backups are broken on certain macOS versions

Time Machine backups on macOS 26.4.x and 15.7.5 is currently broken. See [this article](https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups) for details.

**Workaround:** Macs running these versions can still use the device as a standard Samba network share in Finder, but Time Machine backups will not work properly. You can also try the workaround mentioned in the article.

### The Time Capsule doesn't show up in Finder

1. Try connecting directly:
   ```
   smb://<yourtimecapsule>.local/Data
   ```

2. Use the IP address from your `.env` file if hostname resolution fails

### I get "Error 22" or "Invalid Argument" errors

**Error 22 / Invalid Argument errors usually indicate disk corruption.**

To fix this:
1. Run the disk repair command:
   ```bash
   .venv/bin/tcapsule fsck
   ```

2. If `fsck` doesn't resolve the issue, you may need to:
   - Back up your data if possible
   - Erase the disk using Apple AirPort Utility
   - Re-run the TimeCapsuleSMB setup

### I need to run `activate` after every reboot

This is normal for **NetBSD 4 devices** (older Gen 1-4 Time Capsules). The firmware doesn't persist the `/etc` boot hook needed to auto-start Samba.

**Solution:** Always run `tcapsule activate` after rebooting older devices.

## Security and Privacy

### Is this secure?

Hahahahahaha no. It's using a build of Samba 4.8.12 from 2019. It's *probably* fine for a home network, but if you're very sensitive about security this is not the software for you. Use at your own risk. 

### What files are added to the Time Capsule?

The `deploy` script installs files in:
- `/mnt/Flash` on the Time Capsule (boot files)
  - `/mnt/Flash/rc.local`
  - `/mnt/Flash/start-samba.sh`
  - `/mnt/Flash/watchdog.sh`
  - `/mnt/Flash/common.sh`
  - `/mnt/Flash/dfree.sh`
  - `/mnt/Flash/mdns-advertiser`
  - These files are created by `mdns-advertiser`
    - `/mnt/Flash/allmdns.txt`
    - `/mnt/Flash/applemdns.txt`
- `.samba4` folder on the root of the hard drive (Samba files). This folder name can be changed in the `.env` settings.

All other files/folders are stored on ramdisks and will be deleted after a reboot.

The `uninstall` script removes these managed files and optionally reboots the device. 

## Getting Help

### Where can I get help?

If you find any problems, please [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues). The developer is actively working on improvements.

### What information should I include when reporting issues?

When filing an issue, please include:
1. Your Time Capsule model
2. macOS version you're using
3. Output of `tcapsule doctor`
4. Any error messages you're seeing
5. Steps to reproduce the problem

## Advanced Topics

### Can I rebuild the binaries myself?

Yes! If you want to rebuild `smbd` yourself, run the scripts in `build/` on a NetBSD machine. The binaries are statically compiled, so you don't need anything else on the Time Capsule.

### Can I customize the configuration?

Yes! During `tcapsule configure`, you can customize:
- SMB share name
- Samba username
- Bonjour service name
- Bonjour hostname label

However, for most users, the defaults are recommended.

## Maintenance

### How do I update TimeCapsuleSMB?

To update to a newer version:
1. `git pull` in the TimeCapsuleSMB folder
2. Run `tcapsule deploy` again
3. Run `tcapsule doctor` to verify

### How do I completely remove TimeCapsuleSMB?

To remove TimeCapsuleSMB:
```bash
.venv/bin/tcapsule uninstall
```

This removes the managed payload and boot files. After a reboot, your Time Capsule will be restored to its factory condition (though Apple SMB/AFP settings may vary).

### What if I want to keep the project folder but remove it from my Mac?

You can safely delete the TimeCapsuleSMB folder from your Mac after setup. All the important files are stored on the Time Capsule itself. However, it is recommended to keep it for maintenance purposes (see above).