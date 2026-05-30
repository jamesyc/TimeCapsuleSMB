import Combine
import Foundation

enum DeviceSetupWorkflowState: Equatable {
    case idle
    case configuring
    case awaitingConfirmation
    case savingProfile
    case saved
    case authFailed
    case unsupported
    case failed
}

@MainActor
final class DeviceSetupWorkflow: ObservableObject {
    @Published private(set) var state: DeviceSetupWorkflowState = .idle
    @Published private(set) var savedProfile: DeviceProfile?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?

    let coordinator: OperationCoordinator
    let profilePersistence: DeviceProfilePersistenceService

    private var pendingConfigureDraft: ConfigureProfileDraft?
    private var activeLaneKey: OperationLaneKey?
    private var operationObservers: [OperationLaneKey: BackendOperationObserver] = [:]
    private var cancellables: Set<AnyCancellable> = []
    private var observedLaneKeys: Set<OperationLaneKey> = []

    init(
        coordinator: OperationCoordinator,
        profilePersistence: DeviceProfilePersistenceService
    ) {
        self.coordinator = coordinator
        self.profilePersistence = profilePersistence
    }

    var isRunning: Bool {
        switch activeLaneKey {
        case .some(let key):
            return coordinator.lane(for: key).isBusy
        case .none:
            return false
        }
    }

    var canCancel: Bool {
        guard let activeLaneKey else {
            return false
        }
        return coordinator.lane(for: activeLaneKey).backend.canCancel
    }

    func start(
        target: AddDeviceTarget,
        password: String,
        existingProfile: DeviceProfile?,
        preferredID: DeviceProfile.ID = UUID().uuidString.lowercased(),
        settings: DeviceProfileSettings,
        newProfileSettings: DeviceProfileSettings
    ) {
        let profileID = existingProfile?.id ?? preferredID
        let laneKey = target.setupLaneKey(existingProfileID: existingProfile?.id)
        let lane = coordinator.lane(for: laneKey)
        observe(lane: lane)

        guard !lane.isBusy else {
            clearPendingConfigureDraft()
            rejectRun(L10n.string("operation.error.already_running"))
            return
        }

        let configureDraft: ConfigureProfileDraft
        do {
            configureDraft = try profilePersistence.prepareConfigureTarget(
                targetHost: target.targetHost,
                discoveredDevice: target.discoveredDevice,
                existingProfile: existingProfile,
                preferredID: profileID,
                settings: settings
            )
        } catch {
            failProfileSave(error)
            return
        }

        resetRunState()
        pendingConfigureDraft = configureDraft
        pendingPassword = password
        if configureDraft.existingProfileID == nil {
            pendingNewProfileSettings = newProfileSettings
        }
        observer(for: laneKey).clear()
        switch coordinator.run(
            operation: "configure",
            params: OperationParams.configure(
                host: target.targetHost,
                selectedRecord: target.selectedRecord,
                password: password,
                debugLogging: settings.debugLogging,
                internalShareUseDiskRoot: settings.internalShareUseDiskRoot,
                anyProtocol: settings.anyProtocol,
                ataIdleSeconds: settings.ataIdleSeconds,
                ataStandby: settings.ataStandby,
                includeAtaStandby: true
            ),
            context: configureDraft.context,
            activeDeviceID: existingProfile?.id,
            laneKey: laneKey
        ) {
        case .started(let operation):
            activeLaneKey = laneKey
            observer(for: laneKey).start(operation)
            state = .configuring
            process(lane.backend.events, laneKey: laneKey)
        case .rejected(let message):
            clearPendingConfigureDraft()
            rejectRun(message)
        }

    }

    func cancel() {
        guard let activeLaneKey else {
            return
        }
        coordinator.cancel(laneKey: activeLaneKey)
    }

    func reset() {
        if let activeLaneKey {
            let lane = coordinator.lane(for: activeLaneKey)
            if !lane.isBusy {
                lane.clear()
            }
        }
        savedProfile = nil
        error = nil
        currentStage = nil
        clearPendingConfigureDraft()
        activeLaneKey = nil
        operationObservers = [:]
        pendingNewProfileSettings = nil
        state = .idle
    }

    private var pendingNewProfileSettings: DeviceProfileSettings?

    private func observe(lane: OperationLane) {
        guard observedLaneKeys.insert(lane.key).inserted else {
            return
        }
        lane.backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events, laneKey: lane.key)
                }
            }
            .store(in: &cancellables)
        lane.backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    private func resetRunState() {
        if let activeLaneKey {
            let lane = coordinator.lane(for: activeLaneKey)
            if !lane.isBusy {
                lane.clear()
            }
            observer(for: activeLaneKey).clear()
        }
        error = nil
        currentStage = nil
        savedProfile = nil
        activeLaneKey = nil
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
    }

    private func process(_ events: [BackendEvent], laneKey: OperationLaneKey) {
        observer(for: laneKey).process(events) { event, _ in
            handle(event)
        }
    }

    private func handle(_ event: BackendEvent) {
        guard event.operation == "configure" else {
            return
        }
        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .configuring
            }
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

        state = .savingProfile
        guard let configureDraft = pendingConfigureDraft else {
            failContract(DeviceRegistryError.profileNotFound("pending"))
            return
        }
        let overrides = ConfiguredDeviceProfileOverrides(
            displayName: nil,
            settings: pendingNewProfileSettings
        )
        let savedPassword = pendingPassword
        Task { @MainActor in
            do {
                savedProfile = try await profilePersistence.commitConfiguredProfile(
                    configuredDevice: configured,
                    draft: configureDraft,
                    password: savedPassword,
                    overrides: overrides
                )
                error = nil
                state = .saved
                finishActiveOperation()
                clearPendingConfigureDraft()
                pendingNewProfileSettings = nil
                pendingPassword = ""
            } catch {
                failProfileSave(error)
            }
        }
    }

    private var pendingPassword = ""

    private func applyError(_ event: BackendEvent) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled()
            return
        }
        error = BackendErrorViewModel(event: event)
        switch event.code {
        case "auth_failed":
            state = .authFailed
        case "unsupported_device":
            state = .unsupported
        default:
            state = .failed
        }
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        savedProfile = nil
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        state = .idle
    }

    private func failFromResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(
            operation: event.operation,
            code: "operation_failed",
            message: event.localizedPayloadSummaryText ?? event.localizedSummary
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func failContract(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func failProfileSave(_ error: Error) {
        self.error = BackendErrorViewModel(
            operation: "add-device",
            code: "profile_save_failed",
            message: error.localizedDescription
        )
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func rejectRun(_ message: String) {
        error = BackendErrorViewModel(
            operation: "add-device",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        state = .failed
        finishActiveOperation()
        clearPendingConfigureDraft()
        pendingNewProfileSettings = nil
        pendingPassword = ""
    }

    private func observer(for laneKey: OperationLaneKey) -> BackendOperationObserver {
        if let observer = operationObservers[laneKey] {
            return observer
        }
        let observer = BackendOperationObserver()
        operationObservers[laneKey] = observer
        return observer
    }

    private func finishActiveOperation() {
        if let activeLaneKey {
            operationObservers[activeLaneKey]?.finish()
        }
        activeLaneKey = nil
    }

    private func clearPendingConfigureDraft() {
        profilePersistence.discardConfigureDraft(pendingConfigureDraft)
        pendingConfigureDraft = nil
    }
}
