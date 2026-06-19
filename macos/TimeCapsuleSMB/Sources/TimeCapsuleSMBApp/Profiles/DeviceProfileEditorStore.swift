import Combine
import Foundation

enum DeviceProfileEditorState: String, CaseIterable, Equatable {
    case clean
    case dirty
    case invalid
    case saving
    case reconfiguring
    case saved
    case authFailed
    case unsupported
    case failed

    var title: String {
        switch self {
        case .clean:
            return L10n.string("profile_editor.state.clean")
        case .dirty:
            return L10n.string("profile_editor.state.dirty")
        case .invalid:
            return L10n.string("profile_editor.state.invalid")
        case .saving:
            return L10n.string("profile_editor.state.saving")
        case .reconfiguring:
            return L10n.string("profile_editor.state.reconfiguring")
        case .saved:
            return L10n.string("profile_editor.state.saved")
        case .authFailed:
            return L10n.string("profile_editor.state.auth_failed")
        case .unsupported:
            return L10n.string("profile_editor.state.unsupported")
        case .failed:
            return L10n.string("profile_editor.state.failed")
        }
    }
}

enum DeviceProfileEditorValidationError: String, CaseIterable, Equatable, LocalizedError {
    case hostRequired
    case duplicateHost
    case mountWaitInvalid
    case ataIdleSecondsInvalid
    case ataStandbyInvalid
    case passwordRequired

    var errorDescription: String? {
        switch self {
        case .hostRequired:
            return L10n.string("profile_editor.error.host_required")
        case .duplicateHost:
            return L10n.string("profile_editor.error.duplicate_host")
        case .mountWaitInvalid:
            return L10n.string("profile_editor.error.mount_wait_invalid")
        case .ataIdleSecondsInvalid:
            return L10n.string("profile_editor.error.ata_idle_seconds_invalid")
        case .ataStandbyInvalid:
            return L10n.string("profile_editor.error.ata_standby_invalid")
        case .passwordRequired:
            return L10n.string("profile_editor.error.password_required")
        }
    }
}

fileprivate struct DeviceProfileEditorSettingsValidation {
    let settings: DeviceProfileSettings?
    let errors: [DeviceProfileEditorValidationError]
}

fileprivate struct DeviceProfileEditorDraftValidation {
    let settings: DeviceProfileSettings?
    let errors: [DeviceProfileEditorValidationError]
}

struct DeviceProfileEditorDraft: Equatable {
    var displayName: String
    var host: String
    var nbnsEnabled: Bool
    var internalShareUseDiskRoot: Bool
    var smbBindLanOnly: Bool
    var smbBrowseCompatibility: Bool
    var mdnsAdvertiseAFP: Bool
    var anyProtocol: Bool
    var fruitMetadataNetatalk: Bool
    var debugLogging: Bool
    var mountWaitSeconds: String
    var ataIdleSeconds: String
    var ataStandby: String

    init(
        displayName: String,
        host: String,
        nbnsEnabled: Bool,
        internalShareUseDiskRoot: Bool = false,
        smbBindLanOnly: Bool = DeviceProfileSettings.default.smbBindLanOnly,
        smbBrowseCompatibility: Bool = false,
        mdnsAdvertiseAFP: Bool = DeviceProfileSettings.default.mdnsAdvertiseAFP,
        anyProtocol: Bool = false,
        fruitMetadataNetatalk: Bool = true,
        debugLogging: Bool,
        mountWaitSeconds: String,
        ataIdleSeconds: String = String(DeviceProfileSettings.default.ataIdleSeconds),
        ataStandby: String = DeviceProfileSettings.default.ataStandby.map { String($0) } ?? ""
    ) {
        self.displayName = displayName
        self.host = host
        self.nbnsEnabled = nbnsEnabled
        self.internalShareUseDiskRoot = internalShareUseDiskRoot
        self.smbBindLanOnly = smbBindLanOnly
        self.smbBrowseCompatibility = smbBrowseCompatibility
        self.mdnsAdvertiseAFP = mdnsAdvertiseAFP
        self.anyProtocol = anyProtocol
        self.fruitMetadataNetatalk = fruitMetadataNetatalk
        self.debugLogging = debugLogging
        self.mountWaitSeconds = mountWaitSeconds
        self.ataIdleSeconds = ataIdleSeconds
        self.ataStandby = ataStandby
    }

    init(profile: DeviceProfile) {
        self.init(
            displayName: profile.displayName,
            host: profile.host,
            nbnsEnabled: profile.settings.nbnsEnabled,
            internalShareUseDiskRoot: profile.settings.internalShareUseDiskRoot,
            smbBindLanOnly: profile.settings.smbBindLanOnly,
            smbBrowseCompatibility: profile.settings.smbBrowseCompatibility,
            mdnsAdvertiseAFP: profile.settings.mdnsAdvertiseAFP,
            anyProtocol: profile.settings.anyProtocol,
            fruitMetadataNetatalk: profile.settings.fruitMetadataNetatalk,
            debugLogging: profile.settings.debugLogging,
            mountWaitSeconds: String(profile.settings.mountWaitSeconds),
            ataIdleSeconds: String(profile.settings.ataIdleSeconds),
            ataStandby: profile.settings.ataStandby.map { String($0) } ?? ""
        )
    }

    var trimmedHost: String {
        host.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func hostChanged(from profile: DeviceProfile) -> Bool {
        DeviceEndpointPolicy.normalizedHostKey(trimmedHost) != DeviceEndpointPolicy.normalizedHostKey(profile.host)
    }

    func validatedSettings() throws -> DeviceProfileSettings {
        let validation = settingsValidation()
        if let settings = validation.settings {
            return settings
        }
        throw validation.errors.first ?? DeviceProfileEditorValidationError.mountWaitInvalid
    }

    fileprivate func settingsValidation() -> DeviceProfileEditorSettingsValidation {
        var errors: [DeviceProfileEditorValidationError] = []
        let mountWait = ValueParsers.nonNegativeInteger(mountWaitSeconds)
        if mountWait == nil {
            errors.append(.mountWaitInvalid)
        }
        let ataIdle = ValueParsers.nonNegativeInteger(ataIdleSeconds)
        if ataIdle == nil {
            errors.append(.ataIdleSecondsInvalid)
        }
        let trimmedAtaStandby = ataStandby.trimmingCharacters(in: .whitespacesAndNewlines)
        let parsedAtaStandby: Int?
        if trimmedAtaStandby.isEmpty {
            parsedAtaStandby = nil
        } else if let value = ValueParsers.nonNegativeInteger(trimmedAtaStandby) {
            parsedAtaStandby = value
        } else {
            errors.append(.ataStandbyInvalid)
            parsedAtaStandby = nil
        }
        guard errors.isEmpty, let mountWait, let ataIdle else {
            return DeviceProfileEditorSettingsValidation(settings: nil, errors: errors)
        }
        let settings = DeviceProfileSettings(
            nbnsEnabled: nbnsEnabled,
            internalShareUseDiskRoot: internalShareUseDiskRoot,
            smbBindLanOnly: smbBindLanOnly,
            smbBrowseCompatibility: smbBrowseCompatibility,
            mdnsAdvertiseAFP: mdnsAdvertiseAFP,
            anyProtocol: anyProtocol,
            fruitMetadataNetatalk: fruitMetadataNetatalk,
            debugLogging: debugLogging,
            mountWaitSeconds: mountWait,
            ataIdleSeconds: ataIdle,
            ataStandby: parsedAtaStandby
        )
        return DeviceProfileEditorSettingsValidation(settings: settings, errors: [])
    }

    func editableFields() throws -> DeviceProfileEditableFields {
        DeviceProfileEditableFields(displayName: displayName, settings: try validatedSettings())
    }
}

@MainActor
final class DeviceProfileEditorStore: ObservableObject {
    @Published var draft: DeviceProfileEditorDraft {
        didSet { markDirtyAfterDraftChange() }
    }
    @Published private(set) var state: DeviceProfileEditorState = .clean
    @Published private(set) var validationErrors: [DeviceProfileEditorValidationError] = []
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?
    @Published private(set) var savedProfile: DeviceProfile?
    @Published var replacementPassword = "" {
        didSet { markDirtyAfterPasswordChange() }
    }
    @Published private(set) var passwordError: String?

    private let appStore: AppStore
    private let coordinator: OperationCoordinator
    private let lane: OperationLane
    private let profilePersistence: DeviceProfilePersistenceService
    private let localNetworkPreflightChecker: LocalNetworkPreflightChecking?
    private var baselineDraft: DeviceProfileEditorDraft
    private let operationObserver = BackendOperationObserver()
    private var pendingProfile: DeviceProfile?
    private var pendingEditableFields: DeviceProfileEditableFields?
    private var pendingPassword: String?
    private var pendingConfigureDraft: ConfigureProfileDraft?
    private var localNetworkPreflightTask: Task<Void, Never>?
    private var isApplyingDraft = false
    private var isApplyingPasswordDraft = false
    private var cancellables: Set<AnyCancellable> = []

    init(
        profile: DeviceProfile,
        appStore: AppStore,
        profilePersistence: DeviceProfilePersistenceService? = nil
    ) {
        let initialDraft = DeviceProfileEditorDraft(profile: profile)
        self.draft = initialDraft
        self.baselineDraft = initialDraft
        self.appStore = appStore
        self.coordinator = appStore.operationCoordinator
        self.lane = appStore.operationCoordinator.lane(for: .deviceWorkflow(profile.id, .configure))
        self.profilePersistence = profilePersistence ?? appStore.profilePersistence
        self.localNetworkPreflightChecker = appStore.localNetworkPreflightChecker
        observeBackend()
    }

    var isRunning: Bool {
        state == .saving || state == .reconfiguring
    }

    var canSave: Bool {
        !isRunning && hasPendingChanges
    }

    private var hasPendingPasswordChange: Bool {
        !replacementPassword.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var hasPendingChanges: Bool {
        draft != baselineDraft || hasPendingPasswordChange
    }

    func sync(to profile: DeviceProfile) {
        let profileDraft = DeviceProfileEditorDraft(profile: profile)
        guard profileDraft != baselineDraft else {
            return
        }

        let wasClean = !hasPendingChanges
        baselineDraft = profileDraft
        guard !isRunning else {
            return
        }

        if wasClean {
            applyDraft(profileDraft)
            validationErrors = []
            error = nil
            currentStage = nil
            savedProfile = nil
            state = .clean
        } else {
            updateDraftChangeState()
        }
    }

    func reset(to profile: DeviceProfile) {
        let profileDraft = DeviceProfileEditorDraft(profile: profile)
        baselineDraft = profileDraft
        applyDraft(profileDraft)
        applyPasswordDraft("")
        passwordError = nil
        validationErrors = []
        error = nil
        currentStage = nil
        savedProfile = nil
        state = .clean
        clearPendingOperation()
    }

    func save(profile: DeviceProfile) async {
        let validation = validationResult(for: profile)
        guard validation.errors.isEmpty, let settings = validation.settings else {
            self.validationErrors = validation.errors
            self.error = nil
            self.state = .invalid
            return
        }

        let pendingReplacementPassword = hasPendingPasswordChange ? replacementPassword : nil

        if draft.hostChanged(from: profile) {
            guard let password = pendingReplacementPassword ?? appStore.password(for: profile) else {
                self.validationErrors = [.passwordRequired]
                passwordError = L10n.string("password.error.required")
                error = nil
                state = .invalid
                return
            }
            startReconfigure(profile: profile, password: password, settings: settings)
        } else {
            await saveRegistryOnly(profile: profile, settings: settings, replacementPassword: pendingReplacementPassword)
        }
    }

    func requestPasswordReplacement(error: String?) {
        if !hasPendingPasswordChange {
            applyPasswordDraft("")
        }
        passwordError = error
        if error != nil {
            validationErrors = []
            self.error = nil
            state = .invalid
        } else {
            updateDraftChangeState()
        }
    }

    func clearPasswordAttention() {
        passwordError = nil
        if state == .invalid && validationErrors.isEmpty {
            updateDraftChangeState()
        }
    }

    private func observeBackend() {
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
    }

    private func validationResult(for profile: DeviceProfile) -> DeviceProfileEditorDraftValidation {
        var errors: [DeviceProfileEditorValidationError] = []
        if draft.trimmedHost.isEmpty {
            errors.append(.hostRequired)
        } else if let duplicate = appStore.deviceRegistry.matchingProfile(host: draft.trimmedHost, bonjourFullname: nil),
                  duplicate.id != profile.id {
            errors.append(.duplicateHost)
        }
        let settingsValidation = draft.settingsValidation()
        errors.append(contentsOf: settingsValidation.errors)
        return DeviceProfileEditorDraftValidation(
            settings: errors.isEmpty ? settingsValidation.settings : nil,
            errors: errors
        )
    }

    private func saveRegistryOnly(profile: DeviceProfile, settings: DeviceProfileSettings, replacementPassword: String?) async {
        state = .saving
        validationErrors = []
        error = nil
        currentStage = nil
        do {
            let saved = try await appStore.saveProfileEdits(
                profile: profile,
                fields: DeviceProfileEditableFields(displayName: draft.displayName, settings: settings),
                replacementPassword: replacementPassword
            )
            savedProfile = saved
            let savedDraft = DeviceProfileEditorDraft(profile: saved)
            baselineDraft = savedDraft
            applyDraft(savedDraft)
            applyPasswordDraft("")
            passwordError = nil
            state = .saved
        } catch {
            if replacementPassword != nil {
                passwordError = error.localizedDescription
            }
            failSave(error)
        }
    }

    private func startReconfigure(profile: DeviceProfile, password: String, settings: DeviceProfileSettings) {
        let discoveredDevice = currentDiscoveredDeviceMatchingDraftHost(for: profile)
        let targetHost = discoveredDevice?.connectionTarget ?? draft.trimmedHost
        let editableFields = DeviceProfileEditableFields(displayName: draft.displayName, settings: settings)
        let configureDraft: ConfigureProfileDraft
        do {
            configureDraft = try profilePersistence.prepareConfigureTarget(
                targetHost: targetHost,
                discoveredDevice: discoveredDevice,
                existingProfile: profile,
                preferredID: profile.id,
                settings: settings
            )
        } catch {
            failSave(error)
            return
        }

        if let localNetworkPreflightChecker {
            state = .reconfiguring
            validationErrors = []
            error = nil
            currentStage = nil
            savedProfile = nil
            localNetworkPreflightTask = Task { [weak self, localNetworkPreflightChecker] in
                let result = await localNetworkPreflightChecker.check()
                await MainActor.run {
                    guard let self, !Task.isCancelled else {
                        return
                    }
                    self.localNetworkPreflightTask = nil
                    self.runReconfigureBackend(
                        profile: profile,
                        password: password,
                        settings: settings,
                        editableFields: editableFields,
                        discoveredDevice: discoveredDevice,
                        targetHost: targetHost,
                        configureDraft: configureDraft,
                        localNetworkPreflight: result
                    )
                }
            }
            return
        }

        runReconfigureBackend(
            profile: profile,
            password: password,
            settings: settings,
            editableFields: editableFields,
            discoveredDevice: discoveredDevice,
            targetHost: targetHost,
            configureDraft: configureDraft,
            localNetworkPreflight: nil
        )
    }

    private func runReconfigureBackend(
        profile: DeviceProfile,
        password: String,
        settings: DeviceProfileSettings,
        editableFields: DeviceProfileEditableFields,
        discoveredDevice: DiscoveredDevice?,
        targetHost: String,
        configureDraft: ConfigureProfileDraft,
        localNetworkPreflight: LocalNetworkPreflightResult?
    ) {
        let params = OperationParams.Configure.save(
            host: targetHost,
            selectedRecord: discoveredDevice?.rawRecord,
            password: password,
            debugLogging: draft.debugLogging,
            internalShareUseDiskRoot: draft.internalShareUseDiskRoot,
            smbBindLanOnly: draft.smbBindLanOnly,
            smbBrowseCompatibility: draft.smbBrowseCompatibility,
            mdnsAdvertiseAFP: draft.mdnsAdvertiseAFP,
            anyProtocol: draft.anyProtocol,
            fruitMetadataNetatalk: draft.fruitMetadataNetatalk,
            ataIdleSeconds: settings.ataIdleSeconds,
            ataStandby: settings.ataStandby,
            includeAtaStandby: true,
            localNetworkPreflight: localNetworkPreflight
        )
        let start = coordinator.run(
            operation: "configure",
            params: params,
            context: configureDraft.context,
            activeDeviceID: profile.id,
            laneKey: .deviceWorkflow(profile.id, .configure)
        )
        guard case .started(let operation) = start else {
            error = BackendErrorViewModel(
                operation: "configure",
                code: "operation_rejected",
                message: start.rejectionMessage ?? L10n.string("operation.error.already_running")
            )
            state = .failed
            profilePersistence.discardConfigureDraft(configureDraft)
            return
        }
        operationObserver.start(operation)
        pendingProfile = profile
        pendingEditableFields = editableFields
        pendingPassword = password
        pendingConfigureDraft = configureDraft
        validationErrors = []
        error = nil
        currentStage = nil
        savedProfile = nil
        state = .reconfiguring
        process(lane.backend.events)
    }

    private func currentDiscoveredDeviceMatchingDraftHost(for profile: DeviceProfile) -> DiscoveredDevice? {
        guard let device = appStore.deviceDiscovery.currentDiscoveredDevice(for: profile),
              DeviceEndpointPolicy.normalizedHostKey(draft.trimmedHost) == DeviceEndpointPolicy.normalizedHostKey(device.connectionTarget) else {
            return nil
        }
        return device
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "configure" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            return
        }
        if event.type == "error" {
            applyError(event)
            return
        }
        guard event.type == "result" else {
            return
        }
        if event.ok == false {
            failFromResult(event)
            return
        }
        applyConfigureResult(event, activeOperation: activeOperation)
    }

    private func applyConfigureResult(_ event: BackendEvent, activeOperation: ActiveOperation) {
        let configured: ConfiguredDeviceState
        do {
            configured = ConfiguredDeviceState(payload: try event.decodePayload(ConfigurePayload.self))
        } catch {
            failContract(error)
            return
        }
        guard pendingProfile != nil,
              let editableFields = pendingEditableFields,
              let password = pendingPassword,
              let configureDraft = pendingConfigureDraft else {
            failContract(DeviceRegistryError.profileNotFound(activeOperation.profileID ?? "unknown"))
            return
        }

        state = .saving
        Task { @MainActor in
            do {
                let saved = try await profilePersistence.commitConfiguredProfile(
                    configuredDevice: configured,
                    draft: configureDraft,
                    password: password,
                    overrides: ConfiguredDeviceProfileOverrides(
                        displayName: editableFields.displayName,
                        settings: editableFields.settings
                    )
                )
                savedProfile = saved
                let savedDraft = DeviceProfileEditorDraft(profile: saved)
                baselineDraft = savedDraft
                applyDraft(savedDraft)
                applyPasswordDraft("")
                passwordError = nil
                error = nil
                validationErrors = []
                currentStage = nil
                state = .saved
                clearPendingOperation()
            } catch {
                failSave(error)
            }
        }
    }

    private func applyError(_ event: BackendEvent) {
        error = BackendErrorViewModel(event: event)
        switch event.code {
        case "auth_failed":
            if let profileID = operationObserver.activeOperation?.profileID {
                Task { await appStore.deviceRegistry.updatePasswordState(.invalid, for: profileID) }
            }
            state = .authFailed
        case "unsupported_device":
            state = .unsupported
        default:
            state = .failed
        }
        clearPendingOperation()
    }

    private func failFromResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        clearPendingOperation()
    }

    private func failContract(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "configure",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        state = .failed
        clearPendingOperation()
    }

    private func failSave(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "device-profile",
            code: "profile_save_failed",
            message: error.localizedDescription
        )
        state = .failed
        clearPendingOperation()
    }

    private func clearPendingOperation() {
        localNetworkPreflightTask?.cancel()
        localNetworkPreflightTask = nil
        operationObserver.finish()
        profilePersistence.discardConfigureDraft(pendingConfigureDraft)
        pendingProfile = nil
        pendingEditableFields = nil
        pendingPassword = nil
        pendingConfigureDraft = nil
    }

    private func applyDraft(_ draft: DeviceProfileEditorDraft) {
        isApplyingDraft = true
        self.draft = draft
        isApplyingDraft = false
    }

    private func applyPasswordDraft(_ password: String) {
        isApplyingPasswordDraft = true
        replacementPassword = password
        isApplyingPasswordDraft = false
    }

    private func markDirtyAfterDraftChange() {
        guard !isApplyingDraft, !isRunning else {
            return
        }
        updateDraftChangeState()
    }

    private func markDirtyAfterPasswordChange() {
        guard !isApplyingPasswordDraft, !isRunning else {
            return
        }
        passwordError = nil
        updateDraftChangeState()
    }

    private func updateDraftChangeState() {
        error = nil
        validationErrors = []
        savedProfile = nil
        state = hasPendingChanges ? .dirty : .clean
    }
}
