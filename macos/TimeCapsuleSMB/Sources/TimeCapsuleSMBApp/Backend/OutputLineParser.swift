import Foundation

public struct OutputLineParser {
    private var buffer = Data()
    private let decoder = JSONDecoder()

    public init() {
    }

    public mutating func append(_ data: Data) -> [BackendEvent] {
        buffer.append(data)
        return consumeCompleteLines()
    }

    public mutating func finish() -> [BackendEvent] {
        guard !buffer.isEmpty else { return [] }
        let event = decode(buffer)
        buffer.removeAll()
        return event.map { [$0] } ?? []
    }

    private mutating func consumeCompleteLines() -> [BackendEvent] {
        var events: [BackendEvent] = []
        while let newline = buffer.firstIndex(of: 0x0A) {
            let line = buffer.prefix(upTo: newline)
            buffer.removeSubrange(...newline)
            if let event = decode(line) {
                events.append(event)
            }
        }
        return events
    }

    private func decode(_ line: Data.SubSequence) -> BackendEvent? {
        guard !line.isEmpty else { return nil }
        return try? decoder.decode(BackendEvent.self, from: Data(line))
    }
}
