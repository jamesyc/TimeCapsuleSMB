import Foundation

@MainActor
final class BackendClient: ObservableObject {
    @Published var helperPath: String
    @Published var events: [BackendEvent] = []
    @Published var isRunning = false
    @Published var lastExitCode: Int32?

    private let runner: any HelperRunning
    private var runTask: Task<Void, Never>?

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
    }

    func run(operation: String, params: [String: JSONValue] = [:]) {
        guard !isRunning else { return }
        isRunning = true
        lastExitCode = nil
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
        runTask?.cancel()
    }

    fileprivate func appendEvent(_ event: BackendEvent) {
        events.append(event)
    }

    fileprivate func finishRun(exitCode: Int32) {
        lastExitCode = exitCode
        isRunning = false
        runTask = nil
    }
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
