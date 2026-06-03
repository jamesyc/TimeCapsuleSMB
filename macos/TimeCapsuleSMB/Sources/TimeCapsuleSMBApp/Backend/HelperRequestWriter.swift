import Foundation
#if canImport(Darwin)
import Darwin
#endif
import Dispatch

public protocol HelperRequestWriting: Sendable {
    func write(_ data: Data, to handle: FileHandle) async throws
}

public final class PipeRequestWriter: HelperRequestWriting, @unchecked Sendable {
    private let chunkSize: Int

    public init(chunkSize: Int = 4096) {
        self.chunkSize = chunkSize
    }

    public func write(_ data: Data, to handle: FileHandle) async throws {
        try Task.checkCancellation()
        guard !data.isEmpty else {
            return
        }
        #if canImport(Darwin)
        var noSigpipe: CInt = 1
        _ = fcntl(handle.fileDescriptor, F_SETNOSIGPIPE, &noSigpipe)
        #endif

        let state = PipeRequestWriteState(data: data, handle: handle, chunkSize: chunkSize)
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                state.start(continuation: continuation)
            }
        } onCancel: {
            state.cancel()
        }
    }
}

private final class PipeRequestWriteState: @unchecked Sendable {
    private let data: Data
    private let fileDescriptor: CInt
    private let chunkSize: Int
    private let queue = DispatchQueue(label: "TimeCapsuleSMB.PipeRequestWriter")
    private var offset = 0
    private var originalFlags: CInt?
    private var continuation: CheckedContinuation<Void, Error>?
    private var source: DispatchSourceWrite?
    private var completed = false

    init(data: Data, handle: FileHandle, chunkSize: Int) {
        self.data = data
        self.fileDescriptor = handle.fileDescriptor
        self.chunkSize = max(1, chunkSize)
    }

    func start(continuation: CheckedContinuation<Void, Error>) {
        queue.async {
            if self.completed {
                continuation.resume(throwing: CancellationError())
                return
            }

            let originalFlags = fcntl(self.fileDescriptor, F_GETFL)
            guard originalFlags != -1 else {
                self.completed = true
                continuation.resume(throwing: Self.posixError(errno))
                return
            }
            self.originalFlags = originalFlags
            if fcntl(self.fileDescriptor, F_SETFL, originalFlags | O_NONBLOCK) == -1 {
                self.completed = true
                continuation.resume(throwing: Self.posixError(errno))
                return
            }

            self.continuation = continuation
            let source = DispatchSource.makeWriteSource(fileDescriptor: self.fileDescriptor, queue: self.queue)
            self.source = source
            source.setEventHandler { [weak self] in
                self?.writeAvailableData()
            }
            source.resume()
        }
    }

    func cancel() {
        queue.async {
            self.complete(.failure(CancellationError()))
        }
    }

    private func writeAvailableData() {
        guard !completed else {
            return
        }

        while offset < data.count {
            let length = min(chunkSize, data.count - offset)
            let written = data.withUnsafeBytes { bytes in
                guard let baseAddress = bytes.baseAddress else {
                    return 0
                }
                return Darwin.write(fileDescriptor, baseAddress.advanced(by: offset), length)
            }

            if written > 0 {
                offset += written
                continue
            }

            if written == -1 {
                switch errno {
                case EAGAIN, EWOULDBLOCK:
                    return
                case EINTR:
                    continue
                default:
                    complete(.failure(Self.posixError(errno)))
                    return
                }
            }

            complete(.failure(Self.posixError(EPIPE)))
            return
        }

        complete(.success(()))
    }

    private func complete(_ result: Result<Void, Error>) {
        guard !completed else {
            return
        }
        completed = true
        let continuation = self.continuation
        self.continuation = nil
        let source = self.source
        self.source = nil

        if let originalFlags {
            _ = fcntl(fileDescriptor, F_SETFL, originalFlags)
        }
        source?.cancel()
        continuation?.resume(with: result)
    }

    private static func posixError(_ code: CInt) -> POSIXError {
        POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
    }
}
