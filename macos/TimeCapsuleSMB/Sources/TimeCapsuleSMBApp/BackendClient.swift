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

    func clear() {
        events.removeAll()
        lastExitCode = nil
        pendingConfirmation = nil
        currentStage = nil
        currentRisk = nil
        currentCancellable = nil
    }

    var canCancel: Bool {
        isRunning && (currentCancellable ?? true)
    }

    func run(operation: String, params: [String: JSONValue] = [:]) {
        guard !isRunning else { return }
        isRunning = true
        lastExitCode = nil
        pendingConfirmation = nil
        currentStage = nil
        currentRisk = nil
        currentCancellable = nil
        activeCall = BackendCall(operation: operation, params: params)
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
        runTask = Task.detached(priority: .userInitiated) { [runner, updateTarget, helperPath, operation, params] in
            let result = await runner.run(
                helperPath: helperPath.isEmpty ? nil : helperPath,
                operation: operation,
                params: params
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
        run(operation: confirmation.operation, params: confirmation.params)
    }

    fileprivate func appendEvent(_ event: BackendEvent) {
        if event.type == "stage" {
            currentStage = event.stage
            currentRisk = event.risk
            currentCancellable = event.cancellable
        }
        if let activeCall, let confirmation = PendingConfirmation(
            confirmationEvent: event,
            originalParams: activeCall.params
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
    }
}

private struct BackendCall: Sendable {
    let operation: String
    let params: [String: JSONValue]
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
