import Foundation

@MainActor
final class BackendClient: ObservableObject {
    @Published var helperPath: String
    @Published var events: [BackendEvent] = []
    @Published var isRunning = false
    @Published var lastExitCode: Int32?

    init() {
        helperPath = ProcessInfo.processInfo.environment["TCAPSULE_HELPER"] ?? ".venv/bin/tcapsule"
    }

    func clear() {
        events.removeAll()
        lastExitCode = nil
    }

    func run(operation: String, params: [String: JSONValue] = [:]) {
        guard !isRunning else { return }
        isRunning = true
        lastExitCode = nil
        let helperPath = self.helperPath
        Task.detached {
            let exitCode = await Self.runHelper(
                helperPath: helperPath,
                operation: operation,
                params: params
            ) { event in
                Task { @MainActor in
                    self.events.append(event)
                }
            }
            await MainActor.run {
                self.lastExitCode = exitCode
                self.isRunning = false
            }
        }
    }

    private static func runHelper(
        helperPath: String,
        operation: String,
        params: [String: JSONValue],
        onEvent: @escaping (BackendEvent) -> Void
    ) async -> Int32 {
        let process = Process()
        process.executableURL = helperURL(for: helperPath)
        process.arguments = ["api"]
        process.environment = helperEnvironment()

        let input = Pipe()
        let output = Pipe()
        let error = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = error

        let decoder = JSONDecoder()
        let parser = OutputLineParser(onEvent: onEvent)
        output.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            parser.append(data)
        }

        do {
            try process.run()
            let request = ["operation": JSONValue.string(operation), "params": JSONValue.object(params)]
            let requestData = try JSONEncoder().encode(JSONValue.object(request))
            input.fileHandleForWriting.write(requestData)
            input.fileHandleForWriting.closeFile()
            process.waitUntilExit()
            output.fileHandleForReading.readabilityHandler = nil
            _ = error.fileHandleForReading.readDataToEndOfFile()
            return process.terminationStatus
        } catch {
            let fallback = """
            {"type":"error","operation":"\(operation)","message":"\(error.localizedDescription)"}
            """
            if let data = fallback.data(using: .utf8), let event = try? decoder.decode(BackendEvent.self, from: data) {
                onEvent(event)
            }
            return 1
        }
    }

    private static func helperURL(for path: String) -> URL {
        if path.hasPrefix("/") {
            return URL(fileURLWithPath: path)
        }
        return URL(fileURLWithPath: FileManager.default.currentDirectoryPath).appendingPathComponent(path)
    }

    private static func helperEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        guard
            let appSupport = FileManager.default.urls(
                for: .applicationSupportDirectory,
                in: .userDomainMask
            ).first?.appendingPathComponent("TimeCapsuleSMB", isDirectory: true)
        else {
            return environment
        }
        try? FileManager.default.createDirectory(at: appSupport, withIntermediateDirectories: true)
        if environment["TCAPSULE_CONFIG"] == nil {
            environment["TCAPSULE_CONFIG"] = appSupport.appendingPathComponent(".env").path
        }
        if environment["TCAPSULE_STATE_DIR"] == nil {
            environment["TCAPSULE_STATE_DIR"] = appSupport.path
        }
        return environment
    }
}

private final class OutputLineParser: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()
    private let decoder = JSONDecoder()
    private let onEvent: (BackendEvent) -> Void

    init(onEvent: @escaping (BackendEvent) -> Void) {
        self.onEvent = onEvent
    }

    func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        buffer.append(data)
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            guard !line.isEmpty, let event = try? decoder.decode(BackendEvent.self, from: line) else {
                continue
            }
            onEvent(event)
        }
    }
}
