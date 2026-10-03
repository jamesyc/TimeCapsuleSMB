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
    case identityUnverified
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
        case .identityUnverified:
            return L10n.string("discovery.identity_unverified")
        case .io(let message):
            return message
        }
    }
}

enum DeviceProfileMatch: Equatable {
    case none
    case unique(DeviceProfile)
    case conflict([DeviceProfile.ID])

    var profile: DeviceProfile? {
        if case .unique(let profile) = self { return profile }
        return nil
    }

    static func resolve(_ identity: DeviceNetworkIdentity, in profiles: [DeviceProfile]) -> DeviceProfileMatch {
        let hardware = identity.airportMAC.map { mac in profiles.filter { $0.network.airportMAC == mac } } ?? []
        let matches = hardware.isEmpty ? profiles.filter { $0.network.matches(identity) } : hardware
        switch matches.count {
        case 0: return .none
        case 1: return .unique(matches[0])
        default: return .conflict(matches.map(\.id).sorted())
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
    private var operationUpdateTask: Task<Void, Never>?

    // Enqueue when an event arrives: independent Tasks can reach the repository
    // out of order despite actor isolation. Share ordering across sessions too.
    func enqueueOperationUpdate(_ update: @escaping @MainActor () async -> Void) {
        let previous = operationUpdateTask
        operationUpdateTask = Task {
            await previous?.value
            await update()
        }
    }

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

    func makeConfiguredDeviceProfile(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        passwordState: DevicePasswordState,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased(),
        existingProfileID: DeviceProfile.ID? = nil
    ) async throws -> DeviceProfile {
        try await repository.makeConfiguredDeviceProfile(
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            passwordState: passwordState,
            preferredID: preferredID,
            existingProfileID: existingProfileID
        )
    }

    @discardableResult
    func saveProfile(_ profile: DeviceProfile) async throws -> DeviceProfile {
        state = .saving
        error = nil
        do {
            let result = try await repository.saveProfile(profile)
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
        airportMAC: String? = nil,
        for profileID: DeviceProfile.ID
    ) async {
        await applyBackgroundMutation {
            try await repository.updateCheckup(snapshot, runtimeState: runtimeState, airportMAC: airportMAC, for: profileID)
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
        rsyncEnabled: Bool? = nil,
        for profileID: DeviceProfile.ID
    ) async {
        await applyBackgroundMutation {
            try await repository.updateInstallOperationState(
                deployState: deployState,
                runtimeState: runtimeState,
                rsyncEnabled: rsyncEnabled,
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

    func matchingProfile(host: String) -> DeviceProfile? {
        DeviceProfileMatch.resolve(DeviceNetworkIdentity(configuredSSHTarget: host), in: profiles).profile
    }

    var conflictingProfile: DeviceProfile? {
        guard case .duplicateProfile(_, _, let id) = error else { return nil }
        return profile(id: id)
    }

    var identityErrorMessage: String? {
        switch error {
        case .duplicateProfile:
            if let profile = conflictingProfile {
                return L10n.format("discovery.identity_owned", profile.title)
            }
            return L10n.string("discovery.identity_conflict")
        case .identityUnverified:
            return L10n.string("discovery.identity_unverified")
        default:
            return nil
        }
    }

    func profileMatch(for device: DiscoveredDevice) -> DeviceProfileMatch {
        DeviceProfileMatch.resolve(device.observedIdentity, in: profiles)
    }

    func matchingProfile(for device: DiscoveredDevice) -> DeviceProfile? {
        profileMatch(for: device).profile
    }

    func suggestedProfile(for device: DiscoveredDevice) -> DeviceProfile? {
        guard case .none = profileMatch(for: device) else { return nil }
        let identity = device.observedIdentity
        let suggestions = profiles.filter { $0.network.sharesName(with: identity) }
        return suggestions.count == 1 ? suggestions[0] : nil
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
            case .profileNotFound, .duplicateProfile, .identityUnverified:
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
    ) throws -> DeviceProfile {
        let existing = existingProfileID.flatMap { id in profiles.first { $0.id == id } }
        if let existingProfileID, existing == nil { throw DeviceRegistryError.profileNotFound(existingProfileID) }
        try validateConfirmedIdentity(configuredDevice.airportMAC, for: existing)
        if let advertised = discoveredDevice?.airportMAC, let confirmed = configuredDevice.airportMAC,
           advertised != confirmed {
            throw DeviceRegistryError.identityUnverified
        }
        var profile = DeviceProfile.make(
            id: preferredID,
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            applicationSupportURL: applicationSupportURL,
            existing: existing,
            date: now()
        )
        profile.passwordState = passwordState
        try validateProfileIdentity(profile)
        return profile
    }

    func saveProfile(_ profile: DeviceProfile) throws -> DeviceRegistryMutationResult {
        let storedProfile = profileWithStorageFields(profile)
        try fileManager.createDirectory(at: devicesDirectoryURL, withIntermediateDirectories: true)
        try fileManager.createDirectory(
            at: storedProfile.configURL.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        // Profile construction chooses one verified identity. Never delete every weak match.
        try validateProfileIdentity(storedProfile)
        var updated = profiles.filter { $0.id != storedProfile.id }
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
        try validateProfileIdentity(storedProfile)

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
        let stagedDirectory = try stageConfigDirectoryDelete(configDirectory, profileID: storedProfile.id)
        do {
            try persist(updatedProfiles)
        } catch {
            restoreStagedConfigDirectory(stagedDirectory, to: configDirectory)
            throw error
        }
        profiles = updatedProfiles
        removeStagedConfigDirectory(stagedDirectory)
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
        airportMAC: String? = nil,
        for profileID: DeviceProfile.ID
    ) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        let confirmed = DeviceNetworkIdentity.normalizedAirportMAC(airportMAC)
        var updatedProfiles = profiles
        if let confirmed { updatedProfiles[index].network.airportMAC = confirmed }
        // Checkups learn identity through the same actor-owned validation as saves.
        // Reject the whole mutation before writing snapshots or persistent state.
        try validateProfileIdentity(updatedProfiles[index])
        if runtimeState != nil {
            // Finish an orphaned attempt too, so reload cannot undo this fresh
            // SSH-backed observation. Dashboard updates are enqueued in order.
            updatedProfiles[index] = profileWithInterruptedRuntimeState(updatedProfiles[index])
        }
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
        guard !isStaleDeployProgress(snapshot, replacing: profiles[index].lastDeployState) else {
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
        rsyncEnabled: Bool? = nil,
        for profileID: DeviceProfile.ID
    ) throws -> [DeviceProfile]? {
        guard let index = profiles.firstIndex(where: { $0.id == profileID }) else {
            return nil
        }
        guard !isStaleDeployProgress(deployState, replacing: profiles[index].lastDeployState) else {
            return nil
        }
        var updatedProfiles = profiles
        updatedProfiles[index].lastDeployState = deployState
        updatedProfiles[index].runtimeState = runtimeState
        if let rsyncEnabled {
            updatedProfiles[index].settings.rsyncEnabled = rsyncEnabled
        }
        updatedProfiles[index].updatedAt = now()
        try persist(updatedProfiles)
        profiles = updatedProfiles
        return updatedProfiles
    }

    private func isStaleDeployProgress(
        _ incoming: DeviceDeployStateSnapshot,
        replacing current: DeviceDeployStateSnapshot?
    ) -> Bool {
        guard let operationID = incoming.operationID, let current else {
            return false
        }
        // Async stage/start writes must not undo this operation's result.
        return current.operationID == operationID &&
            !current.status.isInProgress && incoming.status.isInProgress
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

    private func validateConfirmedIdentity(_ confirmed: String?, for existing: DeviceProfile?) throws {
        if let expected = existing?.network.airportMAC, confirmed != expected {
            throw DeviceRegistryError.identityUnverified
        }
    }

    private func validateProfileIdentity(_ profile: DeviceProfile) throws {
        try validateConfirmedIdentity(profile.network.airportMAC, for: profiles.first { $0.id == profile.id })
        if let conflict = duplicateConflict(for: profile, excluding: profile.id) { throw conflict }
    }

    private func duplicateConflict(for profile: DeviceProfile, excluding profileID: DeviceProfile.ID) -> DeviceRegistryError? {
        for existing in profiles where existing.id != profileID {
            let left = profile.network, right = existing.network
            if let mac = left.airportMAC, mac == right.airportMAC {
                return .duplicateProfile(field: "AirPort MAC", value: mac, conflictingProfileID: existing.id)
            }
            if !left.normalizedConfiguredHost.isEmpty && left.normalizedConfiguredHost == right.normalizedConfiguredHost {
                return .duplicateProfile(field: "host", value: left.configuredHost, conflictingProfileID: existing.id)
            }
            // Saved observations are informational. Only configured endpoints are reserved.
        }
        return nil
    }

    private func persist(_ profiles: [DeviceProfile]) throws {
        try fileManager.createDirectory(at: applicationSupportURL, withIntermediateDirectories: true)
        let data = try encoder.encode(profiles.map(profileWithStorageFields))
        try data.write(to: registryURL, options: [.atomic])
    }

    private func stageConfigDirectoryDelete(_ configDirectory: URL, profileID: DeviceProfile.ID) throws -> URL? {
        guard fileManager.fileExists(atPath: configDirectory.path) else {
            return nil
        }
        let configDirectoryPath = configDirectory.standardizedFileURL.path
        let devicesDirectoryPath = devicesDirectoryURL.standardizedFileURL.path
        guard configDirectoryPath.hasPrefix(devicesDirectoryPath + "/") else {
            throw DeviceRegistryError.io("Refusing to delete profile artifacts outside the devices directory.")
        }

        let stagingDirectory = devicesDirectoryURL.appendingPathComponent(".Staging", isDirectory: true)
        try fileManager.createDirectory(at: stagingDirectory, withIntermediateDirectories: true)
        let stagedDirectory = stagingDirectory.appendingPathComponent("\(profileID)-delete-\(UUID().uuidString.lowercased())", isDirectory: true)
        try fileManager.moveItem(at: configDirectory, to: stagedDirectory)
        return stagedDirectory
    }

    private func restoreStagedConfigDirectory(_ stagedDirectory: URL?, to configDirectory: URL) {
        guard let stagedDirectory, fileManager.fileExists(atPath: stagedDirectory.path) else {
            return
        }
        try? fileManager.createDirectory(at: configDirectory.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fileManager.moveItem(at: stagedDirectory, to: configDirectory)
    }

    private func removeStagedConfigDirectory(_ stagedDirectory: URL?) {
        guard let stagedDirectory, fileManager.fileExists(atPath: stagedDirectory.path) else {
            return
        }
        try? fileManager.removeItem(at: stagedDirectory)
    }

    private func sorted(_ profiles: [DeviceProfile]) -> [DeviceProfile] {
        profiles.sorted { $0.updatedAt > $1.updatedAt }
    }

    private func profileWithStorageFields(_ profile: DeviceProfile) -> DeviceProfile {
        var updated = profile
        updated.network.airportMAC = DeviceNetworkIdentity.normalizedAirportMAC(updated.network.airportMAC)
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
        if var deployState = profile.lastDeployState, deployState.status.isInProgress {
            deployState.updatedAt = interruptedAt
            deployState.finishedAt = interruptedAt
            deployState.status = .interrupted
            deployState.summary = ""
            deployState.summaryRef = nil
            deployState.errorCode = "operation_interrupted"
            deployState.errorMessage = nil
            deployState.diagnosticText = nil
            updated.lastDeployState = deployState
        }
        if var runtimeState = profile.runtimeState, runtimeState.state == .installing {
            runtimeState.state = .installInterrupted
            runtimeState.source = .appRecovery
            runtimeState.summary = ""
            runtimeState.summaryRef = nil
            runtimeState.errorCode = "operation_interrupted"
            runtimeState.errorMessage = nil
            updated.runtimeState = runtimeState
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
