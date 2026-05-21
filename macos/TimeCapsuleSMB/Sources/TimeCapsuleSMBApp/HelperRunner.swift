import Darwin
import Foundation

public struct HelperRunResult: Equatable, Sendable {
    public let exitCode: Int32
    public let sawTerminalEvent: Bool
    public let stderr: String
}

public protocol HelperRunning: Sendable {
    func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult
}

public final class HelperRunner: @unchecked Sendable, HelperRunning {
    private static let pipeReadChunkSize = 4096

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
        context: DeviceRuntimeContext? = nil,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult {
        let terminalTracker = TerminalEventTracker()
        let eventSink: @Sendable (BackendEvent) async -> Void = { event in
            await terminalTracker.record(event)
            await onEvent(event)
        }

        let resolution: HelperResolution
        do {
            resolution = try locator.resolve(helperPath: helperPath)
        } catch {
            await eventSink(BackendEvent.error(operation: operation, code: "helper_not_found", message: error.localizedDescription))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let process = Process()
        process.executableURL = resolution.executableURL
        process.arguments = ["api"]
        process.environment = locator.helperEnvironment(for: resolution, context: context)

        let input = Pipe()
        let output = Pipe()
        let error = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = error

        do {
            try process.run()
        } catch {
            await eventSink(BackendEvent.error(operation: operation, code: "helper_launch_failed", message: error.localizedDescription))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let stdoutTask = Task.detached {
            await Self.readOutput(output.fileHandleForReading, onEvent: eventSink)
        }
        let stderrLimit = self.stderrLimit
        let stderrTask = Task.detached {
            Self.readCapped(error.fileHandleForReading, limit: stderrLimit)
        }

        do {
            var requestParams = params
            if let context, requestParams["config"] == nil {
                requestParams["config"] = .string(context.configURL.path)
            }
            let request = ["operation": JSONValue.string(operation), "params": JSONValue.object(requestParams)]
            let requestData = try JSONEncoder().encode(JSONValue.object(request))
            try input.fileHandleForWriting.write(contentsOf: requestData)
            try input.fileHandleForWriting.close()
        } catch {
            try? input.fileHandleForWriting.close()
            await Self.terminate(process)
            await eventSink(BackendEvent.error(operation: operation, code: "helper_write_failed", message: error.localizedDescription))
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
        let sawTerminalEvent = await terminalTracker.sawTerminalEvent
        if cancelled {
            await eventSink(BackendEvent.error(
                operation: operation,
                code: "cancelled",
                message: L10n.string("helper.error.cancelled"),
                debug: stderrText.isEmpty ? nil : .object(["stderr": .string(stderrText)])
            ))
        } else if !sawTerminalEvent {
            await eventSink(BackendEvent.error(
                operation: operation,
                code: "missing_terminal_event",
                message: L10n.string("helper.error.missing_terminal_event"),
                debug: stderrText.isEmpty ? nil : .object(["stderr": .string(stderrText)])
            ))
        }
        let finalSawTerminalEvent = await terminalTracker.sawTerminalEvent

        return HelperRunResult(
            exitCode: cancelled ? 130 : process.terminationStatus,
            sawTerminalEvent: finalSawTerminalEvent,
            stderr: stderrText
        )
    }

    private static func readOutput(
        _ handle: FileHandle,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async {
        var parser = OutputLineParser()
        while let data = readChunk(from: handle) {
            for event in parser.append(data) {
                await onEvent(event)
            }
        }
        for event in parser.finish() {
            await onEvent(event)
        }
    }

    private static func readCapped(_ handle: FileHandle, limit: Int) -> String {
        var output = Data()
        while let data = readChunk(from: handle) {
            if output.count < limit {
                output.append(data.prefix(limit - output.count))
            }
        }
        return String(decoding: output, as: UTF8.self)
    }

    private static func readChunk(from handle: FileHandle) -> Data? {
        let data: Data?
        do {
            data = try handle.read(upToCount: pipeReadChunkSize)
        } catch {
            return nil
        }
        guard let data, !data.isEmpty else {
            return nil
        }
        return data
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

private actor TerminalEventTracker {
    private var seen = false

    var sawTerminalEvent: Bool {
        seen
    }

    func record(_ event: BackendEvent) {
        guard event.type == "result" || event.type == "error" else { return }
        seen = true
    }
}
