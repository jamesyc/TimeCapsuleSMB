import Foundation

/// Brings device profiles saved by earlier versions of the app up to the
/// current format, once, when `DeviceRegistryRepository.load()` reads them,
/// so the rest of the app reads one format.
///
/// Each profile records its `formatVersion`, per profile rather than for the
/// whole file: an older app reads the file as it always did and drops the
/// unknown key when it saves, and the next launch of this app migrates that
/// profile again, which every step allows (each can run twice). For the same
/// reason this app never writes a version newer than its own
/// (`DeviceProfile.encode`): it drops keys it does not know, so a newer app
/// must run its later steps again.
///
/// - 0 to 1, on the profile's JSON before it is decoded: the old name of
///   "turn off SMB encryption"; the old opt-in aio_fork key, whose saved
///   false is ignored; settings combinations the device refuses (required
///   encryption with any protocol or with encryption turned off); an idle
///   time the device runs as 300 seconds, saved as 300; and the English
///   deploy and runtime summaries saved before summary keys existed, given
///   their keys.
///
/// The profile's .env is not brought up to date: app versions before v3.0.0
/// never wrote the settings there, and every operation that uses them
/// (configure, update-config-settings, deploy, Checkup) gets all of them
/// from the profile. Deploy and Checkup still validate the .env, so the
/// backend accepts what older versions saved there, such as an idle time
/// past the limit, which it reads as the device does (300).
enum DeviceProfileMigrations {
    static let currentVersion = 1
    /// The copy of devices.json kept before the first write that migrates it.
    static let backupFileName = "devices.json.before-format-1"

    /// Moves every profile in a devices.json to `currentVersion`. Returns the
    /// file to decode, and whether any profile changed. A file that is not a
    /// list of objects is returned as is, for the decoder to report.
    static func migrate(registryData data: Data) -> (data: Data, migrated: Bool) {
        guard var profiles = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]] else {
            return (data, false)
        }
        var migrated = false
        for index in profiles.indices where migrate(&profiles[index]) {
            migrated = true
        }
        guard migrated, let migratedData = try? JSONSerialization.data(withJSONObject: profiles) else {
            return (data, false)
        }
        return (migratedData, true)
    }

    /// Brings saved `DeviceProfileSettings` JSON to the current keys: the old
    /// name of "turn off SMB encryption", the ignored opt-in aio_fork key,
    /// combinations the device refuses, and an idle time past
    /// `ValueParsers.maxAtaIdleSeconds`, which the device runs as 300: the
    /// app no longer accepts one, and a Checkup would warn after every
    /// install. App settings keep their default
    /// device settings in the same form (AppSettingsRepository.load), so
    /// both run it; it can run any number of times.
    static func migrateSettings(_ settings: inout [String: Any]) {
        if let old = settings.removeValue(forKey: "forceDisableSMBSigningAndEncryption"),
           settings["disableSMBEncryption"] == nil {
            settings["disableSMBEncryption"] = old
        }
        settings.removeValue(forKey: "vfsAIOForkEnabled")
        if settings["requireSMBEncryption"] as? Bool == true {
            settings["anyProtocol"] = false
            settings["disableSMBEncryption"] = false
        }
        if let idle = settings["ataIdleSeconds"] as? Int, idle > ValueParsers.maxAtaIdleSeconds {
            settings["ataIdleSeconds"] = deviceAtaIdleFallbackSeconds
        }
    }

    /// What samba/config.c runs an idle time it does not accept as.
    static let deviceAtaIdleFallbackSeconds = 300

    /// Moves one profile's JSON to `currentVersion`. Returns whether it changed.
    static func migrate(_ profile: inout [String: Any]) -> Bool {
        guard (profile["formatVersion"] as? Int ?? 0) < currentVersion else {
            return false
        }
        if var settings = profile["settings"] as? [String: Any] {
            migrateSettings(&settings)
            profile["settings"] = settings
        }
        for key in ["lastDeployState", "runtimeState"] {
            guard var snapshot = profile[key] as? [String: Any] else { continue }
            if snapshot["summaryRef"] == nil || snapshot["summaryRef"] is NSNull,
               let text = snapshot["summary"] as? String,
               let summary = LegacySummaryText.summary(for: text),
               let encoded = try? JSONEncoder().encode(summary),
               let object = try? JSONSerialization.jsonObject(with: encoded) {
                snapshot["summaryRef"] = object
                profile[key] = snapshot
            }
        }
        profile["formatVersion"] = currentVersion
        return true
    }
}

/// The English summaries profiles saved before summary keys existed, with
/// their keys; DeviceProfileMigrations stores the key once, so the line
/// follows the app language. New snapshots store a `BackendSummary`.
enum LegacySummaryText {
    static func summary(for text: String) -> BackendSummary? {
        let normalized = text.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if normalized.hasPrefix("netbsd4 activation complete.") {
            return BackendSummary(key: "backend.summary.activation_completed_followup", text: text)
        }
        guard let key = keys[normalized] else {
            return nil
        }
        return BackendSummary(key: "backend.summary.\(key)", text: text)
    }

    private static let keys: [String: String] = [
        "deployment completed.": "deploy_completed",
        "runtime activation complete.": "deploy_completed",
        "netbsd4 payload was already active.": "activation_already_active",
        "netbsd4 activation completed.": "activation_completed",
        "doctor checks passed.": "doctor_checks_passed",
        "doctor found one or more fatal problems.": "doctor_found_fatal",
    ]
}
