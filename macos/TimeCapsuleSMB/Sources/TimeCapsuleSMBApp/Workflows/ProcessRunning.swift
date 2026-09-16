import Foundation

struct ProcessOutput: Equatable, Sendable {
    let exitCode: Int32
    let stdout: String
    let stderr: String
}

/// Runs a local executable to completion. Injected so tests can script `ditto`, `codesign` and `spctl`.
protocol ProcessRunning {
    func run(_ executable: String, _ arguments: [String]) async throws -> ProcessOutput
}

struct FoundationProcessRunner: ProcessRunning {
    func run(_ executable: String, _ arguments: [String]) async throws -> ProcessOutput {
        try await withCheckedThrowingContinuation { continuation in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: executable)
            process.arguments = arguments
            let stdoutPipe = Pipe()
            let stderrPipe = Pipe()
            process.standardOutput = stdoutPipe
            process.standardError = stderrPipe
            process.terminationHandler = { finished in
                let stdout = String(decoding: stdoutPipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                let stderr = String(decoding: stderrPipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                continuation.resume(returning: ProcessOutput(
                    exitCode: finished.terminationStatus,
                    stdout: stdout,
                    stderr: stderr
                ))
            }
            do {
                try process.run()
            } catch {
                continuation.resume(throwing: error)
            }
        }
    }
}
