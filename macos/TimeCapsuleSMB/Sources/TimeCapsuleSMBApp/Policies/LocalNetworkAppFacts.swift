import AppKit
import Foundation

/// What this Mac shows about why macOS may block this copy of the app from
/// the local network while "Local Network" lists it as allowed. The Local
/// Network card and Checkup's block line put the matching steps first.
struct LocalNetworkAppFacts: Equatable {
    /// Other copies of this app that macOS knows of: macOS can block a copy
    /// when there are several. Copies on a mounted disk image are apart,
    /// since those are ejected, not deleted.
    let otherCopies: [String]
    let diskImageCopies: [String]
    /// This version first ran on this Mac a few minutes ago, after an older
    /// one: macOS can block a new version for a few minutes.
    let justUpdated: Bool

    static let empty = LocalNetworkAppFacts(otherCopies: [], diskImageCopies: [], justUpdated: false)

    /// The paths a step names; the rest are counted.
    static let namedCopiesLimit = 3

    /// A volume in the mount table.
    struct Volume: Equatable {
        let mountPoint: String
        let isReadOnly: Bool
        let isLocal: Bool
    }

    static func current() -> LocalNetworkAppFacts {
        let volumes = mountedVolumes()
        return LocalNetworkAppFacts(
            copies: copies(
                of: .main,
                applicationURLs: { NSWorkspace.shared.urlsForApplications(withBundleIdentifier: $0) },
                volumes: volumes,
                exists: { FileManager.default.fileExists(atPath: $0) }
            ),
            volumes: volumes,
            justUpdated: AppLaunchHistory(defaults: .standard).justUpdated(now: Date())
        )
    }

    /// Only an app has copies: a test runner's bundle has none to report. A
    /// copy opened where it was downloaded runs from a temporary path
    /// (App Translocation), and its own download would look like another copy.
    /// LaunchServices keeps copies that were deleted, and knows those in Time
    /// Machine backups, which are not the user's to delete.
    ///
    /// A card is drawn on the main thread, so only a copy on the startup disk
    /// is looked at: another volume's disk may be asleep, or a network share
    /// that no longer answers, and an automounted path mounts when touched.
    /// The mount table says, without asking any disk, that the volume is still
    /// mounted; a copy on a network volume is left out, as nothing can tell
    /// whether it is still there.
    static func copies(
        of bundle: Bundle,
        applicationURLs: (String) -> [URL],
        volumes: [Volume],
        exists: (String) -> Bool
    ) -> [String] {
        guard bundle.bundleURL.pathExtension == "app",
              !bundle.bundleURL.path.contains("/AppTranslocation/"),
              let identifier = bundle.bundleIdentifier else {
            return []
        }
        let own = bundle.bundleURL.resolvingSymlinksInPath().standardizedFileURL.path
        let copies = applicationURLs(identifier).compactMap { url -> String? in
            let path = url.standardizedFileURL.path
            if isInBackup(path) {
                return nil
            }
            guard let volume = volume(of: path, in: volumes), volume.mountPoint != "/" else {
                let resolved = url.resolvingSymlinksInPath().standardizedFileURL.path
                return exists(resolved) ? resolved : nil
            }
            return volume.isLocal ? path : nil
        }
        return Array(Set(copies.filter { $0 != own })).sorted()
    }

    /// Time Machine's backups on APFS and on HFS+, and its local snapshots.
    private static func isInBackup(_ path: String) -> Bool {
        ["/.timemachine/", "/Backups.backupdb/", "/com.apple.TimeMachine.localsnapshots/"].contains { path.contains($0) }
    }

    /// The mounted volume that holds a path, from the path alone.
    static func volume(of path: String, in volumes: [Volume]) -> Volume? {
        volumes
            .filter { $0.mountPoint == "/" || path.hasPrefix($0.mountPoint + "/") }
            .max { $0.mountPoint.count < $1.mountPoint.count }
    }

    /// MNT_NOWAIT reads the kernel's table without asking any file system.
    static func mountedVolumes() -> [Volume] {
        var table: UnsafeMutablePointer<statfs>?
        let count = getmntinfo_r_np(&table, MNT_NOWAIT)
        guard count > 0, let table else {
            return []
        }
        defer { free(table) }
        return UnsafeBufferPointer(start: table, count: Int(count)).map { entry in
            var name = entry.f_mntonname
            let mountPoint = withUnsafeBytes(of: &name) { String(decoding: $0.prefix { $0 != 0 }, as: UTF8.self) }
            return Volume(
                mountPoint: mountPoint,
                isReadOnly: entry.f_flags & UInt32(MNT_RDONLY) != 0,
                isLocal: entry.f_flags & UInt32(MNT_LOCAL) != 0
            )
        }
    }

    /// Sent with every helper request (HelperRunner), so telemetry can tell
    /// whether other copies or an update explain the blocks it sees.
    var requestFields: [String: JSONValue] {
        [
            "macos_app_other_copies": .number(Double(otherCopies.count + diskImageCopies.count)),
            "macos_app_just_updated": .bool(justUpdated)
        ]
    }

    /// The steps that go first on the Local Network card. Other copies
    /// outrank an update, as one copy after another starting looks like
    /// updates too.
    var localizedSteps: [String] {
        let copies = copySteps(conditional: false)
        return copies.isEmpty && justUpdated ? [L10n.string("recovery.local_network.just_updated")] : copies
    }

    /// What to do about other copies, if macOS blocks the app, for a card
    /// about a block nothing proved.
    var ifBlockedSteps: [String] {
        copySteps(conditional: true)
    }

    private func copySteps(conditional: Bool) -> [String] {
        let prefix = conditional ? "recovery.local_network.if_blocked." : "recovery.local_network."
        var steps: [String] = []
        if !otherCopies.isEmpty {
            steps.append(L10n.format("\(prefix)other_copies", Self.named(otherCopies)))
        }
        if !diskImageCopies.isEmpty {
            steps.append(L10n.format("\(prefix)disk_image_copies", Self.named(diskImageCopies)))
        }
        return steps
    }

    private static func named(_ paths: [String]) -> String {
        let named = paths.prefix(namedCopiesLimit).joined(separator: ", ")
        return paths.count > namedCopiesLimit ? "\(named), …" : named
    }
}

extension LocalNetworkAppFacts {
    /// A disk image mounts read-only under /Volumes; an external disk with a
    /// copy on it does not, and neither does the system volume.
    init(copies: [String], volumes: [Volume], justUpdated: Bool) {
        let onDiskImage = copies.filter { copy in
            Self.volume(of: copy, in: volumes).map { $0.mountPoint.hasPrefix("/Volumes/") && $0.isReadOnly } == true
        }
        self.init(
            otherCopies: copies.filter { !onDiskImage.contains($0) },
            diskImageCopies: onDiskImage,
            justUpdated: justUpdated
        )
    }
}

/// When this version of the app first ran on this Mac, and whether an older
/// version ran before it. Kept in the app's defaults: an update replaces the
/// app, so only a launch can tell a new version from the one before.
public struct AppLaunchHistory {
    static let justUpdatedWindow: TimeInterval = 5 * 60

    private static let versionKey = "launchHistory.version"
    private static let firstLaunchKey = "launchHistory.versionFirstLaunchedAt"
    private static let updatedKey = "launchHistory.versionUpdatedAnother"

    private let defaults: UserDefaults

    init(defaults: UserDefaults) {
        self.defaults = defaults
    }

    public static func recordLaunch(bundle: Bundle = .main) {
        let version = bundle.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "development"
        AppLaunchHistory(defaults: .standard).record(version: version, now: Date())
    }

    /// An older copy starting is not an update. A version that is not a
    /// build number, as in a development build, is never one either.
    func record(version: String, now: Date) {
        let previous = defaults.string(forKey: Self.versionKey)
        guard previous != version else {
            return
        }
        defaults.set(version, forKey: Self.versionKey)
        defaults.set(now, forKey: Self.firstLaunchKey)
        let updated = previous.flatMap(Int.init).map { old in Int(version).map { $0 > old } ?? false } ?? false
        defaults.set(updated, forKey: Self.updatedKey)
    }

    func justUpdated(now: Date) -> Bool {
        guard defaults.bool(forKey: Self.updatedKey),
              let firstLaunch = defaults.object(forKey: Self.firstLaunchKey) as? Date else {
            return false
        }
        let elapsed = now.timeIntervalSince(firstLaunch)
        return elapsed >= 0 && elapsed < Self.justUpdatedWindow
    }
}
