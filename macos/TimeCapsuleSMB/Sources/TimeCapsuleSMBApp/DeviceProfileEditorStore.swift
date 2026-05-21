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
    case passwordRequired

    var errorDescription: String? {
        switch self {
        case .hostRequired:
            return L10n.string("profile_editor.error.host_required")
        case .duplicateHost:
            return L10n.string("profile_editor.error.duplicate_host")
        case .mountWaitInvalid:
            return L10n.string("profile_editor.error.mount_wait_invalid")
        case .passwordRequired:
            return L10n.string("profile_editor.error.password_required")
        }
    }
}

struct DeviceProfileEditorDraft: Equatable {
    var displayName: String
    var host: String
    var nbnsEnabled: Bool
    var debugLogging: Bool
    var mountWaitSeconds: String

    init(
        displayName: String,
        host: String,
        nbnsEnabled: Bool,
        debugLogging: Bool,
        mountWaitSeconds: String
    ) {
        self.displayName = displayName
        self.host = host
        self.nbnsEnabled = nbnsEnabled
        self.debugLogging = debugLogging
        self.mountWaitSeconds = mountWaitSeconds
    }

    init(profile: DeviceProfile) {
        self.init(
            displayName: profile.displayName,
            host: profile.host,
            nbnsEnabled: profile.settings.nbnsEnabled,
            debugLogging: profile.settings.debugLogging,
            mountWaitSeconds: String(profile.settings.mountWaitSeconds)
        )
    }

    var trimmedHost: String {
        host.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func hostChanged(from profile: DeviceProfile) -> Bool {
        trimmedHost != profile.host.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func validatedSettings() throws -> DeviceProfileSettings {
        guard let mountWait = ValueParsers.nonNegativeInteger(mountWaitSeconds) else {
            throw DeviceProfileEditorValidationError.mountWaitInvalid
        }
        return DeviceProfileSettings(
            nbnsEnabled: nbnsEnabled,
            debugLogging: debugLogging,
            mountWaitSeconds: mountWait
        )
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

    private let appStore: AppStore
    private let coordinator: OperationCoordinator
    private let profileSaver: ConfiguredDeviceProfileSaving
    private var activeOperation: ActiveOperation?
    private var pendingProfile: DeviceProfile?
    private var pendingDraft: DeviceProfileEditorDraft?
    private var pendingPassword: String?
    private var lastProcessedEventCount = 0
    private var isApplyingDraft = false
    private var cancellables: Set<AnyCancellable> = []

    init(
        profile: DeviceProfile,
        appStore: AppStore,
        profileSaver: ConfiguredDeviceProfileSaving? = nil
    ) {
        self.draft = DeviceProfileEditorDraft(profile: profile)
        self.appStore = appStore
        self.coordinator = appStore.operationCoordinator
        self.profileSaver = profileSaver ?? ConfiguredDeviceProfileSaver(
            registry: appStore.deviceRegistry,
            passwordStore: appStore.passwordStore
        )
        observeBackend()
    }

    var isRunning: Bool {
        state == .saving || state == .reconfiguring
    }

    func canSave(profile: DeviceProfile) -> Bool {
        !isRunning && draft != DeviceProfileEditorDraft(profile: profile)
    }

    func reset(to profile: DeviceProfile) {
        applyDraft(DeviceProfileEditorDraft(profile: profile))
        validationErrors = []
        error = nil
        currentStage = nil
        savedProfile = nil
        state = .clean
        clearPendingOperation()
    }

    func save(profile: DeviceProfile) async {
        let validationErrors = validationErrors(for: profile)
        guard validationErrors.isEmpty else {
            self.validationErrors = validationErrors
            error = nil
            state = .invalid
            return
        }

        if draft.hostChanged(from: profile) {
            guard let password = appStore.password(for: profile) else {
                self.validationErrors = [.passwordRequired]
                error = nil
                state = .invalid
                return
            }
            startReconfigure(profile: profile, password: password)
        } else {
            await saveRegistryOnly(profile: profile)
        }
    }

    private func observeBackend() {
        coordinator.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
    }

    private func validationErrors(for profile: DeviceProfile) -> [DeviceProfileEditorValidationError] {
        var errors: [DeviceProfileEditorValidationError] = []
        if draft.trimmedHost.isEmpty {
            errors.append(.hostRequired)
        } else if let duplicate = appStore.deviceRegistry.matchingProfile(host: draft.trimmedHost, bonjourFullname: nil),
                  duplicate.id != profile.id {
            errors.append(.duplicateHost)
        }
        if ValueParsers.nonNegativeInteger(draft.mountWaitSeconds) == nil {
            errors.append(.mountWaitInvalid)
        }
        return errors
    }

    private func saveRegistryOnly(profile: DeviceProfile) async {
        state = .saving
        validationErrors = []
        error = nil
        currentStage = nil
        do {
            let saved = try await appStore.saveProfileEdits(profile: profile, draft: draft)
            savedProfile = saved
            applyDraft(DeviceProfileEditorDraft(profile: saved))
            state = .saved
        } catch {
            failSave(error)
        }
    }

    private func startReconfigure(profile: DeviceProfile, password: String) {
        let params = OperationParams.configure(
            host: draft.trimmedHost,
            password: password,
            debugLogging: draft.debugLogging
        )
        let start = coordinator.run(
            operation: "configure",
            params: params,
            context: profile.runtimeContext,
            activeDeviceID: profile.id
        )
        guard case .started(let operation) = start else {
            error = BackendErrorViewModel(
                operation: "configure",
                code: "operation_rejected",
                message: start.rejectionMessage ?? L10n.string("operation.error.already_running")
            )
            state = .failed
            return
        }
        lastProcessedEventCount = 0
        activeOperation = operation
        pendingProfile = profile
        pendingDraft = draft
        pendingPassword = password
        validationErrors = []
        error = nil
        currentStage = nil
        savedProfile = nil
        state = .reconfiguring
    }

    private func process(_ events: [BackendEvent]) {
        if events.count < lastProcessedEventCount {
            lastProcessedEventCount = 0
        }
        guard events.count > lastProcessedEventCount else {
            return
        }
        for event in events.dropFirst(lastProcessedEventCount) {
            handle(event)
        }
        lastProcessedEventCount = events.count
    }

    private func handle(_ event: BackendEvent) {
        guard activeOperation?.operation == event.operation, event.operation == "configure" else {
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
        applyConfigureResult(event)
    }

    private func applyConfigureResult(_ event: BackendEvent) {
        let configured: ConfiguredDeviceState
        do {
            configured = ConfiguredDeviceState(payload: try event.decodePayload(ConfigurePayload.self))
        } catch {
            failContract(error)
            return
        }
        guard let profile = pendingProfile,
              let draft = pendingDraft,
              let password = pendingPassword else {
            failContract(DeviceRegistryError.profileNotFound(activeOperation?.profileID ?? "unknown"))
            return
        }

        state = .saving
        Task { @MainActor in
            do {
                let saved = try await profileSaver.saveConfiguredDevice(
                    configuredDevice: configured,
                    discoveredDevice: nil,
                    password: password,
                    preferredID: profile.id,
                    existingProfileID: profile.id,
                    overrides: ConfiguredDeviceProfileOverrides(
                        displayName: draft.displayName,
                        settings: try draft.validatedSettings()
                    )
                )
                savedProfile = saved
                applyDraft(DeviceProfileEditorDraft(profile: saved))
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
            if let profileID = activeOperation?.profileID {
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
            message: event.payloadSummaryText ?? event.summary
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
        activeOperation = nil
        pendingProfile = nil
        pendingDraft = nil
        pendingPassword = nil
    }

    private func applyDraft(_ draft: DeviceProfileEditorDraft) {
        isApplyingDraft = true
        self.draft = draft
        isApplyingDraft = false
    }

    private func markDirtyAfterDraftChange() {
        guard !isApplyingDraft, !isRunning else {
            return
        }
        error = nil
        validationErrors = []
        savedProfile = nil
        state = .dirty
    }
}
