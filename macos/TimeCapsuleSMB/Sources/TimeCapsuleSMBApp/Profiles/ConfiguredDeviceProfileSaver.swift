import Foundation

struct ConfiguredDeviceProfileOverrides: Equatable {
    var displayName: String?
    var settings: DeviceProfileSettings?

    static let empty = ConfiguredDeviceProfileOverrides()
}

@MainActor
protocol ConfiguredDeviceProfileSaving: AnyObject {
    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        password: String,
        preferredID: DeviceProfile.ID,
        existingProfileID: DeviceProfile.ID?,
        overrides: ConfiguredDeviceProfileOverrides
    ) async throws -> DeviceProfile
}

extension ConfiguredDeviceProfileSaving {
    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        password: String,
        preferredID: DeviceProfile.ID
    ) async throws -> DeviceProfile {
        try await saveConfiguredDevice(
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            password: password,
            preferredID: preferredID,
            existingProfileID: nil,
            overrides: .empty
        )
    }
}

@MainActor
final class ConfiguredDeviceProfileSaver: ConfiguredDeviceProfileSaving {
    private enum PasswordRollback {
        case delete
        case restore(String)
    }

    private let registry: DeviceRegistryStore
    private let passwordStore: PasswordStore

    init(registry: DeviceRegistryStore, passwordStore: PasswordStore) {
        self.registry = registry
        self.passwordStore = passwordStore
    }

    func saveConfiguredDevice(
        configuredDevice: ConfiguredDeviceState,
        discoveredDevice: DiscoveredDevice?,
        password: String,
        preferredID: DeviceProfile.ID,
        existingProfileID: DeviceProfile.ID? = nil,
        overrides: ConfiguredDeviceProfileOverrides = .empty
    ) async throws -> DeviceProfile {
        var profile = await registry.makeConfiguredDeviceProfile(
            configuredDevice: configuredDevice,
            discoveredDevice: discoveredDevice,
            passwordState: .available,
            preferredID: preferredID,
            existingProfileID: existingProfileID
        )
        if let displayName = overrides.displayName {
            profile.displayName = displayName
        }
        if let settings = overrides.settings {
            profile.settings = settings
        }
        let wasSavedProfile = registry.profile(id: profile.id) != nil
        let rollback = try passwordRollback(for: profile.keychainAccount)

        do {
            try passwordStore.save(password, for: profile.keychainAccount)
        } catch {
            if !wasSavedProfile {
                await registry.discardArtifacts(for: profile)
            }
            throw error
        }

        do {
            return try await registry.saveProfileMergingDuplicates(profile)
        } catch {
            rollbackPassword(rollback, account: profile.keychainAccount)
            if !wasSavedProfile {
                await registry.discardArtifacts(for: profile)
            }
            throw error
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
