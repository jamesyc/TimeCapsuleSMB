# Frequently Asked Questions (FAQ)

## General Questions

### What is TimeCapsuleSMB?

TimeCapsuleSMB is a project that configures a modern Samba setup to run directly on Apple AirPort Time Capsules. This allows Time Capsules to work as normal SMB servers on modern networks, since Apple removed SMB1 support from macOS and is removing AFP support.

### What Time Capsule models are supported?

This project is confirmed to work for:
- **NetBSD 6 based Time Capsules** - Fully supported with automatic startup
- **NetBSD 4 based Time Capsules** - Supported with manual activation after each reboot

Your Time Capsule should look like this:
![Time Capsule Model](https://github.com/user-attachments/assets/5d0b044f-2137-4bb7-8d65-3d1bb251754c)

### Is this safe to use?

This project is built by a developer in their free time, not Apple. While we don't expect it to permanently break your Time Capsule, it may mess up your configuration or data. We do not recommend using this if:
- You still use the Time Capsule as your primary router
- You have data on it that you're not comfortable losing
- You need 100% reliability

**Important:** This is not supported by Apple or any large company. Use at your own risk.

## Setup and Configuration

### What are the system requirements?

For the typical setup, you need:
- A Mac or Linux machine on the same local network as the Time Capsule
- The Time Capsule password
- Python 3.9+
- `smbclient` installed locally (for the `doctor` command)

### What is the "Device Password" mode?

**Critical:** Your Time Capsule must be set to **"Device Password"** mode, not "User/Password" mode. This is a common setup mistake that prevents the tool from working properly.

To check/change this:
1. Open AirPort Utility on your Mac
2. Select your Time Capsule
3. Go to the "Base Station" tab
4. Look for the password setting
5. Ensure it's set to "Device Password" mode

### Do I need to keep the TimeCapsuleSMB folder after setup?

**Yes, we recommend keeping the TimeCapsuleSMB folder** on your Mac for maintenance purposes. While you can delete it after initial setup, keeping it allows you to:
- Run `tcapsule doctor` to diagnose issues
- Run `tcapsule activate` after reboots (for NetBSD 4 devices)
- Run `tcapsule uninstall` if you want to remove the setup
- Update to newer versions when available

The folder contains all the scripts, binaries, and configuration files needed for ongoing maintenance.

### What are the default settings?

The default configuration values are:
- **SMB share name:** `Data`
- **Samba username:** `admin`
- **Bonjour service name:** `Time Capsule Samba`
- **Bonjour hostname label:** `timecapsulesamba`

The password you enter during setup becomes both the Time Capsule password and the SMB password.

### How do I connect to the Time Capsule after setup?

Once deployment is complete, you can connect via:
- **Finder:** Look in the "Network" folder
- **Direct URL:** `smb://timecapsulesamba4.local/Data`
- **IP address:** Use the IP from your `.env` file if Bonjour doesn't work

**Credentials:**
- Username: `admin`
- Password: Your Time Capsule password

## Troubleshooting

### Time Machine backups are broken on certain macOS versions

**Important:** Time Machine on macOS 26.4 and 15.7.5 is currently broken. See [this article](https://www.cultofmac.com/news/macos-tahoe-26-4-breaks-time-machine-network-backups) for details.

**Workaround:** Macs running these versions can still use the device as a standard Samba network share in Finder, but Time Machine backups will not work properly.

### The Time Capsule doesn't show up in Finder

1. Run the diagnostic command:
   ```bash
   .venv/bin/tcapsule doctor
   ```

2. Check Bonjour directly:
   ```bash
   dns-sd -B _smb._tcp local.
   ```

3. Try connecting directly:
   ```
   smb://timecapsulesamba4.local/Data
   ```

4. Use the IP address from your `.env` file if hostname resolution fails

### I get "Error 22" or "Invalid Argument" errors

**Error 22 / Invalid Argument errors usually indicate disk corruption.**

To fix this:
1. Run the disk repair command:
   ```bash
   .venv/bin/tcapsule fsck
   ```

2. If `fsck` doesn't resolve the issue, you may need to:
   - Back up your data if possible
   - Reformat the disk using Apple's tools
   - Re-run the TimeCapsuleSMB setup

### Deploy says SMB listing failed after reboot

This can happen if the device is still finishing startup. These old Time Capsule CPUs are not fast.

**Solution:** Wait a bit longer, then run:
```bash
.venv/bin/tcapsule doctor
```

### I need to run `activate` after every reboot

This is normal for **NetBSD 4 devices** (older Gen 1-4 Time Capsules). The firmware doesn't persist the `/etc` boot hook needed to auto-start Samba.

**Solution:** Always run `tcapsule activate` after rebooting older devices.

### Finder is slow to connect or browse

Finder is not always the best diagnostic tool. The service can be up and correct even when Finder browsing is slow or temperamental.

**Solution:** Use `tcapsule doctor` to verify the system is working correctly, rather than relying solely on Finder.

## Security and Privacy

### Is this secure?

This should be treated as a **LAN-only setup**. Do not:
- Expose this SMB service directly to the public internet
- Forward ports to it
- Assume the Time Capsule is now a hardened NAS

**Note:** The current auth model maps SMB access to `root` internally on the Time Capsule. This is a deliberate compatibility choice for the old firmware.

### What happens to my data?

The `deploy` script installs files in:
- `/mnt/Flash` on the Time Capsule (boot files)
- `.samba4` folder on the root of the hard drive (Samba files)

The `uninstall` script removes these managed files and can optionally reboot the device.

## Getting Help

### Where can I get help?

If you find problems, please [file an issue here](https://github.com/jamesyc/TimeCapsuleSMB/issues). The developer is actively working on improvements.

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

### What's the difference between NetBSD 6 and NetBSD 4 devices?

**NetBSD 6 devices:**
- Automatic startup on boot
- More reliable
- Recommended if available

**NetBSD 4 devices:**
- Manual `activate` required after every reboot
- Some extra caveats
- Older hardware

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

This removes the managed payload and boot files. After reboot, your Time Capsule will be restored to factory condition (though Apple SMB/AFP settings may vary).

### What if I want to keep the project folder but remove it from my Mac?

You can safely delete the TimeCapsuleSMB folder from your Mac after setup. All the important files are stored on the Time Capsule itself. However, we recommend keeping it for maintenance purposes (see above).