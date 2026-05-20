import Foundation

public final class OutputLineParser: @unchecked Sendable {
    private let lock = NSLock()
    private var buffer = Data()
    private let decoder = JSONDecoder()
    private let onEvent: (BackendEvent) -> Void

    public init(onEvent: @escaping (BackendEvent) -> Void) {
        self.onEvent = onEvent
    }

    public func append(_ data: Data) {
        lock.lock()
        defer { lock.unlock() }
        buffer.append(data)
        consumeCompleteLines()
    }

    public func finish() {
        lock.lock()
        defer { lock.unlock() }
        guard !buffer.isEmpty else { return }
        emit(buffer)
        buffer.removeAll()
    }

    private func consumeCompleteLines() {
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            emit(line)
        }
    }

    private func emit(_ line: Data.SubSequence) {
        guard !line.isEmpty, let event = try? decoder.decode(BackendEvent.self, from: Data(line)) else {
            return
        }
        onEvent(event)
    }
}
