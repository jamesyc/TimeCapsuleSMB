import Combine
import Foundation

struct DeployOptions: Equatable {
    let rsyncEnabled: Bool
    let noWait: Bool
    let internalShareUseDiskRoot: Bool
    let smbBrowseCompatibility: Bool
    let mdnsAdvertiseAFP: Bool
    let anyProtocol: Bool
    let requireSMBEncryption: Bool
    let forceDisableSMBSigningAndEncryption: Bool
    let fruitMetadataNetatalk: Bool
    let vfsAIOForkEnabled: Bool
    let debugLogging: Bool
    let ataIdleSeconds: Int
    let ataStandby: Int?
    let mountWait: Int

    init(
        rsyncEnabled: Bool = false,
        noWait: Bool,
        internalShareUseDiskRoot: Bool,
        smbBrowseCompatibility: Bool,
        mdnsAdvertiseAFP: Bool = DeviceProfileSettings.default.mdnsAdvertiseAFP,
        anyProtocol: Bool,
        requireSMBEncryption: Bool = DeviceProfileSettings.default.requireSMBEncryption,
        forceDisableSMBSigningAndEncryption: Bool = DeviceProfileSettings.default.forceDisableSMBSigningAndEncryption,
        fruitMetadataNetatalk: Bool = DeviceProfileSettings.default.fruitMetadataNetatalk,
        vfsAIOForkEnabled: Bool = DeviceProfileSettings.default.vfsAIOForkEnabled,
        debugLogging: Bool,
        ataIdleSeconds: Int = DeviceProfileSettings.default.ataIdleSeconds,
        ataStandby: Int? = DeviceProfileSettings.default.ataStandby,
        mountWait: Int
    ) {
        self.rsyncEnabled = rsyncEnabled
        self.noWait = noWait
        self.internalShareUseDiskRoot = internalShareUseDiskRoot
        self.smbBrowseCompatibility = smbBrowseCompatibility
        self.mdnsAdvertiseAFP = mdnsAdvertiseAFP
        self.anyProtocol = anyProtocol
        self.requireSMBEncryption = requireSMBEncryption
        self.forceDisableSMBSigningAndEncryption = forceDisableSMBSigningAndEncryption
        self.fruitMetadataNetatalk = fruitMetadataNetatalk
        self.vfsAIOForkEnabled = vfsAIOForkEnabled
        self.debugLogging = debugLogging
        self.ataIdleSeconds = ataIdleSeconds
        self.ataStandby = ataStandby
        self.mountWait = mountWait
    }
}

enum DeployWorkflowState: String, CaseIterable, Equatable, Codable {
    case idle
    case deploying
    case awaitingConfirmation
    case deployed
    case deployFailed

    var title: String {
        switch self {
        case .idle:
            return L10n.string("workflow.state.idle")
        case .deploying:
            return L10n.string("workflow.state.deploying")
        case .awaitingConfirmation:
            return L10n.string("workflow.state.awaiting_confirmation")
        case .deployed:
            return L10n.string("workflow.state.deployed")
        case .deployFailed:
            return L10n.string("workflow.state.deploy_failed")
        }
    }
}

@MainActor
final class DeployWorkflowStore: ObservableObject {
    @Published var rsyncEnabled = false
    @Published var noWait = false
    @Published var internalShareUseDiskRoot = false
    @Published var smbBrowseCompatibility = false
    @Published var mdnsAdvertiseAFP = DeviceProfileSettings.default.mdnsAdvertiseAFP
    @Published var anyProtocol = false {
        didSet {
            if anyProtocol && requireSMBEncryption {
                requireSMBEncryption = false
            }
        }
    }
    @Published var requireSMBEncryption = DeviceProfileSettings.default.requireSMBEncryption {
        didSet {
            if requireSMBEncryption && anyProtocol {
                anyProtocol = false
            }
            if requireSMBEncryption && forceDisableSMBSigningAndEncryption {
                forceDisableSMBSigningAndEncryption = false
            }
        }
    }
    @Published var forceDisableSMBSigningAndEncryption = DeviceProfileSettings.default.forceDisableSMBSigningAndEncryption {
        didSet {
            if forceDisableSMBSigningAndEncryption && requireSMBEncryption {
                requireSMBEncryption = false
            }
        }
    }
    @Published var fruitMetadataNetatalk = DeviceProfileSettings.default.fruitMetadataNetatalk
    @Published var vfsAIOForkEnabled = DeviceProfileSettings.default.vfsAIOForkEnabled
    @Published var debugLogging = false
    @Published var ataIdleSeconds = String(DeviceProfileSettings.default.ataIdleSeconds)
    @Published var ataStandby = DeviceProfileSettings.default.ataStandby.map { String($0) } ?? ""
    @Published var mountWait = "30"

    @Published private(set) var state: DeployWorkflowState = .idle
    @Published private(set) var result: DeployResultPayload?
    @Published private(set) var error: BackendErrorViewModel?
    @Published private(set) var currentStage: OperationStageState?
    /// Options of the most recent deploy run; persisted as the profile's rsync setting on success.
    @Published private(set) var runOptions: DeployOptions?
    @Published private(set) var passwordInvalidProfileID: DeviceProfile.ID?

    let backend: BackendClient
    private let coordinator: OperationCoordinator?
    private let laneKey: OperationLaneKey?

    private let operationObserver = BackendOperationObserver()
    private var cancellables: Set<AnyCancellable> = []

    convenience init() {
        self.init(backend: BackendClient())
    }

    init(backend: BackendClient) {
        self.backend = backend
        self.coordinator = nil
        self.laneKey = nil
        observeBackend(backend)
    }

    convenience init(coordinator: OperationCoordinator) {
        self.init(coordinator: coordinator, laneKey: .app)
    }

    init(coordinator: OperationCoordinator, laneKey: OperationLaneKey) {
        let lane = coordinator.lane(for: laneKey)
        self.backend = lane.backend
        self.coordinator = coordinator
        self.laneKey = laneKey
        observeBackend(lane.backend)
    }

    private func observeBackend(_ backend: BackendClient) {
        backend.$events
            .sink { [weak self] events in
                Task { @MainActor in
                    self?.process(events)
                }
            }
            .store(in: &cancellables)
        backend.$isRunning
            .dropFirst()
            .sink { [weak self] _ in
                self?.objectWillChange.send()
            }
            .store(in: &cancellables)
    }

    var events: [BackendEvent] {
        backend.events
    }

    var isRunning: Bool {
        backend.isRunning
    }

    var isBusy: Bool {
        backend.isRunning || backend.pendingConfirmation != nil
    }

    var canCancel: Bool {
        backend.canCancel
    }

    var mountWaitValue: Int? {
        ValueParsers.nonNegativeInteger(mountWait)
    }

    var hasValidOptions: Bool {
        deployOptionsValidationMessage == nil
    }

    var canDeploy: Bool {
        !isBusy && hasValidOptions
    }

    @discardableResult
    func runDeploy(password: String, profile: DeviceProfile? = nil) -> OperationStartResult {
        guard let options = currentOptions else {
            let localError = deployOptionsValidationError ?? .deployOptionsInvalid
            failLocally(state: .deployFailed, localError: localError)
            return .rejected(localError.message)
        }
        guard !isBusy else {
            rejectRun(state: .deployFailed, localError: .operationAlreadyRunning)
            return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
        }
        backend.clear()
        let start = run(
            operation: "deploy",
            params: OperationParams.Deploy.params(
                noWait: options.noWait,
                rsyncEnabled: options.rsyncEnabled,
                internalShareUseDiskRoot: options.internalShareUseDiskRoot,
                smbBrowseCompatibility: options.smbBrowseCompatibility,
                mdnsAdvertiseAFP: options.mdnsAdvertiseAFP,
                anyProtocol: options.anyProtocol,
                requireSMBEncryption: options.requireSMBEncryption,
                forceDisableSMBSigningAndEncryption: options.forceDisableSMBSigningAndEncryption,
                fruitMetadataNetatalk: options.fruitMetadataNetatalk,
                vfsAIOForkEnabled: options.vfsAIOForkEnabled,
                debugLogging: options.debugLogging,
                ataIdleSeconds: options.ataIdleSeconds,
                ataStandby: options.ataStandby,
                mountWait: Double(options.mountWait)
            ),
            profile: profile,
            password: password
        )
        guard case .started(let operation) = start else {
            if let message = start.rejectionMessage {
                rejectRun(state: .deployFailed, message: message)
            } else {
                rejectRun(state: .deployFailed, localError: .operationCouldNotStart)
            }
            return start
        }
        operationObserver.start(operation)
        state = .deploying
        result = nil
        error = nil
        currentStage = nil
        runOptions = options
        passwordInvalidProfileID = nil
        process(backend.events)
        return start
    }

    func clear() {
        backend.clear()
        operationObserver.clear()
        state = .idle
        result = nil
        error = nil
        currentStage = nil
        runOptions = nil
        passwordInvalidProfileID = nil
        operationObserver.finish()
    }

    func cancel() {
        backend.cancel()
    }

    private var currentOptions: DeployOptions? {
        guard let mountWaitValue, let ataIdleSecondsValue, hasValidAtaStandby else {
            return nil
        }
        return DeployOptions(
            rsyncEnabled: rsyncEnabled,
            noWait: noWait,
            internalShareUseDiskRoot: internalShareUseDiskRoot,
            smbBrowseCompatibility: smbBrowseCompatibility,
            mdnsAdvertiseAFP: mdnsAdvertiseAFP,
            anyProtocol: anyProtocol,
            requireSMBEncryption: requireSMBEncryption,
            forceDisableSMBSigningAndEncryption: forceDisableSMBSigningAndEncryption,
            fruitMetadataNetatalk: fruitMetadataNetatalk,
            vfsAIOForkEnabled: vfsAIOForkEnabled,
            debugLogging: debugLogging,
            ataIdleSeconds: ataIdleSecondsValue,
            ataStandby: ataStandbyValue,
            mountWait: mountWaitValue
        )
    }

    private var ataIdleSecondsValue: Int? {
        ValueParsers.nonNegativeInteger(ataIdleSeconds)
    }

    private var ataStandbyValue: Int? {
        let trimmed = ataStandby.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            return nil
        }
        return ValueParsers.nonNegativeInteger(trimmed)
    }

    private var hasValidAtaStandby: Bool {
        ataStandby.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || ataStandbyValue != nil
    }

    private var deployOptionsValidationError: WorkflowLocalError? {
        if mountWaitValue == nil {
            return .mountWaitInvalid
        }
        if ataIdleSecondsValue == nil {
            return .ataIdleSecondsInvalid
        }
        if !hasValidAtaStandby {
            return .ataStandbyInvalid
        }
        return nil
    }

    private var deployOptionsValidationMessage: String? {
        deployOptionsValidationError?.message
    }

    private func process(_ events: [BackendEvent]) {
        operationObserver.process(events) { event, operation in
            handle(event, activeOperation: operation)
        }
    }

    private func handle(_ event: BackendEvent, activeOperation: ActiveOperation) {
        guard event.operation == "deploy" else {
            return
        }

        if let stage = OperationStageState(event: event) {
            currentStage = stage
            if state == .awaitingConfirmation {
                state = .deploying
            }
            return
        }

        if event.type == "error" {
            applyError(event, activeOperation: activeOperation)
            return
        }

        guard event.type == "result" else {
            return
        }
        if event.ok == false {
            applyFailureResult(event)
            return
        }

        if state == .deploying || state == .awaitingConfirmation {
            applyDeployResult(event)
        }
    }

    private func applyDeployResult(_ event: BackendEvent) {
        do {
            result = try event.decodePayload(DeployResultPayload.self)
            error = nil
            state = .deployed
            operationObserver.finish()
        } catch {
            failContract(state: .deployFailed, error: error)
        }
    }

    private func applyError(_ event: BackendEvent, activeOperation: ActiveOperation) {
        if event.code == "confirmation_required" {
            error = nil
            state = .awaitingConfirmation
            return
        }
        if event.code == "confirmation_cancelled" {
            applyConfirmationCancelled()
            return
        }
        if event.code == "auth_failed" {
            passwordInvalidProfileID = activeOperation.profileID
        }
        error = BackendErrorViewModel(event: event)
        state = .deployFailed
        operationObserver.finish()
    }

    private func applyConfirmationCancelled() {
        error = nil
        currentStage = nil
        operationObserver.finish()
        state = .idle
    }

    private func applyFailureResult(_ event: BackendEvent) {
        error = BackendErrorViewModel(event: event)
        state = .deployFailed
        operationObserver.finish()
    }

    private func failContract(state: DeployWorkflowState, error: Error) {
        self.error = BackendErrorViewModel(
            operation: "deploy",
            code: "contract_decode_failed",
            message: error.localizedDescription
        )
        self.state = state
        operationObserver.finish()
    }

    private func failLocally(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "validation_failed",
            message: message
        )
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func failLocally(state: DeployWorkflowState, localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "deploy", localError: localError)
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func rejectRun(state: DeployWorkflowState, message: String) {
        error = BackendErrorViewModel(
            operation: "deploy",
            code: "operation_rejected",
            message: message
        )
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func rejectRun(state: DeployWorkflowState, localError: WorkflowLocalError) {
        error = BackendErrorViewModel(operation: "deploy", localError: localError)
        currentStage = nil
        self.state = state
        operationObserver.finish()
    }

    private func run(
        operation: String,
        params: [String: JSONValue],
        profile: DeviceProfile?,
        password: String? = nil
    ) -> OperationStartResult {
        if let coordinator {
            return coordinator.run(
                operation: operation,
                params: params,
                context: profile?.runtimeContext,
                activeDeviceID: profile?.id,
                password: password,
                laneKey: laneKey
            )
        } else {
            guard !isBusy else {
                return .rejected(WorkflowLocalError.operationAlreadyRunning.message)
            }
            let updatedParams = OperationCredentialInjector.injectingPassword(password, into: params)
            let context = profile?.runtimeContext
            let activeOperation = ActiveOperation(operation: operation, profileID: profile?.id, context: context)
            backend.run(
                operation: operation,
                params: updatedParams,
                context: context,
                requestID: activeOperation.id.uuidString
            )
            return .started(activeOperation)
        }
    }
}
