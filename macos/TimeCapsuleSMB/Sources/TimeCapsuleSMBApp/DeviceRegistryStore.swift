import Foundation

enum DeviceRegistryState: String, CaseIterable, Equatable {
    case idle
    case loading
    case empty
    case loaded
    case saving
    case failed
}

enum DeviceRegistryError: Error, Equatable, LocalizedError {
    case applicationSupportUnavailable
    case corruptRegistry(String)
    case profileNotFound(DeviceProfile.ID)
    case duplicateProfile(field: String, value: String, conflictingProfileID: DeviceProfile.ID)
    case io(String)

    var errorDescription: String? {
        switch self {
        case .applicationSupportUnavailable:
            return "Application Support is unavailable."
        case .corruptRegistry(let message):
            return "Saved devices could not be read: \(message)"
        case .profileNotFound(let id):
            return "Saved device \(id) could not be found."
        case .duplicateProfile(let field, let value, let conflictingProfileID):
            return "Another saved device already uses \(field) \(value): \(conflictingProfileID)."
        case .io(let message):
            return message
        }
    }
}

@MainActor
final class DeviceRegistryStore: ObservableObject {
    @Published private(set) var state: DeviceRegistryState = .idle
    @Published private(set) var profiles: [DeviceProfile] = []
    @Published private(set) var error: DeviceRegistryError?

    let applicationSupportURL: URL
    let registryURL: URL
    let devicesDirectoryURL: URL

    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let now: () -> Date

    convenience init() {
        let appSupport = BundleLayout.applicationSupportDirectory() ?? FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/TimeCapsuleSMB", isDirectory: true)
        self.init(applicationSupportURL: appSupport)
    }

    init(
        applicationSupportURL: URL,
        fileManager: FileManager = .default,
        now: @escaping () -> Date = Date.init
    ) {
        self.applicationSupportURL = applicationSupportURL
        self.registryURL = applicationSupportURL.appendingPathComponent("devices.json")
        self.devicesDirectoryURL = applicationSupportURL.appendingPathComponent("Devices", isDirectory: true)
        self.fileManager = fileManager
        self.now = now

        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        self.encoder = encoder

        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        self.decoder = decoder
    }

    var isEmpty: Bool {
        profiles.isEmpty
    }

    func load() {
        state = .loading
        error = nil
        do {
            try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
            guard fileManager.fileExists(atPath: registryURL.path) else {
                profiles = []
                state = .empty
                return
            }
            let data = try Data(contentsOf: registryURL)
            profiles = try decoder.decode([DeviceProfile].self, from: data)
                .sorted { $0.updatedAt > $1.updatedAt }
            state = profiles.isEmpty ? .empty : .loaded
        } catch let decoding as DecodingError {
            profiles = []
            error = .corruptRegistry(String(describing: decoding))
            state = .failed
        } catch {
            profiles = []
            self.error = .io(error.localizedDescription)
            state = .failed
        }
    }

    @discardableResult
    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased()
    ) throws -> DeviceProfile {
        let existing = matchingProfile(host: configuredDevice.host, bonjourFullname: discoveredDevice?.fullname)
        var profile = DeviceProfile.make(
            id: preferredID,
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            applicationSupportURL: applicationSupportURL,
            existing: existing,
            date: now()
        )
        profile.passwordState = passwordState
        return try saveMergingDuplicates(profile)
    }

    @discardableResult
    private func saveMergingDuplicates(_ profile: DeviceProfile) throws -> DeviceProfile {
        state = .saving
        error = nil
        do {
            try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
            try fileManager.createDirectory(
                at: URL(fileURLWithPath: profile.configPath).deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            var updated = profiles.filter { !DeviceProfile.matches($0, profile) && $0.id != profile.id }
            updated.append(profile)
            profiles = updated.sorted { $0.updatedAt > $1.updatedAt }
            try persist()
            state = profiles.isEmpty ? .empty : .loaded
            return profile
        } catch {
            self.error = .io(error.localizedDescription)
            state = .failed
            throw error
        }
    }

    @discardableResult
    func updateProfile(_ profile: DeviceProfile) throws -> DeviceProfile {
        guard let index = profiles.firstIndex(where: { $0.id == profile.id }) else {
            let error = DeviceRegistryError.profileNotFound(profile.id)
            self.error = error
            throw error
        }
        if let conflict = duplicateConflict(for: profile, excluding: profile.id) {
            self.error = conflict
            throw conflict
        }
        state = .saving
        error = nil
        var updated = profile
        updated.updatedAt = now()
        do {
            try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
            try fileManager.createDirectory(
                at: URL(fileURLWithPath: updated.configPath).deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            profiles[index] = updated
            profiles = profiles.sorted { $0.updatedAt > $1.updatedAt }
            try persist()
            state = profiles.isEmpty ? .empty : .loaded
            return updated
        } catch {
            self.error = .io(error.localizedDescription)
            state = .failed
            throw error
        }
    }

    func delete(_ profile: DeviceProfile) throws {
        state = .saving
        error = nil
        do {
            profiles.removeAll { $0.id == profile.id }
            let configDirectory = URL(fileURLWithPath: profile.configPath).deletingLastPathComponent()
            if fileManager.fileExists(atPath: configDirectory.path) {
                try fileManager.removeItem(at: configDirectory)
            }
            try persist()
            state = profiles.isEmpty ? .empty : .loaded
        } catch {
            self.error = .io(error.localizedDescription)
            state = .failed
            throw error
        }
    }

    func updatePasswordState(_ state: DevicePasswordState, for profileID: DeviceProfile.ID) {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return
        }
        guard profiles[index].passwordState != state else {
            return
        }
        profiles[index].passwordState = state
        profiles[index].updatedAt = now()
        try? persist()
    }

    func updateCheckup(_ snapshot: DeviceCheckupSnapshot, for profileID: DeviceProfile.ID) {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return
        }
        profiles[index].lastCheckup = snapshot
        profiles[index].updatedAt = now()
        try? persist()
    }

    func updateDeploy(_ snapshot: DeviceDeploySnapshot, for profileID: DeviceProfile.ID) {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return
        }
        profiles[index].lastDeploy = snapshot
        profiles[index].updatedAt = now()
        try? persist()
    }

    func profile(id: DeviceProfile.ID?) -> DeviceProfile? {
        guard let id else {
            return nil
        }
        return profiles.first { $0.id == id }
    }

    func matchingProfile(host: String, bonjourFullname: String?) -> DeviceProfile? {
        let normalizedFullname = bonjourFullname?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        if let normalizedFullname, !normalizedFullname.isEmpty,
           let profile = profiles.first(where: { $0.bonjourFullname?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() == normalizedFullname }) {
            return profile
        }
        let normalizedHost = DeviceProfile.normalizedHost(host)
        guard !normalizedHost.isEmpty else {
            return nil
        }
        return profiles.first { $0.normalizedHost == normalizedHost }
    }

    private func duplicateConflict(for profile: DeviceProfile, excluding profileID: DeviceProfile.ID) -> DeviceRegistryError? {
        if let normalizedFullname = normalizedBonjourFullname(profile.bonjourFullname),
           let conflicting = profiles.first(where: {
               $0.id != profileID && normalizedBonjourFullname($0.bonjourFullname) == normalizedFullname
           }) {
            return .duplicateProfile(
                field: "Bonjour fullname",
                value: normalizedFullname,
                conflictingProfileID: conflicting.id
            )
        }

        let normalizedHost = profile.normalizedHost
        if !normalizedHost.isEmpty,
           let conflicting = profiles.first(where: { $0.id != profileID && $0.normalizedHost == normalizedHost }) {
            return .duplicateProfile(
                field: "host",
                value: normalizedHost,
                conflictingProfileID: conflicting.id
            )
        }
        return nil
    }

    private func normalizedBonjourFullname(_ value: String?) -> String? {
        guard let normalized = value?.trimmingCharacters(in: .whitespacesAndNewlines).lowercased(),
              !normalized.isEmpty else {
            return nil
        }
        return normalized
    }

    private func persist() throws {
        try fileManager.createDirectory(at: applicationSupportURL, withIntermediateDirectories: true)
        let data = try encoder.encode(profiles)
        try data.write(to: registryURL, options: [.atomic])
    }
}
