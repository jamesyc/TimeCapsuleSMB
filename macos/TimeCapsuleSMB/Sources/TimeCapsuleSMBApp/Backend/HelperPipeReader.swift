import Darwin
import Dispatch
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
    private let fileDescriptor: CInt
    private let queue = DispatchQueue(label: "TimeCapsuleSMB.PipeReader")
    private let lock = NSLock()
    private var source: DispatchSourceRead?
    private var originalFlags: CInt?
    private var completed = false

    init(handle: FileHandle) {
        fileDescriptor = handle.fileDescriptor
    }

    func start(continuation: AsyncThrowingStream<Data, Error>.Continuation) {
        continuation.onTermination = { [weak self] _ in
            self?.finish()
        }

        queue.async {
            guard !self.isCompleted else {
                return
            }

            let originalFlags = fcntl(self.fileDescriptor, F_GETFL)
            guard originalFlags != -1 else {
                self.finish {
                    continuation.finish(throwing: Self.posixError(errno))
                }
                return
            }
            if fcntl(self.fileDescriptor, F_SETFL, originalFlags | O_NONBLOCK) == -1 {
                self.finish {
                    continuation.finish(throwing: Self.posixError(errno))
                }
                return
            }

            let source = DispatchSource.makeReadSource(fileDescriptor: self.fileDescriptor, queue: self.queue)
            source.setEventHandler {
                self.readAvailableData(continuation: continuation)
            }

            self.lock.lock()
            guard !self.completed else {
                self.lock.unlock()
                _ = fcntl(self.fileDescriptor, F_SETFL, originalFlags)
                source.resume()
                source.cancel()
                return
            }
            self.originalFlags = originalFlags
            self.source = source
            source.resume()
            self.lock.unlock()
        }
    }

    private func readAvailableData(continuation: AsyncThrowingStream<Data, Error>.Continuation) {
        guard !isCompleted else {
            return
        }

        while true {
            var buffer = [UInt8](repeating: 0, count: 4096)
            let bytesRead = Darwin.read(fileDescriptor, &buffer, buffer.count)

            if bytesRead > 0 {
                continuation.yield(Data(buffer[0..<bytesRead]))
                continue
            }

            if bytesRead == 0 {
                finish {
                    continuation.finish()
                }
                return
            }

            switch errno {
            case EAGAIN, EWOULDBLOCK:
                return
            case EINTR:
                continue
            default:
                let error = Self.posixError(errno)
                finish {
                    continuation.finish(throwing: error)
                }
                return
            }
        }
    }

    private var isCompleted: Bool {
        lock.lock()
        let value = completed
        lock.unlock()
        return value
    }

    private func finish(_ finishContinuation: (() -> Void)? = nil) {
        lock.lock()
        guard !completed else {
            lock.unlock()
            return
        }
        completed = true
        let source = source
        self.source = nil
        let originalFlags = originalFlags
        self.originalFlags = nil
        lock.unlock()

        if let originalFlags {
            _ = fcntl(fileDescriptor, F_SETFL, originalFlags)
        }
        source?.cancel()
        finishContinuation?()
    }

    private static func posixError(_ code: CInt) -> POSIXError {
        POSIXError(POSIXErrorCode(rawValue: code) ?? .EIO)
    }
}
