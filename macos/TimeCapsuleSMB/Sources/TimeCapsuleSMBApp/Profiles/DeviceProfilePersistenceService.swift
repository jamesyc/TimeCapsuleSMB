import Foundation

enum CredentialResolution: Equatable {
    case available(String)
    case missing
    case invalid
    case unavailable(String)

    var password: String? {
        guard case .available(let password) = self else {
            return nil
        }
        return password
    }
}

struct ConfigureProfileDraft: Equatable {
    let profileID: DeviceProfile.ID
    let existingProfileID: DeviceProfile.ID?
    let discoveredDevice: DiscoveredDevice?
    let targetHost: String
    let settings: DeviceProfileSettings
    let context: DeviceRuntimeContext
}

struct ConfiguredDeviceProfileOverrides: Equatable {
    var displayName: String?
    var settings: DeviceProfileSettings?

    static let empty = ConfiguredDeviceProfileOverrides()
}

@MainActor
final class DeviceProfilePersistenceService {
    private enum PasswordRollback {
        case delete
        case restore(String)
    }

    private let registry: DeviceRegistryStore
    private let passwordStore: PasswordStore
    private let artifacts: DeviceProfileArtifacts

    init(
        registry: DeviceRegistryStore,
        passwordStore: PasswordStore,
        fileManager: FileManager = .default
    ) {
        self.registry = registry
        self.passwordStore = passwordStore
        self.artifacts = DeviceProfileArtifacts(applicationSupportURL: registry.applicationSupportURL, fileManager: fileManager)
    }

    func prepareConfigureTarget(
        targetHost: String,
        discoveredDevice: DiscoveredDevice?,
        existingProfile: DeviceProfile?,
        preferredID: DeviceProfile.ID,
        settings: DeviceProfileSettings
    ) throws -> ConfigureProfileDraft {
        let profileID = existingProfile?.id ?? preferredID
        let stagedConfigURL = try artifacts.stageConfig(for: profileID, sourceProfile: existingProfile)
        return ConfigureProfileDraft(
            profileID: profileID,
            existingProfileID: existingProfile?.id,
            discoveredDevice: discoveredDevice,
            targetHost: targetHost,
            settings: settings,
            context: DeviceRuntimeContext(profileID: profileID, configURL: stagedConfigURL)
        )
    }

    func discardConfigureDraft(_ draft: ConfigureProfileDraft?) {
        guard let draft else {
            return
        }
        artifacts.discardStagedConfig(at: draft.context.configURL)
    }

    @discardableResult
    func commitConfiguredProfile(
        configuredDevice: ConfiguredDeviceState,
        draft: ConfigureProfileDraft,
        password: String,
        overrides: ConfiguredDeviceProfileOverrides = .empty
    ) async throws -> DeviceProfile {
        var profile = await registry.makeConfiguredDeviceProfile(
            configuredDevice: configuredDevice,
            discoveredDevice: draft.discoveredDevice,
            passwordState: .available,
            preferredID: draft.profileID,
            existingProfileID: draft.existingProfileID
        )
        if let displayName = overrides.displayName {
            profile.displayName = displayName
        }
        if let settings = overrides.settings {
            profile.settings = settings
        }

        let rollback = try passwordRollback(for: profile.keychainAccount)
        do {
            try passwordStore.save(password, for: profile.keychainAccount)
        } catch {
            artifacts.discardStagedConfig(at: draft.context.configURL)
            throw error
        }

        let artifactRollback: DeviceProfileArtifacts.CommitRollback
        do {
            artifactRollback = try artifacts.commitStagedConfig(at: draft.context.configURL, to: profile.configURL)
        } catch {
            rollbackPassword(rollback, account: profile.keychainAccount)
            artifacts.discardStagedConfig(at: draft.context.configURL)
            throw error
        }

        do {
            let saved = try await registry.saveProfileMergingDuplicates(profile)
            artifactRollback.discardBackup()
            return saved
        } catch {
            artifactRollback.rollback()
            rollbackPassword(rollback, account: profile.keychainAccount)
            throw error
        }
    }

    @discardableResult
    func saveProfileEdits(
        profile: DeviceProfile,
        fields: DeviceProfileEditableFields,
        replacementPassword: String? = nil
    ) async throws -> DeviceProfile {
        var updated = profile
        updated.displayName = fields.displayName
        updated.settings = fields.settings

        let rollback: PasswordRollback?
        if let replacementPassword {
            rollback = try passwordRollback(for: profile.keychainAccount)
            try passwordStore.save(replacementPassword, for: profile.keychainAccount)
            updated.passwordState = .available
        } else {
            rollback = nil
        }

        do {
            return try await registry.updateProfile(updated)
        } catch {
            if let rollback {
                rollbackPassword(rollback, account: profile.keychainAccount)
            }
            throw error
        }
    }

    func forget(_ profile: DeviceProfile) async throws {
        let rollback = try passwordRollback(for: profile.keychainAccount)
        try passwordStore.deletePassword(for: profile.keychainAccount)
        do {
            try await registry.delete(profile)
        } catch {
            rollbackPassword(rollback, account: profile.keychainAccount)
            throw error
        }
    }

    func credential(for profile: DeviceProfile) -> CredentialResolution {
        if profile.passwordState == .invalid {
            return .invalid
        }
        do {
            return .available(try passwordStore.password(for: profile.keychainAccount))
        } catch PasswordStoreError.missing {
            Task { await registry.updatePasswordState(.missing, for: profile.id) }
            return .missing
        } catch PasswordStoreError.unavailable(let message) {
            Task { await registry.updatePasswordState(.keychainUnavailable, for: profile.id) }
            return .unavailable(message)
        } catch {
            Task { await registry.updatePasswordState(.keychainUnavailable, for: profile.id) }
            return .unavailable(error.localizedDescription)
        }
    }

    func refreshCredentialStates() async {
        for profile in registry.profiles {
            await registry.updatePasswordState(effectivePasswordState(for: profile), for: profile.id)
        }
    }

    func markCredentialInvalid(profileID: DeviceProfile.ID) async {
        await registry.updatePasswordState(.invalid, for: profileID)
    }

    private func effectivePasswordState(for profile: DeviceProfile) -> DevicePasswordState {
        if profile.passwordState == .invalid {
            return .invalid
        }
        switch passwordStore.credentialAvailability(for: profile.keychainAccount) {
        case .available:
            return .available
        case .missing:
            return .missing
        case .unavailable:
            return .keychainUnavailable
        }
    }

    private func passwordRollback(for account: String) throws -> PasswordRollback {
        do {
            return .restore(try passwordStore.password(for: account))
        } catch PasswordStoreError.missing {
            return .delete
        } catch {
            throw error
        }
    }

    private func rollbackPassword(_ rollback: PasswordRollback, account: String) {
        switch rollback {
        case .delete:
            try? passwordStore.deletePassword(for: account)
        case .restore(let password):
            try? passwordStore.save(password, for: account)
        }
    }
}

private struct DeviceProfileArtifacts {
    struct CommitRollback {
        fileprivate let fileManager: FileManager
        fileprivate let finalURL: URL
        fileprivate let backupURL: URL?

        func rollback() {
            if fileManager.fileExists(atPath: finalURL.path) {
                try? fileManager.removeItem(at: finalURL)
            }
            if let backupURL, fileManager.fileExists(atPath: backupURL.path) {
                try? fileManager.createDirectory(at: finalURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                try? fileManager.moveItem(at: backupURL, to: finalURL)
            }
        }

        func discardBackup() {
            guard let backupURL, fileManager.fileExists(atPath: backupURL.path) else {
                return
            }
            try? fileManager.removeItem(at: backupURL)
        }
    }

    private let applicationSupportURL: URL
    private let fileManager: FileManager

    init(applicationSupportURL: URL, fileManager: FileManager) {
        self.applicationSupportURL = applicationSupportURL
        self.fileManager = fileManager
    }

    func stageConfig(for profileID: DeviceProfile.ID, sourceProfile: DeviceProfile?) throws -> URL {
        let stagingDirectory = applicationSupportURL
            .appendingPathComponent("Devices", isDirectory: true)
            .appendingPathComponent(".Staging", isDirectory: true)
        try fileManager.createDirectory(at: stagingDirectory, withIntermediateDirectories: true)
        let stagedURL = stagingDirectory.appendingPathComponent("\(profileID)-\(UUID().uuidString.lowercased()).env")
        if let sourceProfile {
            let sourceURL = sourceProfile.configURL
            if fileManager.fileExists(atPath: sourceURL.path) {
                try fileManager.copyItem(at: sourceURL, to: stagedURL)
            }
        }
        return stagedURL
    }

    func commitStagedConfig(at stagedURL: URL, to finalURL: URL) throws -> CommitRollback {
        guard fileManager.fileExists(atPath: stagedURL.path) else {
            throw DeviceRegistryError.io("Configured device artifact was not written.")
        }

        let finalDirectory = finalURL.deletingLastPathComponent()
        try fileManager.createDirectory(at: finalDirectory, withIntermediateDirectories: true)

        let backupURL: URL?
        if fileManager.fileExists(atPath: finalURL.path) {
            let backupDirectory = applicationSupportURL
                .appendingPathComponent("Devices", isDirectory: true)
                .appendingPathComponent(".Staging", isDirectory: true)
            try fileManager.createDirectory(at: backupDirectory, withIntermediateDirectories: true)
            let candidate = backupDirectory.appendingPathComponent("\(finalDirectory.lastPathComponent)-rollback-\(UUID().uuidString.lowercased()).env")
            try fileManager.moveItem(at: finalURL, to: candidate)
            backupURL = candidate
        } else {
            backupURL = nil
        }

        do {
            try fileManager.moveItem(at: stagedURL, to: finalURL)
        } catch {
            if let backupURL, fileManager.fileExists(atPath: backupURL.path) {
                try? fileManager.moveItem(at: backupURL, to: finalURL)
            }
            throw error
        }

        return CommitRollback(fileManager: fileManager, finalURL: finalURL, backupURL: backupURL)
    }

    func discardStagedConfig(at stagedURL: URL) {
        guard stagedURL.path.contains("/.Staging/"), fileManager.fileExists(atPath: stagedURL.path) else {
            return
        }
        try? fileManager.removeItem(at: stagedURL)
    }
}
