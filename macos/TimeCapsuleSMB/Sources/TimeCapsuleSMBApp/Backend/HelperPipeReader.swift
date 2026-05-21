import Foundation

public protocol HelperPipeReading: Sendable {
    func chunks(from handle: FileHandle) -> AsyncThrowingStream<Data, Error>
}

public final class ReadabilityPipeReader: HelperPipeReading, @unchecked Sendable {
    public init() {}

    public func chunks(from handle: FileHandle) -> AsyncThrowingStream<Data, Error> {
        let state = PipeReadState(handle: handle)
        return AsyncThrowingStream { continuation in
            state.start(continuation: continuation)
        }
    }
}

private final class PipeReadState: @unchecked Sendable {
    private let handle: FileHandle
    private let lock = NSLock()
    private var completed = false

    init(handle: FileHandle) {
        self.handle = handle
    }

    func start(continuation: AsyncThrowingStream<Data, Error>.Continuation) {
        continuation.onTermination = { _ in
            self.finish()
        }

        handle.readabilityHandler = { readableHandle in
            self.readAvailableData(from: readableHandle, continuation: continuation)
        }
    }

    private func readAvailableData(
        from readableHandle: FileHandle,
        continuation: AsyncThrowingStream<Data, Error>.Continuation
    ) {
        guard !isCompleted else {
            return
        }
        let data = readableHandle.availableData
        guard !data.isEmpty else {
            finish()
            continuation.finish()
            return
        }
        continuation.yield(data)
    }

    private var isCompleted: Bool {
        lock.lock()
        let value = completed
        lock.unlock()
        return value
    }

    private func finish() {
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        completed = true
        lock.unlock()
        handle.readabilityHandler = nil
    }
}
