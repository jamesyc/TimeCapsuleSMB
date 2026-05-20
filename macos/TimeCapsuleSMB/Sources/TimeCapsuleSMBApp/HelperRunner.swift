import Darwin
import Foundation

public struct HelperRunResult: Equatable {
    public let exitCode: Int32
    public let sawTerminalEvent: Bool
    public let stderr: String
}

public final class HelperRunner {
    private let locator: HelperLocator
    private let stderrLimit: Int

    public init(locator: HelperLocator = HelperLocator(), stderrLimit: Int = 64 * 1024) {
        self.locator = locator
        self.stderrLimit = stderrLimit
    }

    public func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        onEvent: @escaping (BackendEvent) -> Void
    ) async -> HelperRunResult {
        let terminalTracker = TerminalEventTracker()
        let eventSink: (BackendEvent) -> Void = { event in
            terminalTracker.record(event)
            onEvent(event)
        }

        let resolution: HelperResolution
        do {
            resolution = try locator.resolve(helperPath: helperPath)
        } catch {
            eventSink(BackendEvent.error(operation: operation, code: "helper_not_found", message: error.localizedDescription))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let process = Process()
        process.executableURL = resolution.executableURL
        process.arguments = ["api"]
        process.environment = locator.helperEnvironment(for: resolution)

        let input = Pipe()
        let output = Pipe()
        let error = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = error

        let parser = OutputLineParser(onEvent: eventSink)
        do {
            try process.run()
        } catch {
            eventSink(BackendEvent.error(operation: operation, code: "helper_launch_failed", message: error.localizedDescription))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let stdoutTask = Task.detached {
            Self.readOutput(output.fileHandleForReading, parser: parser)
        }
        let stderrTask = Task.detached {
            Self.readCapped(error.fileHandleForReading, limit: self.stderrLimit)
        }

        do {
            let request = ["operation": JSONValue.string(operation), "params": JSONValue.object(params)]
            let requestData = try JSONEncoder().encode(JSONValue.object(request))
            try input.fileHandleForWriting.write(contentsOf: requestData)
            try input.fileHandleForWriting.close()
        } catch {
            try? input.fileHandleForWriting.close()
            await Self.terminate(process)
            eventSink(BackendEvent.error(operation: operation, code: "helper_write_failed", message: error.localizedDescription))
            await stdoutTask.value
            let stderr = await stderrTask.value
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: stderr)
        }

        await withTaskCancellationHandler {
            await Self.waitForExit(process)
        } onCancel: {
            Task {
                await Self.terminate(process)
            }
        }
        let cancelled = Task.isCancelled

        await stdoutTask.value
        let stderrText = await stderrTask.value
        let sawTerminalEvent = terminalTracker.sawTerminalEvent
        if cancelled {
            eventSink(BackendEvent.error(
                operation: operation,
                code: "cancelled",
                message: "Operation cancelled.",
                debug: stderrText.isEmpty ? nil : .object(["stderr": .string(stderrText)])
            ))
        } else if !sawTerminalEvent {
            eventSink(BackendEvent.error(
                operation: operation,
                code: "missing_terminal_event",
                message: "Helper exited without a result or error event.",
                debug: stderrText.isEmpty ? nil : .object(["stderr": .string(stderrText)])
            ))
        }

        return HelperRunResult(
            exitCode: cancelled ? 130 : process.terminationStatus,
            sawTerminalEvent: terminalTracker.sawTerminalEvent,
            stderr: stderrText
        )
    }

    private static func readOutput(_ handle: FileHandle, parser: OutputLineParser) {
        while true {
            let data = handle.availableData
            if data.isEmpty {
                parser.finish()
                return
            }
            parser.append(data)
        }
    }

    private static func readCapped(_ handle: FileHandle, limit: Int) -> String {
        var output = Data()
        while true {
            let data = handle.availableData
            if data.isEmpty {
                break
            }
            if output.count < limit {
                output.append(data.prefix(limit - output.count))
            }
        }
        return String(decoding: output, as: UTF8.self)
    }

    private static func waitForExit(_ process: Process) async {
        if !process.isRunning {
            return
        }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            let box = TerminationContinuation(continuation)
            process.terminationHandler = { _ in
                box.resume()
            }
            if !process.isRunning {
                box.resume()
            }
        }
        process.terminationHandler = nil
    }

    private static func terminate(_ process: Process) async {
        process.terminate()
        for _ in 0..<10 {
            if !process.isRunning {
                return
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        if process.isRunning {
            kill(process.processIdentifier, SIGKILL)
        }
    }
}

private final class TerminationContinuation: @unchecked Sendable {
    private let lock = NSLock()
    private var continuation: CheckedContinuation<Void, Never>?

    init(_ continuation: CheckedContinuation<Void, Never>) {
        self.continuation = continuation
    }

    func resume() {
        lock.lock()
        let continuation = continuation
        self.continuation = nil
        lock.unlock()
        continuation?.resume()
    }
}

private final class TerminalEventTracker: @unchecked Sendable {
    private let lock = NSLock()
    private var seen = false

    var sawTerminalEvent: Bool {
        lock.lock()
        defer { lock.unlock() }
        return seen
    }

    func record(_ event: BackendEvent) {
        guard event.type == "result" || event.type == "error" else { return }
        lock.lock()
        seen = true
        lock.unlock()
    }
}
