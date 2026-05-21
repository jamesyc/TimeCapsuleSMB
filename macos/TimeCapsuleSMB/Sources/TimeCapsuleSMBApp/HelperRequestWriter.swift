import Foundation

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
    private let handle: FileHandle
    private let chunkSize: Int
    private let lock = NSLock()
    private var offset = 0
    private var continuation: CheckedContinuation<Void, Error>?
    private var completed = false

    init(data: Data, handle: FileHandle, chunkSize: Int) {
        self.data = data
        self.handle = handle
        self.chunkSize = max(1, chunkSize)
    }

    func start(continuation: CheckedContinuation<Void, Error>) {
        lock.lock()
        if completed {
            lock.unlock()
            continuation.resume(throwing: CancellationError())
            return
        }
        self.continuation = continuation
        lock.unlock()

        handle.writeabilityHandler = { [weak self] writableHandle in
            self?.writeNextChunk(to: writableHandle)
        }
        writeNextChunk(to: handle)
    }

    func cancel() {
        complete(.failure(CancellationError()))
    }

    private func writeNextChunk(to handle: FileHandle) {
        let chunk: Data
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        let end = min(offset + chunkSize, data.count)
        chunk = data.subdata(in: offset..<end)
        offset = end
        lock.unlock()

        do {
            try handle.write(contentsOf: chunk)
        } catch {
            complete(.failure(error))
            return
        }

        lock.lock()
        let finished = offset >= data.count
        lock.unlock()
        if finished {
            complete(.success(()))
        }
    }

    private func complete(_ result: Result<Void, Error>) {
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        completed = true
        let continuation = self.continuation
        self.continuation = nil
        lock.unlock()

        handle.writeabilityHandler = nil
        continuation?.resume(with: result)
    }
}
