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
        requestID: String,
        context: DeviceRuntimeContext?,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async -> HelperRunResult
}

public final class HelperRunner: @unchecked Sendable, HelperRunning {
    private let locator: HelperLocator
    private let stderrLimit: Int
    private let requestWriter: any HelperRequestWriting
    private let pipeReader: any HelperPipeReading

    public init(
        locator: HelperLocator = HelperLocator(),
        stderrLimit: Int = 64 * 1024,
        requestWriter: any HelperRequestWriting = PipeRequestWriter(),
        pipeReader: any HelperPipeReading = ReadabilityPipeReader()
    ) {
        self.locator = locator
        self.stderrLimit = stderrLimit
        self.requestWriter = requestWriter
        self.pipeReader = pipeReader
    }

    public func run(
        helperPath: String?,
        operation: String,
        params: [String: JSONValue],
        requestID: String,
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
            await eventSink(BackendEvent.error(operation: operation, code: "helper_not_found", message: error.localizedDescription, requestId: requestID))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let process = Process()
        process.executableURL = resolution.executableURL
        process.arguments = ["api"]
        process.environment = locator.helperEnvironment(for: resolution, context: context)

        let input = Pipe()
        let output = Pipe()
        let stderrPipe = Pipe()
        process.standardInput = input
        process.standardOutput = output
        process.standardError = stderrPipe

        do {
            try process.run()
        } catch {
            await eventSink(BackendEvent.error(operation: operation, code: "helper_launch_failed", message: error.localizedDescription, requestId: requestID))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: "")
        }

        let pipeReader = self.pipeReader
        let stdoutTask = Task.detached {
            await Self.readOutput(output.fileHandleForReading, pipeReader: pipeReader, onEvent: eventSink)
        }
        let stderrLimit = self.stderrLimit
        let stderrTask = Task.detached {
            await Self.readCapped(stderrPipe.fileHandleForReading, limit: stderrLimit, pipeReader: pipeReader)
        }

        let requestData: Data
        do {
            var requestParams = params
            if let context, requestParams["config"] == nil {
                requestParams["config"] = .string(context.configURL.path)
            }
            let request = [
                "operation": JSONValue.string(operation),
                "request_id": JSONValue.string(requestID),
                "params": JSONValue.object(requestParams)
            ]
            requestData = try JSONEncoder().encode(JSONValue.object(request))
        } catch {
            await Self.terminate(process)
            await eventSink(BackendEvent.error(operation: operation, code: "helper_write_failed", message: error.localizedDescription, requestId: requestID))
            await stdoutTask.value
            let stderr = await stderrTask.value
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: stderr)
        }

        let writeResult = await writeRequest(requestData, to: input.fileHandleForWriting, process: process)

        if case .failure(let error) = writeResult {
            if Task.isCancelled || error is CancellationError {
                await Self.cancelForTelemetry(process)
            } else {
                await Self.terminate(process)
            }
            await stdoutTask.value
            let stderr = await stderrTask.value
            if Task.isCancelled || error is CancellationError {
                let sawTerminalEvent = await terminalTracker.sawTerminalEvent
                if !sawTerminalEvent {
                    await eventSink(BackendEvent.error(
                        operation: operation,
                        code: "cancelled",
                        message: L10n.string("helper.error.cancelled"),
                        requestId: requestID,
                        debug: stderr.isEmpty ? nil : .object(["stderr": .string(stderr)])
                    ))
                }
                return HelperRunResult(exitCode: 130, sawTerminalEvent: await terminalTracker.sawTerminalEvent, stderr: stderr)
            }
            await eventSink(BackendEvent.error(operation: operation, code: "helper_write_failed", message: error.localizedDescription, requestId: requestID))
            return HelperRunResult(exitCode: 1, sawTerminalEvent: true, stderr: stderr)
        }

        await withTaskCancellationHandler {
            await Self.waitForExit(process)
        } onCancel: {
            Task {
                await Self.cancelForTelemetry(process)
            }
        }
        let cancelled = Task.isCancelled

        await stdoutTask.value
        let stderrText = await stderrTask.value
        let sawTerminalEvent = await terminalTracker.sawTerminalEvent
        if cancelled && !sawTerminalEvent {
            await eventSink(BackendEvent.error(
                operation: operation,
                code: "cancelled",
                message: L10n.string("helper.error.cancelled"),
                requestId: requestID,
                debug: stderrText.isEmpty ? nil : .object(["stderr": .string(stderrText)])
            ))
        } else if !sawTerminalEvent {
            await eventSink(BackendEvent.error(
                operation: operation,
                code: "missing_terminal_event",
                message: L10n.string("helper.error.missing_terminal_event"),
                requestId: requestID,
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

    private func writeRequest(_ data: Data, to handle: FileHandle, process: Process) async -> Result<Void, Error> {
        let requestWriter = self.requestWriter
        return await withTaskCancellationHandler {
            defer {
                try? handle.close()
            }
            do {
                try await requestWriter.write(data, to: handle)
                return .success(())
            } catch {
                return .failure(error)
            }
        } onCancel: {
            Task {
                await Self.cancelForTelemetry(process)
            }
        }
    }

    private static func readOutput(
        _ handle: FileHandle,
        pipeReader: any HelperPipeReading,
        onEvent: @escaping @Sendable (BackendEvent) async -> Void
    ) async {
        var parser = OutputLineParser()
        do {
            for try await data in pipeReader.chunks(from: handle) {
                for event in parser.append(data) {
                    await onEvent(event)
                }
            }
        } catch {
            return
        }
        for event in parser.finish() {
            await onEvent(event)
        }
    }

    private static func readCapped(
        _ handle: FileHandle,
        limit: Int,
        pipeReader: any HelperPipeReading
    ) async -> String {
        var output = Data()
        do {
            for try await data in pipeReader.chunks(from: handle) {
                if output.count < limit {
                    output.append(data.prefix(limit - output.count))
                }
            }
        } catch {
            return String(decoding: output, as: UTF8.self)
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
            let pid = process.processIdentifier
            if pid > 0 {
                kill(pid, SIGKILL)
            }
        }
    }

    private static func cancelForTelemetry(_ process: Process) async {
        guard process.isRunning else {
            return
        }
        let pid = process.processIdentifier
        guard pid > 0 else {
            return
        }
        kill(pid, SIGINT)
        for _ in 0..<20 {
            if !process.isRunning {
                return
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        await terminate(process)
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
