import Foundation

@MainActor
final class BackendClient: ObservableObject {
    @Published var helperPath: String
    @Published var events: [BackendEvent] = []
    @Published var isRunning = false
    @Published var lastExitCode: Int32?

    private let runner: HelperRunner
    private var runTask: Task<Void, Never>?

    init(runner: HelperRunner = HelperRunner()) {
        self.runner = runner
        helperPath = ProcessInfo.processInfo.environment["TCAPSULE_HELPER"] ?? ""
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
        runTask = Task {
            let result = await runner.run(
                helperPath: helperPath.isEmpty ? nil : helperPath,
                operation: operation,
                params: params
            ) { event in
                Task { @MainActor in
                    self.events.append(event)
                }
            }
            self.lastExitCode = result.exitCode
            self.isRunning = false
            self.runTask = nil
        }
    }

    func cancel() {
        runTask?.cancel()
    }
}
