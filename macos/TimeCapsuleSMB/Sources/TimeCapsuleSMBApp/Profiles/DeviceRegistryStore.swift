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

    private let repository: DeviceRegistryRepository

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
        self.repository = DeviceRegistryRepository(
            applicationSupportURL: applicationSupportURL,
            fileManager: fileManager,
            now: now
        )
    }

    var isEmpty: Bool {
        profiles.isEmpty
    }

    func load() async {
        state = .loading
        error = nil
        do {
            profiles = try await repository.load()
            state = profiles.isEmpty ? .empty : .loaded
        } catch {
            fail(error, clearProfiles: true)
        }
    }

    @discardableResult
    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased()
    ) async throws -> DeviceProfile {
        state = .saving
        error = nil
        do {
            let result = try await repository.saveConfiguredDevice(
                configuredDevice: configuredDevice,
                discoveredDevice: discoveredDevice,
                passwordState: passwordState,
                preferredID: preferredID
            )
            await refreshProfilesFromRepository()
            return result.profile
        } catch {
            fail(error, clearProfiles: false)
            throw error
        }
    }

    func makeConfiguredDeviceProfile(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased(),
        existingProfileID: DeviceProfile.ID? = nil
    ) async -> DeviceProfile {
        await repository.makeConfiguredDeviceProfile(
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            passwordState: passwordState,
            preferredID: preferredID,
            existingProfileID: existingProfileID
        )
    }

    @discardableResult
    func saveProfileMergingDuplicates(_ profile: DeviceProfile) async throws -> DeviceProfile {
        state = .saving
        error = nil
        do {
            let result = try await repository.saveProfileMergingDuplicates(profile)
            await refreshProfilesFromRepository()
            return result.profile
        } catch {
            fail(error, clearProfiles: false)
            throw error
        }
    }

    func discardArtifacts(for profile: DeviceProfile) async {
        await repository.discardArtifacts(for: profile)
    }

    @discardableResult
    func updateProfile(_ profile: DeviceProfile) async throws -> DeviceProfile {
        state = .saving
        error = nil
        do {
            let result = try await repository.updateProfile(profile)
            await refreshProfilesFromRepository()
            return result.profile
        } catch {
            fail(error, clearProfiles: false)
            throw error
        }
    }

    func delete(_ profile: DeviceProfile) async throws {
        state = .saving
        error = nil
        do {
            _ = try await repository.delete(profile)
            await refreshProfilesFromRepository()
        } catch {
            fail(error, clearProfiles: false)
            throw error
        }
    }

    func updatePasswordState(_ state: DevicePasswordState, for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.updatePasswordState(state, for: profileID)
        }
    }

    func updateCheckup(_ snapshot: DeviceCheckupSnapshot, for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.updateCheckup(snapshot, for: profileID)
        }
    }

    func updateCheckup(
        _ snapshot: DeviceCheckupSnapshot,
        runtimeState: DeviceRuntimeStateSnapshot?,
        for profileID: DeviceProfile.ID
    ) async {
        await applyBackgroundMutation {
            try await repository.updateCheckup(snapshot, runtimeState: runtimeState, for: profileID)
        }
    }

    func clearCheckup(for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.clearCheckup(for: profileID)
        }
    }

    func updateDeployState(_ snapshot: DeviceDeployStateSnapshot, for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.updateDeployState(snapshot, for: profileID)
        }
    }

    func updateInstallOperationState(
        deployState: DeviceDeployStateSnapshot,
        runtimeState: DeviceRuntimeStateSnapshot,
        for profileID: DeviceProfile.ID
    ) async {
        await applyBackgroundMutation {
            try await repository.updateInstallOperationState(
                deployState: deployState,
                runtimeState: runtimeState,
                for: profileID
            )
        }
    }

    func updateRuntimeState(_ snapshot: DeviceRuntimeStateSnapshot, for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.updateRuntimeState(snapshot, for: profileID)
        }
    }

    func clearInstallState(for profileID: DeviceProfile.ID) async {
        await applyBackgroundMutation {
            try await repository.clearInstallState(for: profileID)
        }
    }

    func profile(id: DeviceProfile.ID?) -> DeviceProfile? {
        guard let id else {
            return nil
        }
        return profiles.first { $0.id == id }
    }

    func matchingProfile(host: String, bonjourFullname: String?) -> DeviceProfile? {
        let identity = DeviceNetworkIdentity(configuredSSHTarget: host, bonjourFullname: bonjourFullname)
        return profiles.first { $0.network.matches(identity) }
    }

    func matchingProfile(for device: DiscoveredDevice) -> DeviceProfile? {
        let identity = DeviceNetworkIdentity(
            configuredSSHTarget: device.connectionTarget,
            hostname: device.hostname,
            bonjourName: device.name,
            bonjourFullname: device.fullname,
            addresses: device.networkAddresses
        )
        return profiles.first { $0.network.matches(identity) }
    }

    private func applyBackgroundMutation(_ mutate: () async throws -> [DeviceProfile]?) async {
        do {
            guard try await mutate() != nil else {
                return
            }
            await refreshProfilesFromRepository()
        } catch {
            fail(error, clearProfiles: false)
        }
    }

    private func refreshProfilesFromRepository() async {
        profiles = await repository.profilesSnapshot()
        state = profiles.isEmpty ? .empty : .loaded
    }

    private func fail(_ error: Error, clearProfiles: Bool) {
        if clearProfiles {
            profiles = []
        }
        if let registryError = error as? DeviceRegistryError {
            self.error = registryError
            switch registryError {
            case .profileNotFound, .duplicateProfile:
                state = profiles.isEmpty ? .empty : .loaded
                return
            case .applicationSupportUnavailable, .corruptRegistry, .io:
                break
            }
        } else {
            self.error = .io(error.localizedDescription)
        }
        state = .failed
    }
}

private struct DeviceRegistryMutationResult: Sendable {
    let profile: DeviceProfile
}

private actor DeviceRegistryRepository {
    private let applicationSupportURL: URL
    private let registryURL: URL
    private let devicesDirectoryURL: URL
    private let fileManager: FileManager
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let now: () -> Date
    private var profiles: [DeviceProfile] = []

    init(
        applicationSupportURL: URL,
        fileManager: FileManager,
        now: @escaping () -> Date
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

    func load() throws -> [DeviceProfile] {
        do {
            try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
            guard fileManager.fileExists(atPath: registryURL.path) else {
                profiles = []
                return profiles
            }
            let data = try Data(contentsOf: registryURL)
            let loadedProfiles = try decoder.decode([DeviceProfile].self, from: data)
                .map(profileWithStorageFields)
                .sorted { $0.updatedAt > $1.updatedAt }
            profiles = loadedProfiles
                .map(profileWithInterruptedRuntimeState)
                .sorted { $0.updatedAt > $1.updatedAt }
            if profiles != loadedProfiles {
                try persist(profiles)
            }
            return profiles
        } catch let decoding as DecodingError {
            profiles = []
            throw DeviceRegistryError.corruptRegistry(String(describing: decoding))
        } catch let registryError as DeviceRegistryError {
            profiles = []
            throw registryError
        } catch {
            profiles = []
            throw DeviceRegistryError.io(error.localizedDescription)
        }
    }

    func profilesSnapshot() -> [DeviceProfile] {
        profiles
    }

    func makeConfiguredDeviceProfile(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID,
        existingProfileID: DeviceProfile.ID? = nil
    ) -> DeviceProfile {
        let existing = existingProfileID.flatMap { id in profiles.first { $0.id == id } }
            ?? matchingProfile(configuredHost: configuredDevice.host, discoveredDevice: discoveredDevice)
        var profile = DeviceProfile.make(
            id: preferredID,
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            applicationSupportURL: applicationSupportURL,
            existing: existing,
            date: now()
        )
        profile.passwordState = passwordState
        return profile
    }

    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID
    ) throws -> DeviceRegistryMutationResult {
        let profile = makeConfiguredDeviceProfile(
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            passwordState: passwordState,
            preferredID: preferredID,
            existingProfileID: nil
        )
        return try saveProfileMergingDuplicates(profile)
    }

    func saveProfileMergingDuplicates(_ profile: DeviceProfile) throws -> DeviceRegistryMutationResult {
        let storedProfile = profileWithStorageFields(profile)
        try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
        try fileManager.createDirectory(
            at: storedProfile.configURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        var updated = profiles.filter { !DeviceProfile.matches($0, storedProfile) && $0.id != storedProfile.id }
        updated.append(storedProfile)
        updated = sorted(updated)
        try persist(updated)
        profiles = updated
        return DeviceRegistryMutationResult(profile: storedProfile)
    }

    func discardArtifacts(for profile: DeviceProfile) {
        let configDirectory = profileWithStorageFields(profile).configURL.deletingLastPathComponent()
        let configDirectoryPath = configDirectory.standardizedFileURL.path
        let devicesDirectoryPath = devicesDirectoryURL.standardizedFileURL.path
        guard configDirectoryPath.hasPrefix(devicesDirectoryPath + "/") else {
            return
        }
        try? fileManager.removeItem(at: configDirectory)
    }

    func updateProfile(_ profile: DeviceProfile) throws -> DeviceRegistryMutationResult {
        let storedProfile = profileWithStorageFields(profile)
        guard let index = profiles.firstIndex(where: { $0.id == storedProfile.id }) else {
            throw DeviceRegistryError.profileNotFound(profile.id)
        }
        if let conflict = duplicateConflict(for: storedProfile, excluding: storedProfile.id) {
            throw conflict
        }

        var updated = storedProfile
        updated.updatedAt = now()
        try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
        try fileManager.createDirectory(
            at: updated.configURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        var updatedProfiles = profiles
        updatedProfiles[index] = updated
        updatedProfiles = sorted(updatedProfiles)
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return DeviceRegistryMutationResult(profile: updated)
    }

    func delete(_ profile: DeviceProfile) throws -> [DeviceProfile] {
        let storedProfile = profileWithStorageFields(profile)
        let updatedProfiles = profiles.filter { $0.id != storedProfile.id }
        let configDirectory = storedProfile.configURL.deletingLastPathComponent()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        if fileManager.fileExists(atPath: configDirectory.path) {
            try fileManager.removeItem(at: configDirectory)
        }
        return updatedProfiles
    }

    func updatePasswordState(_ state: DevicePasswordState, for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        guard profiles[index].passwordState != state else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].passwordState = state
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func updateCheckup(_ snapshot: DeviceCheckupSnapshot, for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        try updateCheckup(snapshot, runtimeState: nil, for: profileID)
    }

    func updateCheckup(
        _ snapshot: DeviceCheckupSnapshot,
        runtimeState: DeviceRuntimeStateSnapshot?,
        for profileID: DeviceProfile.ID
    ) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastCheckup = snapshot
        if let runtimeState {
            updatedProfiles[index].runtimeState = runtimeState
        }
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func clearCheckup(for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        guard profiles[index].lastCheckup != nil else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastCheckup = nil
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func updateDeployState(_ snapshot: DeviceDeployStateSnapshot, for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastDeployState = snapshot
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func updateInstallOperationState(
        deployState: DeviceDeployStateSnapshot,
        runtimeState: DeviceRuntimeStateSnapshot,
        for profileID: DeviceProfile.ID
    ) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastDeployState = deployState
        updatedProfiles[index].runtimeState = runtimeState
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func updateRuntimeState(_ snapshot: DeviceRuntimeStateSnapshot, for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].runtimeState = snapshot
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    func clearInstallState(for profileID: DeviceProfile.ID) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        guard profiles[index].lastDeployState != nil || profiles[index].runtimeState != nil || profiles[index].lastCheckup != nil else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastDeployState = nil
        updatedProfiles[index].runtimeState = nil
        updatedProfiles[index].lastCheckup = nil
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    private func matchingProfile(host: String, bonjourFullname: String?) -> DeviceProfile? {
        let identity = DeviceNetworkIdentity(configuredSSHTarget: host, bonjourFullname: bonjourFullname)
        return profiles.first { $0.network.matches(identity) }
    }

    private func matchingProfile(configuredHost: String, discoveredDevice: DiscoveredDevice?) -> DeviceProfile? {
        let identity = DeviceNetworkIdentity.make(configuredSSHTarget: configuredHost, discoveredDevice: discoveredDevice)
        return profiles.first { $0.network.matches(identity) }
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
                value: DeviceEndpointPolicy.hostComponent(profile.host) ?? normalizedHost,
                conflictingProfileID: conflicting.id
            )
        }
        let normalizedHostname = profile.network.normalizedHostname
        if !normalizedHostname.isEmpty,
           let conflicting = profiles.first(where: {
               $0.id != profileID && $0.network.normalizedHostname == normalizedHostname
           }) {
            return .duplicateProfile(
                field: "hostname",
                value: normalizedHostname,
                conflictingProfileID: conflicting.id
            )
        }
        if let conflict = addressConflict(for: profile, excluding: profileID) {
            return conflict
        }
        return nil
    }

    private func addressConflict(for profile: DeviceProfile, excluding profileID: DeviceProfile.ID) -> DeviceRegistryError? {
        let keys = profile.network.addressKeys
        guard !keys.isEmpty else {
            return nil
        }
        for existing in profiles where existing.id != profileID {
            let overlap = keys.intersection(existing.network.addressKeys)
            guard let key = overlap.first else {
                continue
            }
            let value = profile.network.addresses.first { $0.identityKey == key }?.value ?? key
            return .duplicateProfile(field: "address", value: value, conflictingProfileID: existing.id)
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

    private func persist(_ profiles: [DeviceProfile]) throws {
        try fileManager.createDirectory(at: applicationSupportURL, withIntermediateDirectories: true)
        let data = try encoder.encode(profiles.map(profileWithStorageFields))
        try data.write(to: registryURL, options: [.atomic])
    }

    private func sorted(_ profiles: [DeviceProfile]) -> [DeviceProfile] {
        profiles.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func profileWithStorageFields(_ profile: DeviceProfile) -> DeviceProfile {
        var updated = profile
        updated.configPath = DeviceProfile.configURL(for: profile.id, applicationSupportURL: applicationSupportURL).path
        updated.keychainAccount = profile.id
        return updated
    }

    private func profileWithInterruptedRuntimeState(_ profile: DeviceProfile) -> DeviceProfile {
        guard profile.lastDeployState?.status.isInProgress == true || profile.runtimeState?.state == .installing else {
            return profile
        }
        let interruptedAt = now()
        var updated = profile
        if let deployState = profile.lastDeployState, deployState.status.isInProgress {
            updated.lastDeployState = DeviceDeployStateSnapshot(
                operationID: deployState.operationID,
                startedAt: deployState.startedAt,
                updatedAt: interruptedAt,
                finishedAt: interruptedAt,
                status: .interrupted,
                stage: deployState.stage,
                payloadFamily: deployState.payloadFamily,
                rebootRequested: deployState.rebootRequested,
                verified: deployState.verified,
                summary: "",
                errorCode: "operation_interrupted",
                errorMessage: nil,
                recovery: deployState.recovery
            )
        }
        if let runtimeState = profile.runtimeState, runtimeState.state == .installing {
            updated.runtimeState = DeviceRuntimeStateSnapshot(
                state: .installInterrupted,
                source: .appRecovery,
                stage: runtimeState.stage,
                payloadFamily: runtimeState.payloadFamily,
                verified: runtimeState.verified,
                summary: "",
                errorCode: "operation_interrupted",
                errorMessage: nil,
                recovery: runtimeState.recovery
            )
        } else if let deployState = profile.lastDeployState, deployState.status.isInProgress {
            updated.runtimeState = DeviceRuntimeStateSnapshot(
                state: .installInterrupted,
                source: .appRecovery,
                stage: deployState.stage,
                payloadFamily: deployState.payloadFamily,
                verified: deployState.verified,
                summary: "",
                errorCode: "operation_interrupted",
                errorMessage: nil,
                recovery: deployState.recovery
            )
        }
        updated.updatedAt = interruptedAt
        return updated
    }
}
