import Foundation

@MainActor
final class BackendClient: ObservableObject {
    @Published var helperPath: String
    @Published var events: [BackendEvent] = []
    @Published var isRunning = false
    @Published var lastExitCode: Int32?
    @Published var pendingConfirmation: PendingConfirmation?
    @Published var currentStage: String?
    @Published var currentRisk: String?
    @Published var currentCancellable: Bool?
    @Published private(set) var activeOperationName: String?

    private let runner: any HelperRunning
    private var runTask: Task<Void, Never>?
    private var activeCall: BackendCall?

    init(
        runner: any HelperRunning = HelperRunner(),
        helperPath: String = ProcessInfo.processInfo.environment["TCAPSULE_HELPER"] ?? ""
    ) {
        self.runner = runner
        self.helperPath = helperPath
    }

    deinit {
        runTask?.cancel()
    }

    func clear() {
        guard !isRunning else {
            return
        }
        events.removeAll()
        lastExitCode = nil
        pendingConfirmation = nil
        currentStage = nil
        currentRisk = nil
        currentCancellable = nil
        activeOperationName = nil
    }

    var canCancel: Bool {
        isRunning && (currentCancellable ?? true)
    }

    func run(operation: String, params: [String: JSONValue] = [:], context: DeviceRuntimeContext? = nil) {
        guard !isRunning else { return }
        var runParams = params
        if let context, runParams["config"] == nil {
            runParams["config"] = .string(context.configURL.path)
        }
        isRunning = true
        lastExitCode = nil
        pendingConfirmation = nil
        currentStage = nil
        currentRisk = nil
        currentCancellable = nil
        activeOperationName = operation
        activeCall = BackendCall(operation: operation, params: runParams, context: context)
        let helperPath = self.helperPath.trimmingCharacters(in: .whitespacesAndNewlines)
        let runner = self.runner
        let updateTarget = BackendClientUpdateTarget(
            appendEvent: { [weak self] event in
                self?.appendEvent(event)
            },
            finishRun: { [weak self] exitCode in
                self?.finishRun(exitCode: exitCode)
            }
        )
        runTask = Task.detached(priority: .userInitiated) { [runner, updateTarget, helperPath, operation, runParams, context] in
            let result = await runner.run(
                helperPath: helperPath.isEmpty ? nil : helperPath,
                operation: operation,
                params: runParams,
                context: context
            ) { event in
                await updateTarget.appendEvent(event)
            }
            await updateTarget.finishRun(exitCode: result.exitCode)
        }
    }

    func cancel() {
        guard canCancel else { return }
        runTask?.cancel()
    }

    func confirmPending() {
        guard let confirmation = pendingConfirmation, !isRunning else { return }
        pendingConfirmation = nil
        run(operation: confirmation.operation, params: confirmation.params, context: confirmation.context)
    }

    fileprivate func appendEvent(_ event: BackendEvent) {
        if event.type == "stage" {
            currentStage = event.stage
            currentRisk = event.risk
            currentCancellable = event.cancellable
        }
        if let activeCall, let confirmation = PendingConfirmation(
            confirmationEvent: event,
            originalParams: activeCall.params,
            context: activeCall.context
        ) {
            pendingConfirmation = confirmation
        }
        events.append(event)
    }

    fileprivate func finishRun(exitCode: Int32) {
        lastExitCode = exitCode
        isRunning = false
        runTask = nil
        activeCall = nil
        activeOperationName = nil
    }
}

private struct BackendCall: Sendable {
    let operation: String
    let params: [String: JSONValue]
    let context: DeviceRuntimeContext?
}

private final class BackendClientUpdateTarget: Sendable {
    private let appendEventOnMain: @MainActor @Sendable (BackendEvent) -> Void
    private let finishRunOnMain: @MainActor @Sendable (Int32) -> Void

    init(
        appendEvent: @escaping @MainActor @Sendable (BackendEvent) -> Void,
        finishRun: @escaping @MainActor @Sendable (Int32) -> Void
    ) {
        self.appendEventOnMain = appendEvent
        self.finishRunOnMain = finishRun
    }

    func appendEvent(_ event: BackendEvent) async {
        await appendEventOnMain(event)
    }

    func finishRun(exitCode: Int32) async {
        await finishRunOnMain(exitCode)
    }
}
